#!/usr/bin/env python3
"""
rally_tracker – autonomous PTZ camera tracking for motorsport rallye livestreams.

Usage:
  python main.py                  # run tracker (normal operation)
  python main.py --save-home      # drive camera manually then press Enter
                                  # to store current position as start box
  python main.py --config my.yaml # use alternate config file
  python main.py --device /dev/video2  # override video device
"""

import argparse
import logging
import os
import signal
import sys
import time

import yaml


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _env(key: str, default=None):
    """Liest eine Umgebungsvariable; gibt default zurück wenn nicht gesetzt."""
    return os.environ.get(key, default)


def _env_bool(key: str, default: bool) -> bool:
    v = os.environ.get(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def apply_env_overrides(cfg: dict) -> dict:
    """
    Überschreibt config.yaml-Werte mit Umgebungsvariablen (RT_* Präfix).

    Alle Werte aus .env.example sind hier abgebildet.
    Priorität: Umgebungsvariable > config.yaml > eingebauter Default.
    """

    # --- visca ---------------------------------------------------------------
    v = cfg.setdefault("visca", {})
    if _env("RT_VISCA_PORT"):
        v["port"] = _env("RT_VISCA_PORT")

    # --- capture -------------------------------------------------------------
    c = cfg.setdefault("capture", {})
    if _env("RT_CAPTURE_DEVICE"):
        c["device"] = _env("RT_CAPTURE_DEVICE")
    if _env("RT_CAPTURE_WIDTH"):
        c["width"] = int(_env("RT_CAPTURE_WIDTH"))
    if _env("RT_CAPTURE_HEIGHT"):
        c["height"] = int(_env("RT_CAPTURE_HEIGHT"))

    # --- detector ------------------------------------------------------------
    d = cfg.setdefault("detector", {})
    if _env("RT_DETECTOR_MODE"):
        d["mode"] = _env("RT_DETECTOR_MODE")
    if _env("RT_DETECTOR_MODEL"):
        d["model"] = _env("RT_DETECTOR_MODEL")
    if _env("RT_DETECTOR_CONFIDENCE"):
        d["confidence"] = float(_env("RT_DETECTOR_CONFIDENCE"))
    if _env("RT_DETECTOR_IOU"):
        d["iou"] = float(_env("RT_DETECTOR_IOU"))
    if _env("RT_DETECTOR_MAX_LOST_FRAMES"):
        d["max_lost_frames"] = int(_env("RT_DETECTOR_MAX_LOST_FRAMES"))

    # --- tracker -------------------------------------------------------------
    t = cfg.setdefault("tracker", {})
    _tracker_map = {
        "RT_TRACKER_PAN_GAIN":           ("pan_gain",           float),
        "RT_TRACKER_TILT_GAIN":          ("tilt_gain",          float),
        "RT_TRACKER_DEAD_ZONE":          ("dead_zone",          float),
        "RT_TRACKER_PAN_SPEED_MAX":      ("pan_speed_max",      int),
        "RT_TRACKER_TILT_SPEED_MAX":     ("tilt_speed_max",     int),
        "RT_TRACKER_EMA_ALPHA":          ("ema_alpha",          float),
        "RT_TRACKER_GAIN_ADAPT_RATE":    ("gain_adapt_rate",    float),
        "RT_TRACKER_GAIN_RECOVER_RATE":  ("gain_recover_rate",  float),
        "RT_TRACKER_GAIN_MIN_FACTOR":    ("gain_min_factor",    float),
        "RT_TRACKER_PERSIST_ADAPTIVE_GAIN": ("persist_adaptive_gain", lambda v: str(v).strip().lower() in ("1", "true", "yes", "on")),
        "RT_TRACKER_GAIN_STATE_FILE":    ("gain_state_file",    str),
        "RT_TRACKER_GAIN_SAVE_INTERVAL": ("gain_save_interval", float),
        "RT_TRACKER_RETURN_DELAY":       ("return_delay",       float),
        "RT_TRACKER_RETURN_TRAVEL_TIME": ("return_travel_time", float),
        "RT_TRACKER_HOME_PRESET":        ("home_preset",        int),
        "RT_TRACKER_CMD_INTERVAL":       ("cmd_interval",       float),
    }
    for env_key, (cfg_key, cast) in _tracker_map.items():
        if _env(env_key):
            t[cfg_key] = cast(_env(env_key))

    # --- stream --------------------------------------------------------------
    s = cfg.setdefault("stream", {})
    if _env("RT_STREAM_ENABLED") is not None:
        s["enabled"] = _env_bool("RT_STREAM_ENABLED", s.get("enabled", True))
    if _env("RT_STREAM_HOST"):
        s["host"] = _env("RT_STREAM_HOST")
    if _env("RT_STREAM_PORT"):
        s["port"] = int(_env("RT_STREAM_PORT"))
    if _env("RT_STREAM_QP"):
        s["qp"] = int(_env("RT_STREAM_QP"))
    if _env("RT_STREAM_FPS"):
        s["fps"] = float(_env("RT_STREAM_FPS"))
    if _env("RT_STREAM_TTL"):
        s["ttl"] = int(_env("RT_STREAM_TTL"))
    if _env("RT_STREAM_AUDIO_DEVICE") is not None:
        s["audio_device"] = _env("RT_STREAM_AUDIO_DEVICE")
    if _env("RT_STREAM_AUDIO_BITRATE"):
        s["audio_bitrate"] = int(_env("RT_STREAM_AUDIO_BITRATE"))

    # --- api -----------------------------------------------------------------
    a = cfg.setdefault("api", {})
    if _env("RT_API_ENABLED") is not None:
        a["enabled"] = _env_bool("RT_API_ENABLED", a.get("enabled", True))
    if _env("RT_API_HOST"):
        a["host"] = _env("RT_API_HOST")
    if _env("RT_API_PORT"):
        a["port"] = int(_env("RT_API_PORT"))

    # --- mqtt ----------------------------------------------------------------
    m = cfg.setdefault("mqtt", {})
    if _env("RT_MQTT_ENABLED") is not None:
        m["enabled"] = _env_bool("RT_MQTT_ENABLED", m.get("enabled", False))
    if _env("RT_MQTT_HOST"):
        m["host"] = _env("RT_MQTT_HOST")
    if _env("RT_MQTT_PORT"):
        m["port"] = int(_env("RT_MQTT_PORT"))
    if _env("RT_MQTT_USERNAME") is not None:
        m["username"] = _env("RT_MQTT_USERNAME")
    if _env("RT_MQTT_PASSWORD") is not None:
        m["password"] = _env("RT_MQTT_PASSWORD")
    if _env("RT_MQTT_CLIENT_ID") is not None:
        m["client_id"] = _env("RT_MQTT_CLIENT_ID")
    if _env("RT_MQTT_TOPIC_PREFIX"):
        m["topic_prefix"] = _env("RT_MQTT_TOPIC_PREFIX")
    if _env("RT_MQTT_QOS"):
        m["qos"] = int(_env("RT_MQTT_QOS"))
    if _env("RT_MQTT_STATUS_INTERVAL"):
        m["status_interval"] = float(_env("RT_MQTT_STATUS_INTERVAL"))
    if _env("RT_MQTT_TLS_CAFILE"):
        m["tls_cafile"] = _env("RT_MQTT_TLS_CAFILE")

    # --- log_level -----------------------------------------------------------
    if _env("RT_LOG_LEVEL"):
        cfg["log_level"] = _env("RT_LOG_LEVEL")

    return cfg


def setup_logging(level: str):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S"
    )


def save_home_mode(camera, preset: int):
    """Interactive mode: user positions camera manually, then saves preset."""
    print("\n=== SAVE HOME / START BOX ===")
    print("Use an external VISCA controller or the camera's remote to")
    print("position the camera in the desired start-box position.")
    print(f"\nPress ENTER when ready to store as preset {preset} ...")
    input()
    camera.store_preset(preset)
    print(f"Preset {preset} stored. You can now run the tracker normally.")


def run_tracker(cfg: dict, device_override: str | None):
    from visca import VISCACamera
    from capture import VideoCapture
    from detector import VehicleDetector
    from tracker import PTZTracker
    from api import TrackerAPI

    vcfg  = cfg.get("visca",    {})
    ccfg  = cfg.get("capture",  {})
    dcfg  = cfg.get("detector", {})
    tcfg  = cfg.get("tracker",  {})
    scfg  = cfg.get("stream",   {})
    acfg  = cfg.get("api",      {})
    mcfg  = cfg.get("mqtt",     {})

    log = logging.getLogger("main")

    # ----- Camera -------------------------------------------------------
    camera = VISCACamera(
        port=vcfg.get("port", "/dev/ttyUSB0"),
        baudrate=vcfg.get("baudrate", 9600)
    )
    if not camera.connect():
        log.error("Cannot connect to VISCA camera. Check cable and port.")
        sys.exit(1)

    # ----- Video capture (+ integrierter UDP-Stream per GStreamer tee) --
    device = device_override or ccfg.get("device") or None

    stream_cfg = None
    if scfg.get("enabled", False):
        stream_cfg = {
            "host":         scfg.get("host",         "172.18.5.24"),
            "port":         int(scfg.get("port",      4441)),
            "qp":           int(scfg.get("qp",        23)),
            "preset":       scfg.get("preset",        "ultrafast"),
            "fps":          float(scfg.get("fps",     25.0)),
            "ttl":          int(scfg.get("ttl",       64)),
            "audio_device": scfg.get("audio_device",  "").strip(),
            "audio_bitrate":int(scfg.get("audio_bitrate", 128000)),
        }

    cap = VideoCapture(
        device=device,
        stream_cfg=stream_cfg,
        width=ccfg.get("width"),
        height=ccfg.get("height"),
    )
    if not cap.open():
        log.error("Cannot open video device.")
        camera.disconnect()
        sys.exit(1)

    # Pass actual frame dimensions into tracker config
    tcfg["frame_width"]  = cap.width
    tcfg["frame_height"] = cap.height

    # ----- Detector -----------------------------------------------------
    detector = VehicleDetector(
        model_name=dcfg.get("model", "yolov8n.pt"),
        confidence=dcfg.get("confidence", 0.45),
        iou=dcfg.get("iou", 0.45),
        max_lost_frames=dcfg.get("max_lost_frames", 15),
        device=dcfg.get("device", "cpu"),
        mode=dcfg.get("mode", "vehicles"),
    )
    detector.load()

    # ----- PTZ Tracker --------------------------------------------------
    ptz = PTZTracker(camera=camera, config=tcfg)

    # ----- REST API -----------------------------------------------------
    api = TrackerAPI(camera=camera, tracker=ptz, config=acfg)
    ptz._api = api   # Tracker bekommt Zugriff auf gespeicherte Home-Position
    if acfg.get("enabled", True):
        api.start()

    # ----- MQTT ---------------------------------------------------------
    mqtt_client = None
    if mcfg.get("enabled", False):
        from mqtt import MQTTClient
        mqtt_client = MQTTClient(config=mcfg, api=api, tracker=ptz, camera=camera)
        if not mqtt_client.start():
            log.warning("MQTT-Client konnte nicht gestartet werden – weiter ohne MQTT")

    # ----- Graceful shutdown --------------------------------------------
    running = True
    def _stop(sig, frame):
        nonlocal running
        log.info("Shutdown signal received")
        running = False
    signal.signal(signal.SIGINT,  _stop)
    signal.signal(signal.SIGTERM, _stop)

    # ----- Main loop ----------------------------------------------------
    log.info("Tracker running. Press Ctrl+C to stop.")
    frame_count = 0
    fps_t0 = time.monotonic()

    try:
        while running:
            ok, frame = cap.read()
            if not ok or frame is None or frame.size == 0:
                log.warning("Empty frame received – skipping")
                time.sleep(0.01)
                continue

            if frame.ndim < 2:
                log.warning(f"Malformed frame shape {frame.shape} – skipping")
                time.sleep(0.01)
                continue

            h, w = frame.shape[:2]
            if h == 0 or w == 0:
                log.warning(f"Zero-dimension frame {w}x{h} – skipping")
                time.sleep(0.01)
                continue

            # Detection + PTZ control
            vehicle = detector.process_frame(frame)
            if api.tracking_enabled:
                ptz.update(vehicle)
            api.set_last_target(f"id={vehicle.track_id}" if vehicle else None)

            # MQTT: Detektions-Events und Status publizieren
            if mqtt_client is not None:
                mqtt_client.publish_detection(vehicle)
                mqtt_client.publish_status(
                    state_name=ptz.state.name,
                    target=f"id={vehicle.track_id}" if vehicle else None,
                )

            frame_count += 1
            if frame_count % 100 == 0:
                elapsed = time.monotonic() - fps_t0
                fps = frame_count / elapsed
                state = ptz.state.name
                target = f"id={vehicle.track_id}" if vehicle else "none"
                log.info(f"FPS={fps:.1f}  state={state}  target={target}")

    finally:
        if mqtt_client is not None:
            mqtt_client.stop()
        camera.stop()
        log.info("Stopping camera movement")
        cap.release()
        camera.disconnect()
        log.info("Shutdown complete")


def main():
    parser = argparse.ArgumentParser(
        description="Autonomous PTZ camera tracker for rallye livestreams"
    )
    parser.add_argument(
        "--config", default="config.yaml",
        help="Path to YAML config file (default: config.yaml)"
    )
    parser.add_argument(
        "--save-home", action="store_true",
        help="Interactively store the current camera position as start box"
    )
    parser.add_argument(
        "--device",
        help="Override video capture device (e.g. /dev/video2)"
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    cfg = apply_env_overrides(cfg)
    setup_logging(cfg.get("log_level", "INFO"))

    if args.save_home:
        from visca import VISCACamera
        vcfg = cfg.get("visca", {})
        tcfg = cfg.get("tracker", {})
        cam  = VISCACamera(
            port=vcfg.get("port", "/dev/ttyUSB0"),
            baudrate=vcfg.get("baudrate", 9600)
        )
        if cam.connect():
            save_home_mode(cam, tcfg.get("home_preset", 0))
            cam.disconnect()
        else:
            print("ERROR: Cannot connect to camera.")
            sys.exit(1)
    else:
        run_tracker(cfg, args.device)


if __name__ == "__main__":
    main()
