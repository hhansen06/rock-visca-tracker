"""
REST API für manuelle Kamerasteuerung und Tracking-Kontrolle.

Endpunkte:
  GET  /status                → aktueller State, Tracking-Enabled, letztes Ziel
  POST /tracking/enable       → Tracking aktivieren
  POST /tracking/disable      → Tracking deaktivieren (Kamera stoppt)
  POST /move                  → absolute Position  { pan: -880..880, tilt: -300..300, pan_speed: 1..24, tilt_speed: 1..20 }
  POST /move/relative         → inkrementelle Bewegung { delta_pan: int, delta_tilt: int, pan_speed: 1..24, tilt_speed: 1..20 }
  POST /stop                  → Kamera stoppen
  POST /zoom/in               → Zoom in starten    { speed: 0..7 }
  POST /zoom/out              → Zoom out starten   { speed: 0..7 }
  POST /zoom/stop             → Zoom stoppen
  POST /wb/auto               → White balance Auto
  POST /wb/table/manual       → White balance Table Manual
  POST /wb/table/direct       → White balance Table Direct { index: int }
  POST /ae/auto               → Auto Exposure Auto
  POST /ae/manual             → Auto Exposure Manual
  POST /iris/direct           → Iris Direct { position: 0..50 }
  POST /gain/direct           → Gain Direct { position: int }
  POST /backlight/on|off      → Backlight Compensation an/aus
  POST /mirror/on|off         → Mirror (LR Reverse) an/aus
  POST /flip/on|off           → Flip an/aus
  POST /gamma/auto|manual     → Gamma-Modus Auto/Manual
  POST /gamma/direct          → Gamma Table Direct { table: 0..7 }
  POST /zoom/direct           → Zoom Direct { position: int }
  POST /zoomfocus/direct      → Zoom+Focus Direct { zoom: int, focus: int }
  POST /focus/stop            → Focus Stop
  POST /focus/far|near        → Focus motorisch fahren { speed: 0..7 }
  POST /focus/direct          → Focus Direct { position: int }
  POST /focus/auto|manual     → Fokusmodus Auto/Manual
  POST /pt/stop               → PT Stop
  POST /pt/reset              → PT Reset
  POST /pt/up|down|left|right|up-left|up-right|down-left|down-right → PT Jog { pan_speed:1..24, tilt_speed:1..20 }
  POST /pt/direct             → PT Direct { pan:int, tilt:int, pan_speed:1..24, tilt_speed:1..20 }
  POST /ptzf/direct           → PTZF Direct { pan:int, tilt:int, zoom:int, focus:int }
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

        @app.post("/move/relative")
        def move_relative():
            data = request.get_json(silent=True) or {}
            delta_pan  = int(data.get("delta_pan",  data.get("pan", 0)))
            delta_tilt = int(data.get("delta_tilt", data.get("tilt", 0)))
            pan_speed  = int(data.get("pan_speed", 10))
            tilt_speed = int(data.get("tilt_speed", 10))
            pan_speed  = max(1, min(24, pan_speed))
            tilt_speed = max(1, min(20, tilt_speed))

            pos = self.camera.inquire_pantilt()
            if pos is None:
                logger.warning("API: move/relative fehlgeschlagen – inquire_pantilt lieferte None")
                return jsonify({"ok": False, "error": "position query failed"}), 500

            current_pan, current_tilt = pos
            target_pan  = max(-880, min(880, current_pan + delta_pan))
            target_tilt = max(-300, min(300, current_tilt + delta_tilt))

            self.camera.goto_position(target_pan, target_tilt, pan_speed, tilt_speed)
            logger.info(
                f"API: move/relative dpan={delta_pan:+d} dtilt={delta_tilt:+d} "
                f"from=({current_pan:+d},{current_tilt:+d}) to=({target_pan:+d},{target_tilt:+d}) "
                f"speed=({pan_speed},{tilt_speed})"
            )
            return jsonify({
                "ok": True,
                "delta_pan": delta_pan,
                "delta_tilt": delta_tilt,
                "from_pan": current_pan,
                "from_tilt": current_tilt,
                "pan": target_pan,
                "tilt": target_tilt,
                "pan_speed": pan_speed,
                "tilt_speed": tilt_speed,
            })

        @app.post("/stop")
        def stop():
            self.camera.stop()
            logger.info("API: stop")
            return jsonify({"ok": True})

        @app.post("/zoom/in")
        def zoom_in():
            data = request.get_json(silent=True) or {}
            speed = int(data.get("speed", 3))
            speed = max(0, min(7, speed))
            self.camera.zoom_in(speed)
            logger.info(f"API: zoom in speed={speed}")
            return jsonify({"zoom": "in", "speed": speed})

        @app.post("/zoom/out")
        def zoom_out():
            data = request.get_json(silent=True) or {}
            speed = int(data.get("speed", 3))
            speed = max(0, min(7, speed))
            self.camera.zoom_out(speed)
            logger.info(f"API: zoom out speed={speed}")
            return jsonify({"zoom": "out", "speed": speed})

        @app.post("/zoom/stop")
        def zoom_stop():
            self.camera.zoom_stop()
            logger.info("API: zoom stop")
            return jsonify({"zoom": "stop"})

        @app.post("/wb/auto")
        def wb_auto():
            self.camera.wb_auto()
            logger.info("API: wb auto")
            return jsonify({"wb_mode": "auto"})

        @app.post("/wb/table/manual")
        def wb_table_manual():
            self.camera.wb_table_manual()
            logger.info("API: wb table manual")
            return jsonify({"wb_mode": "table_manual"})

        @app.post("/wb/table/direct")
        def wb_table_direct():
            data = request.get_json(silent=True) or {}
            index = int(data.get("index", data.get("table", 0)))
            index = max(0, min(65535, index))
            self.camera.wb_table_direct(index)
            logger.info(f"API: wb table direct index={index}")
            return jsonify({"wb_mode": "table_direct", "index": index})

        @app.post("/ae/auto")
        def ae_auto():
            self.camera.ae_auto()
            logger.info("API: ae auto")
            return jsonify({"ae_mode": "auto"})

        @app.post("/ae/manual")
        def ae_manual():
            self.camera.ae_manual()
            logger.info("API: ae manual")
            return jsonify({"ae_mode": "manual"})

        @app.post("/iris/direct")
        def iris_direct():
            data = request.get_json(silent=True) or {}
            position = int(data.get("position", data.get("iris", 0)))
            position = max(0, min(50, position))
            self.camera.iris_direct(position)
            logger.info(f"API: iris direct position={position}")
            return jsonify({"iris": position})

        @app.post("/gain/direct")
        def gain_direct():
            data = request.get_json(silent=True) or {}
            position = int(data.get("position", data.get("gain", 0)))
            position = max(0, min(65535, position))
            self.camera.gain_direct(position)
            logger.info(f"API: gain direct position={position}")
            return jsonify({"gain": position})

        @app.post("/backlight/on")
        def backlight_on():
            self.camera.backlight_on()
            logger.info("API: backlight on")
            return jsonify({"backlight": True})

        @app.post("/backlight/off")
        def backlight_off():
            self.camera.backlight_off()
            logger.info("API: backlight off")
            return jsonify({"backlight": False})

        @app.post("/mirror/on")
        def mirror_on():
            self.camera.mirror_on()
            logger.info("API: mirror on")
            return jsonify({"mirror": True})

        @app.post("/mirror/off")
        def mirror_off():
            self.camera.mirror_off()
            logger.info("API: mirror off")
            return jsonify({"mirror": False})

        @app.post("/flip/on")
        def flip_on():
            self.camera.flip_on()
            logger.info("API: flip on")
            return jsonify({"flip": True})

        @app.post("/flip/off")
        def flip_off():
            self.camera.flip_off()
            logger.info("API: flip off")
            return jsonify({"flip": False})

        @app.post("/gamma/auto")
        def gamma_auto():
            self.camera.gamma_auto()
            logger.info("API: gamma auto")
            return jsonify({"gamma_mode": "auto"})

        @app.post("/gamma/manual")
        def gamma_manual():
            self.camera.gamma_manual()
            logger.info("API: gamma manual")
            return jsonify({"gamma_mode": "manual"})

        @app.post("/gamma/direct")
        def gamma_direct():
            data = request.get_json(silent=True) or {}
            table = int(data.get("table", data.get("index", 0)))
            table = max(0, min(7, table))
            self.camera.gamma_direct(table)
            logger.info(f"API: gamma direct table={table}")
            return jsonify({"gamma_table": table})

        @app.post("/zoom/direct")
        def zoom_direct():
            data = request.get_json(silent=True) or {}
            position = int(data.get("position", data.get("zoom", 0)))
            position = max(0, min(65535, position))
            self.camera.zoom_direct(position)
            logger.info(f"API: zoom direct position={position}")
            return jsonify({"zoom": position})

        @app.post("/zoomfocus/direct")
        def zoomfocus_direct():
            data = request.get_json(silent=True) or {}
            zoom = int(data.get("zoom", data.get("zoom_position", 0)))
            focus = int(data.get("focus", data.get("focus_position", 0)))
            zoom = max(0, min(65535, zoom))
            focus = max(0, min(65535, focus))
            self.camera.zoom_focus_direct(zoom, focus)
            logger.info(f"API: zoomfocus direct zoom={zoom} focus={focus}")
            return jsonify({"zoom": zoom, "focus": focus})

        @app.post("/focus/stop")
        def focus_stop():
            self.camera.focus_stop()
            logger.info("API: focus stop")
            return jsonify({"focus": "stop"})

        @app.post("/focus/far")
        def focus_far():
            data = request.get_json(silent=True) or {}
            speed = int(data.get("speed", 3))
            speed = max(0, min(7, speed))
            self.camera.focus_far(speed)
            logger.info(f"API: focus far speed={speed}")
            return jsonify({"focus": "far", "speed": speed})

        @app.post("/focus/near")
        def focus_near():
            data = request.get_json(silent=True) or {}
            speed = int(data.get("speed", 3))
            speed = max(0, min(7, speed))
            self.camera.focus_near(speed)
            logger.info(f"API: focus near speed={speed}")
            return jsonify({"focus": "near", "speed": speed})

        @app.post("/focus/direct")
        def focus_direct():
            data = request.get_json(silent=True) or {}
            position = int(data.get("position", data.get("focus", 0)))
            position = max(0, min(65535, position))
            self.camera.focus_direct(position)
            logger.info(f"API: focus direct position={position}")
            return jsonify({"focus": position})

        @app.post("/focus/auto")
        def focus_auto():
            self.camera.focus_auto()
            logger.info("API: focus auto")
            return jsonify({"focus_mode": "auto"})

        @app.post("/focus/manual")
        def focus_manual():
            self.camera.focus_manual()
            logger.info("API: focus manual")
            return jsonify({"focus_mode": "manual"})

        @app.post("/pt/stop")
        def pt_stop():
            self.camera.pt_stop()
            logger.info("API: pt stop")
            return jsonify({"pt": "stop"})

        @app.post("/pt/reset")
        def pt_reset():
            self.camera.pt_reset()
            logger.info("API: pt reset")
            return jsonify({"pt": "reset"})

        @app.post("/pt/up")
        def pt_up():
            data = request.get_json(silent=True) or {}
            pan_speed = max(1, min(24, int(data.get("pan_speed", 3))))
            tilt_speed = max(1, min(20, int(data.get("tilt_speed", 3))))
            self.camera.pt_up(pan_speed, tilt_speed)
            return jsonify({"pt": "up", "pan_speed": pan_speed, "tilt_speed": tilt_speed})

        @app.post("/pt/down")
        def pt_down():
            data = request.get_json(silent=True) or {}
            pan_speed = max(1, min(24, int(data.get("pan_speed", 3))))
            tilt_speed = max(1, min(20, int(data.get("tilt_speed", 3))))
            self.camera.pt_down(pan_speed, tilt_speed)
            return jsonify({"pt": "down", "pan_speed": pan_speed, "tilt_speed": tilt_speed})

        @app.post("/pt/left")
        def pt_left():
            data = request.get_json(silent=True) or {}
            pan_speed = max(1, min(24, int(data.get("pan_speed", 3))))
            tilt_speed = max(1, min(20, int(data.get("tilt_speed", 3))))
            self.camera.pt_left(pan_speed, tilt_speed)
            return jsonify({"pt": "left", "pan_speed": pan_speed, "tilt_speed": tilt_speed})

        @app.post("/pt/right")
        def pt_right():
            data = request.get_json(silent=True) or {}
            pan_speed = max(1, min(24, int(data.get("pan_speed", 3))))
            tilt_speed = max(1, min(20, int(data.get("tilt_speed", 3))))
            self.camera.pt_right(pan_speed, tilt_speed)
            return jsonify({"pt": "right", "pan_speed": pan_speed, "tilt_speed": tilt_speed})

        @app.post("/pt/up-left")
        def pt_up_left():
            data = request.get_json(silent=True) or {}
            pan_speed = max(1, min(24, int(data.get("pan_speed", 3))))
            tilt_speed = max(1, min(20, int(data.get("tilt_speed", 3))))
            self.camera.pt_up_left(pan_speed, tilt_speed)
            return jsonify({"pt": "up-left", "pan_speed": pan_speed, "tilt_speed": tilt_speed})

        @app.post("/pt/up-right")
        def pt_up_right():
            data = request.get_json(silent=True) or {}
            pan_speed = max(1, min(24, int(data.get("pan_speed", 3))))
            tilt_speed = max(1, min(20, int(data.get("tilt_speed", 3))))
            self.camera.pt_up_right(pan_speed, tilt_speed)
            return jsonify({"pt": "up-right", "pan_speed": pan_speed, "tilt_speed": tilt_speed})

        @app.post("/pt/down-left")
        def pt_down_left():
            data = request.get_json(silent=True) or {}
            pan_speed = max(1, min(24, int(data.get("pan_speed", 3))))
            tilt_speed = max(1, min(20, int(data.get("tilt_speed", 3))))
            self.camera.pt_down_left(pan_speed, tilt_speed)
            return jsonify({"pt": "down-left", "pan_speed": pan_speed, "tilt_speed": tilt_speed})

        @app.post("/pt/down-right")
        def pt_down_right():
            data = request.get_json(silent=True) or {}
            pan_speed = max(1, min(24, int(data.get("pan_speed", 3))))
            tilt_speed = max(1, min(20, int(data.get("tilt_speed", 3))))
            self.camera.pt_down_right(pan_speed, tilt_speed)
            return jsonify({"pt": "down-right", "pan_speed": pan_speed, "tilt_speed": tilt_speed})

        @app.post("/pt/direct")
        def pt_direct():
            data = request.get_json(silent=True) or {}
            pan = max(-880, min(880, int(data.get("pan", 0))))
            tilt = max(-300, min(300, int(data.get("tilt", 0))))
            pan_speed = max(1, min(24, int(data.get("pan_speed", 10))))
            tilt_speed = max(1, min(20, int(data.get("tilt_speed", 10))))
            self.camera.pt_direct(pan, tilt, pan_speed, tilt_speed)
            return jsonify({"pan": pan, "tilt": tilt, "pan_speed": pan_speed, "tilt_speed": tilt_speed})

        @app.post("/ptzf/direct")
        def ptzf_direct():
            data = request.get_json(silent=True) or {}
            pan = max(-880, min(880, int(data.get("pan", 0))))
            tilt = max(-300, min(300, int(data.get("tilt", 0))))
            zoom = max(0, min(65535, int(data.get("zoom", 0))))
            focus = max(0, min(65535, int(data.get("focus", 0))))
            self.camera.ptzf_direct(pan, tilt, zoom, focus)
            return jsonify({"pan": pan, "tilt": tilt, "zoom": zoom, "focus": focus})

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
