"""
MQTT-Client für den rally_tracker.

Publiziert:
  <prefix>/status          – Tracker-Zustand, Tracking-Enabled, letztes Ziel (periodisch + bei Änderung)
  <prefix>/detection       – Detektions-Events (Bounding-Box, Konfidenz, Klasse)
  <prefix>/camera/position – Aktuelle Pan/Tilt-Position (bei jedem PTZ-Update)

Abonniert (Steuerung):
  <prefix>/cmd/tracking    – Payload "enable" / "disable"
  <prefix>/cmd/move        – JSON { "pan": int, "tilt": int, "pan_speed": int, "tilt_speed": int }
  <prefix>/cmd/stop        – beliebiger Payload → Kamera stopp
  <prefix>/cmd/zoom/in     – JSON optional { "speed": 0..7 }
  <prefix>/cmd/zoom/out    – JSON optional { "speed": 0..7 }
  <prefix>/cmd/zoom/stop   – beliebiger Payload → Zoom stoppen
  <prefix>/cmd/preset/save   – beliebiger Payload → Home-Position speichern
  <prefix>/cmd/preset/recall – beliebiger Payload → Home-Position anfahren

Alle eingehenden Befehle werden über denselben Mechanismus wie die REST-API
ausgeführt, d.h. über die TrackerAPI-Instanz.
"""

import json
import logging
import threading
import time
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import paho.mqtt.client as mqtt_client
    from paho.mqtt.enums import CallbackAPIVersion
    _PAHO_AVAILABLE = True
except ImportError:
    _PAHO_AVAILABLE = False
    logger.warning("paho-mqtt nicht installiert – MQTT deaktiviert. "
                   "Installation: pip install paho-mqtt>=2.0")


class MQTTClient:
    """
    Thin MQTT-Wrapper, der sich an TrackerAPI und PTZTracker hängt.

    Nutzung in main.py:
        mqtt = MQTTClient(config=cfg["mqtt"], api=api, tracker=ptz, camera=camera)
        mqtt.start()
        # Im Frame-Loop:
        mqtt.publish_detection(vehicle)   # optional, bei jeder Detektion
        mqtt.publish_status(state, target)
        # Am Ende:
        mqtt.stop()
    """

    def __init__(self, config: dict, api, tracker, camera):
        self._cfg     = config
        self._api     = api      # TrackerAPI-Instanz
        self._tracker = tracker  # PTZTracker-Instanz
        self._camera  = camera   # VISCACamera-Instanz

        self._prefix  = config.get("topic_prefix", "rally_tracker").rstrip("/")
        self._qos     = int(config.get("qos", 1))
        self._status_interval = float(config.get("status_interval", 5.0))

        self._client: Optional["mqtt_client.Client"] = None
        self._connected   = False
        self._lock        = threading.Lock()
        self._stop_event  = threading.Event()
        self._status_thread: Optional[threading.Thread] = None

        # Zustand für Change-Detection beim Status-Topic
        self._last_status: dict = {}

    # ------------------------------------------------------------------
    # Öffentliche API
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """Verbindung herstellen, Subscriptions einrichten, Status-Thread starten."""
        if not _PAHO_AVAILABLE:
            logger.error("paho-mqtt nicht verfügbar – MQTT-Client startet nicht.")
            return False

        host      = self._cfg.get("host", "127.0.0.1")
        port      = int(self._cfg.get("port", 1883))
        client_id = self._cfg.get("client_id", "").strip() or f"rally_tracker_{uuid.uuid4().hex[:8]}"

        self._client = mqtt_client.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=mqtt_client.MQTTv5,
        )

        # Authentifizierung
        username = self._cfg.get("username", "").strip()
        password = self._cfg.get("password", "").strip()
        if username:
            self._client.username_pw_set(username, password or None)

        # TLS (optional)
        cafile = self._cfg.get("tls_cafile", "").strip()
        if cafile:
            self._client.tls_set(ca_certs=cafile)

        # Last-Will: offline-Status veröffentlichen wenn Verbindung abbricht
        will_payload = json.dumps({"online": False})
        self._client.will_set(
            topic=f"{self._prefix}/online",
            payload=will_payload,
            qos=self._qos,
            retain=True,
        )

        # Callbacks
        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message    = self._on_message

        try:
            self._client.connect(host, port, keepalive=60)
        except Exception as exc:
            logger.error(f"MQTT Verbindung zu {host}:{port} fehlgeschlagen: {exc}")
            return False

        self._client.loop_start()

        # Periodischer Status-Thread
        self._stop_event.clear()
        self._status_thread = threading.Thread(
            target=self._status_loop,
            daemon=True,
            name="mqtt-status",
        )
        self._status_thread.start()

        logger.info(f"MQTT-Client gestartet → {host}:{port}  prefix={self._prefix}")
        return True

    def stop(self):
        """Verbindung sauber trennen."""
        self._stop_event.set()
        if self._client:
            # Online-Status zurückziehen
            self._publish(f"{self._prefix}/online", json.dumps({"online": False}), retain=True)
            self._client.loop_stop()
            self._client.disconnect()
        logger.info("MQTT-Client gestoppt")

    # ------------------------------------------------------------------
    # Publish-Hilfsmethoden (vom Haupt-Loop aufgerufen)
    # ------------------------------------------------------------------

    def publish_detection(self, vehicle) -> None:
        """
        Veröffentlicht ein Detektions-Event.
        vehicle ist ein TrackedVehicle oder None (= Ziel verloren).
        """
        if vehicle is None:
            payload = {"detected": False}
        else:
            payload = {
                "detected":   True,
                "track_id":   int(vehicle.track_id),
                "class_name": str(vehicle.class_name) if hasattr(vehicle, "class_name") else "",
                "confidence": round(float(vehicle.confidence), 3) if hasattr(vehicle, "confidence") else None,
                "bbox": {
                    "x1": int(vehicle.x1),
                    "y1": int(vehicle.y1),
                    "x2": int(vehicle.x2),
                    "y2": int(vehicle.y2),
                } if all(hasattr(vehicle, a) for a in ("x1", "y1", "x2", "y2")) else None,
                "center": {
                    "cx": int(vehicle.cx) if hasattr(vehicle, "cx") else None,
                    "cy": int(vehicle.cy) if hasattr(vehicle, "cy") else None,
                },
            }
        self._publish(f"{self._prefix}/detection", json.dumps(payload))

    def publish_status(self, state_name: str, target: Optional[str]) -> None:
        """
        Veröffentlicht den Tracker-Zustand.
        Wird immer veröffentlicht wenn sich etwas geändert hat; zusätzlich
        periodisch durch den Status-Thread.
        """
        status = {
            "state":            state_name,
            "tracking_enabled": self._api.tracking_enabled,
            "target":           target,
        }
        changed = status != self._last_status
        if changed:
            self._last_status = status.copy()
            self._publish(f"{self._prefix}/status", json.dumps(status), retain=True)

    def publish_camera_position(self, pan: int, tilt: int) -> None:
        """Veröffentlicht die aktuelle Pan/Tilt-Position."""
        payload = {"pan": pan, "tilt": tilt}
        self._publish(f"{self._prefix}/camera/position", json.dumps(payload))

    # ------------------------------------------------------------------
    # Interner Publish-Wrapper
    # ------------------------------------------------------------------

    def _publish(self, topic: str, payload: str, retain: bool = False) -> None:
        with self._lock:
            if not self._connected or self._client is None:
                return
            try:
                self._client.publish(topic, payload, qos=self._qos, retain=retain)
            except Exception as exc:
                logger.warning(f"MQTT publish fehlgeschlagen ({topic}): {exc}")

    # ------------------------------------------------------------------
    # Periodischer Status-Thread
    # ------------------------------------------------------------------

    def _status_loop(self):
        while not self._stop_event.wait(self._status_interval):
            try:
                state  = self._tracker.state.name
                target = self._api._last_target
                status = {
                    "state":            state,
                    "tracking_enabled": self._api.tracking_enabled,
                    "target":           target,
                }
                # Immer publizieren (Heartbeat), auch ohne Änderung
                self._last_status = status.copy()
                self._publish(f"{self._prefix}/status", json.dumps(status), retain=True)
            except Exception as exc:
                logger.debug(f"MQTT status_loop Fehler: {exc}")

    # ------------------------------------------------------------------
    # MQTT-Callbacks
    # ------------------------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            with self._lock:
                self._connected = True
            logger.info("MQTT verbunden")
            # Online-Status
            self._publish(f"{self._prefix}/online", json.dumps({"online": True}), retain=True)
            # Subscriptions
            cmd_topics = [
                f"{self._prefix}/cmd/tracking",
                f"{self._prefix}/cmd/move",
                f"{self._prefix}/cmd/stop",
                f"{self._prefix}/cmd/zoom/in",
                f"{self._prefix}/cmd/zoom/out",
                f"{self._prefix}/cmd/zoom/stop",
                f"{self._prefix}/cmd/preset/save",
                f"{self._prefix}/cmd/preset/recall",
            ]
            for topic in cmd_topics:
                client.subscribe(topic, qos=self._qos)
                logger.debug(f"MQTT subscribed: {topic}")
        else:
            logger.error(f"MQTT Verbindung abgelehnt: reason_code={reason_code}")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        with self._lock:
            self._connected = False
        if reason_code != 0:
            logger.warning(f"MQTT unerwartet getrennt (reason_code={reason_code}) – paho reconnect läuft")
        else:
            logger.info("MQTT Verbindung getrennt")

    def _on_message(self, client, userdata, msg):
        topic   = msg.topic
        payload = msg.payload.decode("utf-8", errors="replace").strip()
        logger.debug(f"MQTT cmd: {topic} → {payload!r}")

        try:
            suffix = topic[len(self._prefix) + 1:]   # alles nach "<prefix>/"

            if suffix == "cmd/tracking":
                self._handle_tracking(payload)

            elif suffix == "cmd/move":
                self._handle_move(payload)

            elif suffix == "cmd/stop":
                self._handle_stop()

            elif suffix == "cmd/zoom/in":
                self._handle_zoom_in(payload)

            elif suffix == "cmd/zoom/out":
                self._handle_zoom_out(payload)

            elif suffix == "cmd/zoom/stop":
                self._handle_zoom_stop()

            elif suffix == "cmd/preset/save":
                self._handle_preset_save()

            elif suffix == "cmd/preset/recall":
                self._handle_preset_recall()

            else:
                logger.debug(f"MQTT unbekanntes Command-Topic: {topic}")

        except Exception as exc:
            logger.error(f"MQTT Fehler bei Verarbeitung von {topic}: {exc}")

    # ------------------------------------------------------------------
    # Command-Handler
    # ------------------------------------------------------------------

    def _handle_tracking(self, payload: str):
        if payload.lower() == "enable":
            self._api.tracking_enabled = True
            logger.info("MQTT: Tracking aktiviert")
            self._publish(f"{self._prefix}/status/tracking", json.dumps({"tracking_enabled": True}))
        elif payload.lower() == "disable":
            self._api.tracking_enabled = False
            self._camera.stop()
            self._camera.call_led(False)
            logger.info("MQTT: Tracking deaktiviert")
            self._publish(f"{self._prefix}/status/tracking", json.dumps({"tracking_enabled": False}))
        else:
            logger.warning(f"MQTT cmd/tracking: unbekannter Payload {payload!r} (erwartet: enable/disable)")

    def _handle_move(self, payload: str):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            logger.warning(f"MQTT cmd/move: kein gültiges JSON: {payload!r}")
            return

        pan        = max(-880, min(880,  int(data.get("pan",        0))))
        tilt       = max(-300, min(300,  int(data.get("tilt",       0))))
        pan_speed  = max(1,    min(24,   int(data.get("pan_speed",  10))))
        tilt_speed = max(1,    min(20,   int(data.get("tilt_speed", 10))))

        self._camera.goto_position(pan, tilt, pan_speed, tilt_speed)
        logger.info(f"MQTT: move pan={pan:+d} tilt={tilt:+d} speed=({pan_speed},{tilt_speed})")

    def _handle_stop(self):
        self._camera.stop()
        logger.info("MQTT: stop")

    def _parse_zoom_speed(self, payload: str, default: int = 3) -> int:
        speed = default
        if payload:
            try:
                data = json.loads(payload)
                if isinstance(data, dict) and "speed" in data:
                    speed = int(data.get("speed", default))
                elif isinstance(data, int):
                    speed = int(data)
            except json.JSONDecodeError:
                # Optional: reine Zahl als Payload erlauben
                try:
                    speed = int(payload)
                except ValueError:
                    pass
        return max(0, min(7, speed))

    def _handle_zoom_in(self, payload: str):
        speed = self._parse_zoom_speed(payload, default=3)
        self._camera.zoom_in(speed)
        logger.info(f"MQTT: zoom in speed={speed}")

    def _handle_zoom_out(self, payload: str):
        speed = self._parse_zoom_speed(payload, default=3)
        self._camera.zoom_out(speed)
        logger.info(f"MQTT: zoom out speed={speed}")

    def _handle_zoom_stop(self):
        self._camera.zoom_stop()
        logger.info("MQTT: zoom stop")

    def _handle_preset_save(self):
        pos = self._camera.inquire_pantilt()
        if pos is None:
            logger.warning("MQTT: Preset speichern fehlgeschlagen – inquire_pantilt lieferte None")
            return
        pan, tilt = pos
        self._api._home_position = (pan, tilt)
        logger.info(f"MQTT: Home-Position gespeichert pan={pan} tilt={tilt}")

    def _handle_preset_recall(self):
        if self._api._home_position is None:
            logger.warning("MQTT: Preset recall – keine Position gespeichert")
            return
        pan, tilt = self._api._home_position
        self._camera.stop()
        self._camera.goto_position(pan, tilt)
        logger.info(f"MQTT: Home-Position angefahren pan={pan} tilt={tilt}")
