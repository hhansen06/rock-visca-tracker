"""
Object detection and tracking using either:
  A) YOLOv8 via Ultralytics (CPU, original behaviour)
  B) YOLOv8 via RKNN Lite2 (RK3588 NPU, preferred when .rknn model exists)

Backend is chosen automatically at load() time:
  - If <model_stem>.rknn exists alongside the .pt file → RKNN backend
  - Otherwise                                          → Ultralytics/CPU backend

Unterstützte Modi:
  - vehicles  → COCO-Klassen (car, motorcycle, bus, truck) mit yolov8n.pt / yolov8n.rknn
  - faces     → Gesichtserkennung mit yolov8n-face.pt / yolov8n-face.rknn
"""

import logging
import os
import time
import numpy as np
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# COCO class IDs für Fahrzeuge
VEHICLE_CLASSES = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}

# yolov8-face hat nur eine Klasse
FACE_CLASSES = {0: "face"}

# Input size for RKNN — determined at load() time from model filename
# (models exported with imgsz=320 contain "320" in the name)
RKNN_INPUT_SIZE_DEFAULT = 640


def _classes_for_mode(mode: str) -> dict[int, str]:
    if mode == "faces":
        return FACE_CLASSES
    return VEHICLE_CLASSES


@dataclass
class TrackedObject:
    """Repräsentiert das aktuell verfolgte Objekt (Fahrzeug oder Gesicht)."""
    track_id: int
    bbox: tuple[int, int, int, int]   # x1, y1, x2, y2
    confidence: float
    class_id: int
    label: str
    frames_lost: int = 0
    center_x: float = field(init=False)
    center_y: float = field(init=False)

    def __post_init__(self):
        self._update_center()

    def update(self, bbox: tuple[int, int, int, int], confidence: float):
        self.bbox = bbox
        self.confidence = confidence
        self.frames_lost = 0
        self._update_center()

    def _update_center(self):
        x1, y1, x2, y2 = self.bbox
        self.center_x = (x1 + x2) / 2.0
        self.center_y = (y1 + y2) / 2.0

    @property
    def width(self) -> int:
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self) -> int:
        return self.bbox[3] - self.bbox[1]


# Rückwärtskompatibilität
TrackedVehicle = TrackedObject


# ---------------------------------------------------------------------------
# Simple IoU tracker (used by RKNN backend instead of ByteTrack)
# ---------------------------------------------------------------------------

def _iou(a: tuple, b: tuple) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    ua = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / ua if ua > 0 else 0.0


class _IoUTracker:
    """
    Minimal IoU-based tracker that assigns stable IDs to detections.
    No Kalman filter — just greedy nearest-IoU matching.
    """

    def __init__(self, iou_threshold: float = 0.35, max_lost: int = 5):
        self._next_id = 1
        self._tracks: dict[int, dict] = {}   # id → {bbox, lost}
        self._iou_threshold = iou_threshold
        self._max_lost = max_lost

    def update(self, bboxes: list[tuple]) -> list[tuple[int, tuple]]:
        """
        bboxes: list of (x1, y1, x2, y2)
        Returns: list of (track_id, bbox) — same length as matched bboxes
        """
        # Age all existing tracks
        for t in self._tracks.values():
            t["lost"] += 1

        # Greedy matching: for each detection find best overlapping track
        matched_track_ids = set()
        result = []

        for bbox in bboxes:
            best_id = None
            best_iou = self._iou_threshold

            for tid, track in self._tracks.items():
                if tid in matched_track_ids:
                    continue
                s = _iou(bbox, track["bbox"])
                if s > best_iou:
                    best_iou = s
                    best_id = tid

            if best_id is not None:
                self._tracks[best_id]["bbox"] = bbox
                self._tracks[best_id]["lost"] = 0
                matched_track_ids.add(best_id)
                result.append((best_id, bbox))
            else:
                # New track
                new_id = self._next_id
                self._next_id += 1
                self._tracks[new_id] = {"bbox": bbox, "lost": 0}
                result.append((new_id, bbox))

        # Remove dead tracks
        dead = [tid for tid, t in self._tracks.items() if t["lost"] > self._max_lost]
        for tid in dead:
            del self._tracks[tid]

        return result


# ---------------------------------------------------------------------------
# NMS helper
# ---------------------------------------------------------------------------

def _nms(boxes: np.ndarray, scores: np.ndarray, iou_thr: float) -> list[int]:
    """Simple greedy NMS. boxes: (N,4) x1y1x2y2, scores: (N,)."""
    if len(boxes) == 0:
        return []
    order = scores.argsort()[::-1]
    keep = []
    while len(order):
        i = order[0]
        keep.append(int(i))
        if len(order) == 1:
            break
        ious = np.array([_iou(tuple(boxes[i]), tuple(boxes[j])) for j in order[1:]])
        order = order[1:][ious < iou_thr]
    return keep


# ---------------------------------------------------------------------------
# RKNN backend
# ---------------------------------------------------------------------------

class _RKNNBackend:
    """
    Runs YOLOv8 inference on the RK3588 NPU via rknn-toolkit-lite2.

    Handles both:
      - Standard detection output: (1, 84, 8400)  — vehicles mode
      - Pose/face output:          (1, 20, 8400)  — faces mode (5+15 keypoints)
    """

    def __init__(self, rknn_path: str, mode: str, conf: float, iou: float):
        self._rknn_path = rknn_path
        self._mode = mode
        self._conf = conf
        self._iou = iou
        self._rknn = None
        self._tracker = _IoUTracker()
        self._classes = _classes_for_mode(mode)
        self._num_classes = 1 if mode == "faces" else 80
        # Derive input size from filename: yolov8n-face-320.rknn → 320
        import re
        m = re.search(r'-(\d+)\.rknn$', rknn_path)
        self._input_size = int(m.group(1)) if m else RKNN_INPUT_SIZE_DEFAULT
        logger.debug(f"RKNN input size: {self._input_size}px")

    def load(self):
        from rknnlite.api import RKNNLite
        rknn = RKNNLite()
        ret = rknn.load_rknn(self._rknn_path)
        if ret != 0:
            raise RuntimeError(f"RKNNLite.load_rknn failed: {ret}")
        ret = rknn.init_runtime()
        if ret != 0:
            raise RuntimeError(f"RKNNLite.init_runtime failed: {ret}")
        self._rknn = rknn
        logger.info(f"RKNN backend geladen: {self._rknn_path}  mode={self._mode}")

    def infer(self, frame: np.ndarray) -> list[dict]:
        """
        Run inference on an OpenCV BGR frame (any size).
        Returns list of dicts: {track_id, bbox, conf, class_id, label}
        """
        h, w = frame.shape[:2]
        inp, scale, pad_x, pad_y = self._preprocess(frame)

        outputs = self._rknn.inference(inputs=[inp])
        if outputs is None:
            return []

        raw = outputs[0]  # shape: (1, num_outputs, 8400)
        if raw.ndim == 3:
            raw = raw[0]  # → (num_outputs, 8400)

        # Decode boxes + scores
        bboxes_norm, scores, class_ids = self._decode(raw)

        if len(bboxes_norm) == 0:
            return []

        # Scale back to original frame coordinates
        bboxes_px = []
        for (cx, cy, bw, bh) in bboxes_norm:
            # undo letterbox
            x1 = int((cx - bw / 2 - pad_x) / scale)
            y1 = int((cy - bh / 2 - pad_y) / scale)
            x2 = int((cx + bw / 2 - pad_x) / scale)
            y2 = int((cy + bh / 2 - pad_y) / scale)
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(w, x2), min(h, y2)
            bboxes_px.append((x1, y1, x2, y2))

        # NMS
        boxes_arr = np.array(bboxes_px, dtype=np.float32)
        scores_arr = np.array(scores, dtype=np.float32)
        keep = _nms(boxes_arr, scores_arr, self._iou)

        kept_bboxes = [bboxes_px[i] for i in keep]
        kept_scores = [scores[i] for i in keep]
        kept_class_ids = [class_ids[i] for i in keep]

        # IoU tracking
        tracked = self._tracker.update(kept_bboxes)

        detections = []
        for (tid, bbox) in tracked:
            idx = kept_bboxes.index(bbox)
            cls_id = kept_class_ids[idx]
            label = self._classes.get(cls_id, str(cls_id))
            detections.append({
                "track_id": tid,
                "bbox": bbox,
                "conf": kept_scores[idx],
                "class_id": cls_id,
                "label": label,
            })

        return detections

    def _preprocess(self, frame: np.ndarray):
        """Letterbox resize to _input_size × _input_size, return RGB uint8."""
        if frame.ndim < 2:
            raise ValueError(f"Frame has unexpected shape: {frame.shape}")
        h, w = frame.shape[:2]
        if h == 0 or w == 0:
            raise ValueError(f"Invalid frame dimensions: {w}x{h}")
        size = self._input_size
        scale = min(size / w, size / h)
        nw, nh = int(w * scale), int(h * scale)
        if nw == 0 or nh == 0:
            raise ValueError(f"_preprocess: scale={scale:.6f} produced nw={nw} nh={nh} "
                             f"from frame {w}x{h} (shape={frame.shape} dtype={frame.dtype})")

        import cv2
        resized = cv2.resize(frame, (nw, nh))
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)

        canvas = np.full((size, size, 3), 114, dtype=np.uint8)
        pad_x = (size - nw) // 2
        pad_y = (size - nh) // 2
        canvas[pad_y:pad_y + nh, pad_x:pad_x + nw] = rgb

        inp = canvas[np.newaxis, ...]
        return inp, scale, pad_x, pad_y

    def _decode(self, raw: np.ndarray):
        """
        Decode raw YOLO output tensor.

        Detection model: shape (84, 8400)  → [cx,cy,w,h, cls0..cls79]
        Face/pose model: shape (20, 8400)  → [cx,cy,w,h, conf, kp0x,kp0y,kp0v, ...]
        """
        # Replace NaN/Inf with 0 — RKNN can produce garbage in unused anchor slots
        raw = np.nan_to_num(raw, nan=0.0, posinf=0.0, neginf=0.0)

        bboxes, scores, class_ids = [], [], []

        if self._mode == "faces":
            # Row 0-3: cx,cy,w,h  Row 4: objectness
            obj_scores = raw[4, :]           # (8400,)
            mask = obj_scores >= self._conf
            if not np.any(mask):
                return bboxes, scores, class_ids
            indices = np.where(mask)[0]
            for i in indices:
                cx, cy, bw, bh = raw[0, i], raw[1, i], raw[2, i], raw[3, i]
                bboxes.append((cx, cy, bw, bh))
                scores.append(float(obj_scores[i]))
                class_ids.append(0)
        else:
            # Standard detection: rows 0-3 bbox, rows 4-83 class scores
            bbox_raw = raw[:4, :]            # (4, 8400)
            cls_raw  = raw[4:, :]            # (80, 8400)
            cls_scores = cls_raw.max(axis=0) # (8400,)
            cls_ids    = cls_raw.argmax(axis=0)

            # Filter to target classes
            target = set(self._classes.keys())
            mask = (cls_scores >= self._conf) & np.isin(cls_ids, list(target))
            if not np.any(mask):
                return bboxes, scores, class_ids
            indices = np.where(mask)[0]
            for i in indices:
                cx, cy, bw, bh = bbox_raw[0, i], bbox_raw[1, i], bbox_raw[2, i], bbox_raw[3, i]
                bboxes.append((cx, cy, bw, bh))
                scores.append(float(cls_scores[i]))
                class_ids.append(int(cls_ids[i]))

        return bboxes, scores, class_ids

    def release(self):
        if self._rknn:
            self._rknn.release()
            self._rknn = None


# ---------------------------------------------------------------------------
# Main detector class (public API unchanged)
# ---------------------------------------------------------------------------

class VehicleDetector:
    """
    Wraps YOLOv8 Erkennung + Tracking.
    Hält immer nur ein Ziel bis es verschwindet.

    Wählt automatisch den Backend:
      - RKNN (NPU) wenn <model>.rknn vorhanden
      - Ultralytics/CPU sonst

    mode="vehicles" → yolov8n.pt,      erkennt Autos/Motorräder/Busse/LKW
    mode="faces"    → yolov8n-face.pt, erkennt Gesichter
    """

    def __init__(self,
                 model_name: str = "yolov8n.pt",
                 confidence: float = 0.45,
                 iou: float = 0.45,
                 max_lost_frames: int = 15,
                 device: str = "cpu",
                 mode: str = "vehicles"):
        self.model_name = model_name
        self.confidence = confidence
        self.iou = iou
        self.max_lost_frames = max_lost_frames
        self.device = device
        self.mode = mode
        self._classes = _classes_for_mode(mode)
        self._model = None           # Ultralytics YOLO (CPU backend)
        self._rknn_backend: Optional[_RKNNBackend] = None
        self.tracked: Optional[TrackedObject] = None

    # Modelle die nicht im Ultralytics-Hub sind und manuell heruntergeladen werden müssen
    _MANUAL_DOWNLOAD_URLS = {
        "yolov8n-face.pt": "https://github.com/akanametov/yolo-face/releases/download/1.0.0/yolov8n-face.pt",
        "yolov8s-face.pt": "https://github.com/akanametov/yolo-face/releases/download/1.0.0/yolov8s-face.pt",
        "yolov8l-face.pt": "https://github.com/akanametov/yolo-face/releases/download/1.0.0/yolov8l-face.pt",
    }

    def load(self):
        """Lädt das Modell. Bevorzugt RKNN wenn <model>.rknn vorhanden."""
        import os

        rknn_path = os.path.splitext(self.model_name)[0] + ".rknn"
        if os.path.exists(rknn_path):
            self._rknn_backend = _RKNNBackend(
                rknn_path=rknn_path,
                mode=self.mode,
                conf=self.confidence,
                iou=self.iou,
            )
            self._rknn_backend.load()
            return

        # Fallback: Ultralytics CPU
        import urllib.request
        if self.model_name in self._MANUAL_DOWNLOAD_URLS and not os.path.exists(self.model_name):
            url = self._MANUAL_DOWNLOAD_URLS[self.model_name]
            logger.info(f"Lade Modell herunter: {self.model_name} von {url}")
            try:
                urllib.request.urlretrieve(url, self.model_name,
                    reporthook=lambda b, bs, ts: logger.debug(
                        f"  {min(b*bs, ts)}/{ts} Bytes ({min(b*bs,ts)/ts*100:.0f}%)"
                        if ts > 0 else f"  {b*bs} Bytes"
                    )
                )
                logger.info(f"Modell heruntergeladen: {self.model_name}")
            except Exception as e:
                raise RuntimeError(f"Konnte {self.model_name} nicht herunterladen: {e}\n"
                                   f"Bitte manuell laden: wget -O {self.model_name} '{url}'")

        try:
            from ultralytics import YOLO
            self._model = YOLO(self.model_name)
            logger.info(f"YOLOv8 (CPU) geladen: {self.model_name}  mode={self.mode}  "
                        f"classes={list(self._classes.values())}  device={self.device}")
        except ImportError:
            raise RuntimeError("ultralytics nicht installiert. Bitte: pip install ultralytics")

    def process_frame(self, frame: np.ndarray) -> Optional[TrackedObject]:
        """
        Erkennung + Tracking auf einem Frame.
        Gibt das aktuell verfolgte Objekt zurück oder None.
        """
        if self._rknn_backend is not None:
            detections = self._rknn_backend.infer(frame)
        elif self._model is not None:
            detections = self._infer_ultralytics(frame)
        else:
            raise RuntimeError("Modell nicht geladen. load() aufrufen.")

        if self.tracked is None:
            if detections:
                d = detections[0]
                self.tracked = TrackedObject(
                    track_id=d["track_id"],
                    bbox=d["bbox"],
                    confidence=d["conf"],
                    class_id=d["class_id"],
                    label=d["label"],
                )
                logger.info(f"Ziel erfasst: id={self.tracked.track_id} "
                            f"label={self.tracked.label} conf={self.tracked.confidence:.2f}")
        else:
            matched = next(
                (d for d in detections if d["track_id"] == self.tracked.track_id),
                None
            )
            if matched:
                self.tracked.update(matched["bbox"], matched["conf"])
            else:
                self.tracked.frames_lost += 1
                if self.tracked.frames_lost > self.max_lost_frames:
                    logger.info(f"Ziel verloren nach {self.tracked.frames_lost} Frames "
                                f"(id={self.tracked.track_id})")
                    self.tracked = None

        return self.tracked

    def _infer_ultralytics(self, frame: np.ndarray) -> list[dict]:
        """Run inference via Ultralytics (CPU fallback)."""
        track_kwargs = dict(
            persist=True,
            conf=self.confidence,
            iou=self.iou,
            tracker="bytetrack.yaml",
            verbose=False,
            device=self.device,
        )
        if self.mode != "faces":
            track_kwargs["classes"] = list(self._classes.keys())

        results = self._model.track(frame, **track_kwargs)
        return self._parse_results(results)

    def _parse_results(self, results) -> list[dict]:
        """Extrahiert Detektionen mit Track-IDs aus den YOLO-Ergebnissen."""
        detections = []
        for r in results:
            boxes = r.boxes
            if boxes is None:
                continue
            ids = boxes.id
            if ids is None:
                continue
            for i, box in enumerate(boxes):
                cls_id = int(box.cls[0])
                if self.mode != "faces" and cls_id not in self._classes:
                    continue
                label = self._classes.get(cls_id, str(cls_id))
                x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
                detections.append({
                    "track_id": int(ids[i]),
                    "bbox": (x1, y1, x2, y2),
                    "conf": float(box.conf[0]),
                    "class_id": cls_id,
                    "label": label,
                })
        return detections

    def reset(self):
        """Aktuelles Ziel freigeben."""
        self.tracked = None
        logger.debug("Tracker zurückgesetzt")
