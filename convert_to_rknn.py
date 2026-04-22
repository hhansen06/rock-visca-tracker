#!/usr/bin/env python3
"""
Convert YOLOv8 .pt models to .rknn format for RK3588 NPU inference.

Usage (in the conversion venv):
    /root/stream/venv_convert/bin/python convert_to_rknn.py

Steps:
  1. Export .pt → .onnx via ultralytics
  2. Convert .onnx → .rknn via rknn-toolkit2
"""

import os
import sys

MODELS = [
    {
        "pt":   "yolov8n.pt",
        "onnx": "yolov8n.onnx",
        "rknn": "yolov8n.rknn",
        "imgsz": 640,
    },
    {
        "pt":   "yolov8n-face.pt",
        "onnx": "yolov8n-face.onnx",
        "rknn": "yolov8n-face.rknn",
        "imgsz": 640,
    },
]

PLATFORM = "rk3588"  # Rock 5B

# -------------------------------------------------------------------
# Step 1: Export .pt → .onnx
# -------------------------------------------------------------------
print("=== Step 1: Export .pt → .onnx ===")
from ultralytics import YOLO

for m in MODELS:
    pt_path = m["pt"]
    onnx_path = m["onnx"]

    if not os.path.exists(pt_path):
        print(f"  [SKIP] {pt_path} not found")
        continue

    if os.path.exists(onnx_path):
        print(f"  [SKIP] {onnx_path} already exists")
        continue

    print(f"  Exporting {pt_path} → {onnx_path} ...")
    model = YOLO(pt_path)
    model.export(format="onnx", imgsz=m["imgsz"], simplify=True, opset=12, dynamic=False)
    # ultralytics exports to same dir as pt, with .onnx extension
    exported = pt_path.replace(".pt", ".onnx")
    if exported != onnx_path:
        os.rename(exported, onnx_path)
    print(f"  → {onnx_path} ✓")

# -------------------------------------------------------------------
# Step 2: Convert .onnx → .rknn
# -------------------------------------------------------------------
print("\n=== Step 2: Convert .onnx → .rknn ===")
from rknn.api import RKNN

for m in MODELS:
    onnx_path = m["onnx"]
    rknn_path = m["rknn"]

    if not os.path.exists(onnx_path):
        print(f"  [SKIP] {onnx_path} not found")
        continue

    if os.path.exists(rknn_path):
        print(f"  [SKIP] {rknn_path} already exists")
        continue

    print(f"  Converting {onnx_path} → {rknn_path} ...")

    rknn = RKNN(verbose=False)

    # Config: mean/std for uint8 RGB input, quantise to int8
    rknn.config(
        mean_values=[[0, 0, 0]],
        std_values=[[255, 255, 255]],
        target_platform=PLATFORM,
        quantized_dtype="asymmetric_quantized-8",
    )

    ret = rknn.load_onnx(model=onnx_path)
    if ret != 0:
        print(f"  [ERROR] load_onnx failed: {ret}")
        sys.exit(1)

    # Build without quantization dataset (post-training quant optional)
    ret = rknn.build(do_quantization=False)
    if ret != 0:
        print(f"  [ERROR] build failed: {ret}")
        sys.exit(1)

    ret = rknn.export_rknn(rknn_path)
    if ret != 0:
        print(f"  [ERROR] export_rknn failed: {ret}")
        sys.exit(1)

    rknn.release()
    print(f"  → {rknn_path} ✓")

print("\nConversion complete.")
