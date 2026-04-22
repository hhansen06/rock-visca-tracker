"""
Video capture via GStreamer.

GStreamer ist alleiniger V4L2-Reader und verteilt die Frames per tee:

  v4l2src → tee ┬→ queue → videoconvert → x264enc → mpegtsmux → udpsink  (Stream)
                └→ queue → appsink                                         (Tracking)

Vorteil:
- Stream läuft mit nativer V4L2-Framerate (50fps), unabhängig vom Tracking
- Tracking bekommt immer den neuesten Frame (drop=true), kein Stau
- Nur ein V4L2-Reader — kein Ressourcenkonflikt

VideoCapture ist die öffentliche Klasse.  Nach open() gibt read() per
appsink den jeweils neuesten Frame als (H,W,3) BGR numpy-Array zurück.
"""

import logging
import os
import threading
import time
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# Defaults — werden zur Laufzeit durch VideoCapture(width=, height=) überschrieben
CAPTURE_WIDTH  = int(os.environ.get("RT_CAPTURE_WIDTH",  1920))
CAPTURE_HEIGHT = int(os.environ.get("RT_CAPTURE_HEIGHT", 1080))


class VideoCapture:
    """
    GStreamer-basierte Videoquelle mit integriertem UDP-Stream.

    Öffnet /dev/video0 per v4l2src (io-mode=mmap), splittet per tee:
      - Stream-Branch:   x264enc → mpegtsmux → udpsink  (immer 50 fps)
      - Tracking-Branch: appsink (neuester Frame, ältere werden verworfen)

    stream_cfg: dict mit host, port, bitrate_kbps, preset, ttl, fps
                Wenn None, wird kein Stream gestartet.
    """

    def __init__(self,
                 device: Optional[str] = None,
                 stream_cfg: Optional[dict] = None,
                 width: Optional[int] = None,
                 height: Optional[int] = None):
        self.device     = device or "/dev/video0"
        self.stream_cfg = stream_cfg

        # Auflösung: Parameter > Env-Var > Modul-Default
        global CAPTURE_WIDTH, CAPTURE_HEIGHT
        if width:
            CAPTURE_WIDTH  = width
        if height:
            CAPTURE_HEIGHT = height

        self._pipeline  = None
        self._appsink   = None
        self._loop      = None
        self._loop_thread: Optional[threading.Thread] = None
        self._last_frame: Optional[np.ndarray] = None
        self._lock = threading.Lock()
        self._frame_event = threading.Event()

    # ------------------------------------------------------------------
    def open(self) -> bool:
        try:
            import gi
            gi.require_version("Gst", "1.0")
            gi.require_version("GLib", "2.0")
            from gi.repository import Gst, GLib
        except Exception as e:
            logger.error(f"GStreamer nicht verfügbar: {e}")
            return False

        Gst.init(None)

        # Auto-detect device if needed
        if self.device is None:
            self.device = _find_hdmirx_device()
        if self.device is None:
            logger.error("Kein Videogerät gefunden")
            return False

        pipe_str = self._build_pipeline()
        logger.debug(f"GStreamer pipeline: {pipe_str}")

        try:
            self._pipeline = Gst.parse_launch(pipe_str)
        except Exception as e:
            logger.error(f"GStreamer pipeline Fehler: {e}")
            return False

        self._appsink = self._pipeline.get_by_name("tracking_sink")

        # GLib mainloop im Hintergrund
        self._loop = GLib.MainLoop()
        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)
        self._loop_thread = threading.Thread(
            target=self._loop.run, daemon=True, name="gst-main")
        self._loop_thread.start()

        self._pipeline.set_state(Gst.State.PLAYING)

        # Warten bis erste Frame ankommen
        logger.debug("Warte auf ersten Frame...")
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            sample = self._appsink.emit("pull-sample")
            if sample:
                frame = self._sample_to_array(sample)
                if frame is not None:
                    with self._lock:
                        self._last_frame = frame
                    logger.debug(f"Erster Frame: {frame.shape[1]}x{frame.shape[0]}")
                    # Start background pull thread
                    self._running = True
                    self._pull_thread = threading.Thread(
                        target=self._pull_loop, daemon=True, name="appsink-pull")
                    self._pull_thread.start()
                    stream_info = ""
                    if self.stream_cfg:
                        cfg = self.stream_cfg
                        audio_dev = cfg.get("audio_device", "").strip()
                        stream_info = (f"  Stream → {cfg['host']}:{cfg['port']} "
                                       f"QP={cfg.get('qp', 23)}"
                                       + (f"  Audio={audio_dev}" if audio_dev else "  Audio=aus"))
                    logger.info(f"Video gestartet: {self.device} "
                                f"@ {CAPTURE_WIDTH}x{CAPTURE_HEIGHT}{stream_info}")
                    return True
            time.sleep(0.05)

        logger.warning("Kein Frame innerhalb 8 s — fortsetzen trotzdem")
        return True

    def _build_pipeline(self) -> str:
        src = (
            f"v4l2src device={self.device} io-mode=mmap ! "
            f"video/x-raw,format=BGR,width={CAPTURE_WIDTH},height={CAPTURE_HEIGHT} ! "
            f"tee name=t "
        )

        # Stream branch
        if self.stream_cfg:
            cfg         = self.stream_cfg
            fps         = int(cfg.get("fps", 15))
            host        = cfg["host"]
            port        = cfg["port"]
            ttl         = cfg.get("ttl", 64)
            qp          = cfg.get("qp", 23)
            keyint      = max(1, fps // 2)
            audio_dev   = cfg.get("audio_device", "").strip()
            audio_br    = int(cfg.get("audio_bitrate", 128000))

            if audio_dev:
                # Video-Branch → mpegtsmux
                video = (
                    f"t. ! queue name=stream_q max-size-buffers=2 leaky=downstream ! "
                    f"videorate drop-only=true ! video/x-raw,framerate={fps}/1 ! "
                    f"mpph264enc qp={qp} keyint={keyint} ! "
                    f"h264parse config-interval=-1 ! "
                    f"mux. "
                )
                # Audio-Branch: alsasrc → mono → AAC → mpegtsmux
                audio = (
                    f"alsasrc device={audio_dev} ! "
                    f"audioconvert ! "
                    f"audio/x-raw,channels=1 ! "
                    f"voaacenc bitrate={audio_br} ! "
                    f"aacparse ! "
                    f"mux. "
                )
                mux = (
                    f"mpegtsmux name=mux alignment=7 ! "
                    f"udpsink host={host} port={port} ttl={ttl} sync=false "
                )
                stream = video + audio + mux
            else:
                # Kein Audio: wie bisher
                stream = (
                    f"t. ! queue name=stream_q max-size-buffers=2 leaky=downstream ! "
                    f"videorate drop-only=true ! video/x-raw,framerate={fps}/1 ! "
                    f"mpph264enc qp={qp} keyint={keyint} ! "
                    f"h264parse config-interval=-1 ! "
                    f"mpegtsmux alignment=7 ! "
                    f"udpsink host={host} port={port} ttl={ttl} sync=false "
                )
        else:
            # No streaming: discard stream branch
            stream = "t. ! queue max-size-buffers=2 leaky=downstream ! fakesink sync=false "

        # Tracking branch: always take newest frame, drop the rest
        tracking = (
            f"t. ! queue name=track_q max-size-buffers=1 leaky=downstream ! "
            f"appsink name=tracking_sink emit-signals=false "
            f"max-buffers=1 drop=true sync=false"
        )

        return src + stream + tracking

    def _pull_loop(self):
        """Background thread: continuously pull frames from appsink."""
        while self._running:
            sample = self._appsink.emit("pull-sample")
            if sample is None:
                time.sleep(0.005)
                continue
            frame = self._sample_to_array(sample)
            if frame is not None:
                with self._lock:
                    self._last_frame = frame
                    self._frame_event.set()

    def _sample_to_array(self, sample) -> Optional[np.ndarray]:
        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
            buf = sample.get_buffer()
            ok, info = buf.map(Gst.MapFlags.READ)
            if not ok:
                return None
            arr = np.frombuffer(info.data, dtype=np.uint8).reshape(
                CAPTURE_HEIGHT, CAPTURE_WIDTH, 3).copy()
            buf.unmap(info)
            return arr
        except Exception:
            return None

    def _on_bus_message(self, bus, msg):
        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
            if msg.type == Gst.MessageType.ERROR:
                err, dbg = msg.parse_error()
                logger.error(f"GStreamer Fehler: {err}  {dbg}")
        except Exception:
            pass

    # ------------------------------------------------------------------
    def read(self) -> tuple[bool, Optional[np.ndarray]]:
        """Blockiert bis ein neuer Frame verfügbar ist (max. 200ms Timeout)."""
        self._frame_event.wait(timeout=0.2)
        self._frame_event.clear()
        with self._lock:
            frame = self._last_frame
        if frame is None:
            return False, None
        return True, frame

    @property
    def width(self) -> int:
        return CAPTURE_WIDTH

    @property
    def height(self) -> int:
        return CAPTURE_HEIGHT

    def release(self):
        self._running = False
        if hasattr(self, "_pull_thread") and self._pull_thread:
            self._pull_thread.join(timeout=2.0)
        if self._pipeline:
            self._pipeline.set_state_async = None  # suppress warnings
            self._pipeline.set_state(
                __import__("gi.repository", fromlist=["Gst"]).Gst.State.NULL)
            self._pipeline = None
        if self._loop and self._loop.is_running():
            self._loop.quit()
        if self._loop_thread:
            self._loop_thread.join(timeout=2.0)

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.release()


# ---------------------------------------------------------------------------
# Device detection (kept for compatibility / manual override)
# ---------------------------------------------------------------------------

def _find_hdmirx_device() -> Optional[str]:
    import subprocess, re
    try:
        result = subprocess.run(
            ["v4l2-ctl", "--list-devices"],
            capture_output=True, text=True, timeout=5)
        current = None
        for line in result.stdout.splitlines():
            if line and not line.startswith('\t'):
                current = line.strip().rstrip(':')
            elif line.startswith('\t') and current:
                path = line.strip()
                if re.match(r'^/dev/video\d+$', path):
                    if "hdmi" in current.lower():
                        return path
    except Exception:
        pass
    return "/dev/video0"


# Keep old name for any code that imports it
find_hdmirx_device = _find_hdmirx_device
list_v4l2_devices  = lambda: [{"path": f"/dev/video{i}", "name": "unknown"} for i in range(8)]
