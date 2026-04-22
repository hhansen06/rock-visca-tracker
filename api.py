"""
REST API für manuelle Kamerasteuerung und Tracking-Kontrolle.

Endpunkte:
  GET  /status                → aktueller State, Tracking-Enabled, letztes Ziel
  POST /tracking/enable       → Tracking aktivieren
  POST /tracking/disable      → Tracking deaktivieren (Kamera stoppt)
  POST /move                  → absolute Position  { pan: -880..880, tilt: -300..300, pan_speed: 1..24, tilt_speed: 1..20 }
  POST /stop                  → Kamera stoppen
  POST /preset/save           → aktuelle Position als Preset 0 speichern
  POST /preset/recall         → zu Preset 0 zurückfahren
"""

import logging
import threading
from typing import Optional

from flask import Flask, jsonify, request

logger = logging.getLogger(__name__)


class TrackerAPI:
    """
    Startet einen Flask-Webserver in einem Daemon-Thread.
    Erhält Referenzen auf camera, tracker und einen "tracking_enabled"-Flag
    der vom Haupt-Loop ausgewertet wird.
    """

    def __init__(self, camera, tracker, config: dict):
        self.camera  = camera
        self.tracker = tracker
        self.port    = int(config.get("port", 8080))
        self.host    = config.get("host", "0.0.0.0")

        self.tracking_enabled: bool = True
        self._last_target: Optional[str] = None
        self._home_position: Optional[tuple[int, int]] = None
        self._lock = threading.Lock()

        self._app = Flask(__name__)
        self._app.logger.setLevel(logging.WARNING)
        logging.getLogger("werkzeug").setLevel(logging.WARNING)
        self._register_routes()

    # ------------------------------------------------------------------
    # Öffentliche Helfer (vom Haupt-Loop aufgerufen)
    # ------------------------------------------------------------------

    def set_last_target(self, target: Optional[str]):
        with self._lock:
            self._last_target = target

    def start(self):
        t = threading.Thread(
            target=self._app.run,
            kwargs={"host": self.host, "port": self.port, "use_reloader": False},
            daemon=True,
            name="rest-api",
        )
        t.start()
        logger.info(f"REST API gestartet auf http://{self.host}:{self.port}")

    # ------------------------------------------------------------------
    # Routen
    # ------------------------------------------------------------------

    def _register_routes(self):
        app = self._app

        @app.get("/status")
        def status():
            with self._lock:
                target = self._last_target
            return jsonify({
                "state":            self.tracker.state.name,
                "tracking_enabled": self.tracking_enabled,
                "target":           target,
            })

        @app.post("/tracking/enable")
        def tracking_enable():
            self.tracking_enabled = True
            logger.info("API: Tracking aktiviert")
            return jsonify({"tracking_enabled": True})

        @app.post("/tracking/disable")
        def tracking_disable():
            self.tracking_enabled = False
            self.camera.stop()
            self.camera.call_led(False)
            logger.info("API: Tracking deaktiviert")
            return jsonify({"tracking_enabled": False})

        @app.post("/move")
        def move():
            data = request.get_json(silent=True) or {}
            pan       = int(data.get("pan",       0))
            tilt      = int(data.get("tilt",      0))
            pan_speed = int(data.get("pan_speed", 10))
            tilt_speed = int(data.get("tilt_speed", 10))
            # Limits einhalten (Pan ±880, Tilt ±300 typisch für Tandberg PrecisionHD)
            pan  = max(-880, min(880, pan))
            tilt = max(-300, min(300, tilt))
            pan_speed  = max(1, min(24, pan_speed))
            tilt_speed = max(1, min(20, tilt_speed))
            self.camera.goto_position(pan, tilt, pan_speed, tilt_speed)
            logger.info(f"API: move pan={pan:+d} tilt={tilt:+d} speed=({pan_speed},{tilt_speed})")
            return jsonify({"pan": pan, "tilt": tilt, "pan_speed": pan_speed, "tilt_speed": tilt_speed})

        @app.post("/stop")
        def stop():
            self.camera.stop()
            logger.info("API: stop")
            return jsonify({"ok": True})

        @app.post("/preset/save")
        def preset_save():
            pos = self.camera.inquire_pantilt()
            if pos is None:
                logger.warning("API: Preset speichern fehlgeschlagen – inquire_pantilt lieferte None")
                return jsonify({"saved": False, "error": "position query failed"}), 500
            pan, tilt = pos
            self._home_position = (pan, tilt)
            logger.info(f"API: Home-Position gespeichert pan={pan} tilt={tilt}")
            return jsonify({"saved": True, "pan": pan, "tilt": tilt})

        @app.post("/preset/recall")
        def preset_recall():
            if self._home_position is None:
                logger.warning("API: Preset recall – keine Position gespeichert")
                return jsonify({"recalled": False, "error": "no preset saved"}), 404
            pan, tilt = self._home_position
            self.camera.stop()
            self.camera.goto_position(pan, tilt)
            logger.info(f"API: Home-Position angefahren pan={pan} tilt={tilt}")
            return jsonify({"recalled": True, "pan": pan, "tilt": tilt})
