"""
PTZ Tracking Controller

State machine:
  IDLE      → Kamera steht in der gespeicherten Startbox
  TRACKING  → Fahrzeug erkannt, Kamera folgt
  RETURNING → Fahrzeug verschwunden, Kamera kehrt zur Startbox zurück

Steuerungsstrategie: Kontinuierliche Bewegung
  Pro Frame wird die Kamera mit einer neuen Speed gestartet (oder gestoppt).
  Es gibt keine kurzen Pulse mehr — die Kamera läuft kontinuierlich mit der
  berechneten Geschwindigkeit bis zum nächsten Frame-Update.
  Ein EMA-Filter (Exponential Moving Average) glättet den normierten Fehler,
  damit Speed-Sprünge zwischen Frames weich werden.
"""

import time
import math
import logging
import threading
from enum import Enum, auto
from typing import Optional

from visca import VISCACamera
from detector import TrackedVehicle

logger = logging.getLogger(__name__)


class State(Enum):
    IDLE      = auto()
    TRACKING  = auto()
    RETURNING = auto()


class PTZTracker:
    """
    Verarbeitet das aktuelle TrackedVehicle vom Detector und steuert
    die PTZ-Kamera entsprechend.
    """

    def __init__(self, camera: VISCACamera, config: dict):
        self.camera = camera
        self.cfg = config
        self._api = None   # wird von außen gesetzt (TrackerAPI-Referenz)

        # P-Regler-Verstärkung (Fehler normalisiert auf [-1, +1])
        self.pan_gain:  float = config.get("pan_gain",  12.0)
        self.tilt_gain: float = config.get("tilt_gain", 8.0)

        # Adaptiver Gain: Überschwingen wird erkannt und Gain automatisch reduziert.
        # gain_adapt_rate:    wie stark Gain bei Überschwingen reduziert wird (pro Ereignis)
        # gain_recover_rate:  wie schnell Gain sich pro Frame erholt wenn kein Überschwingen
        # gain_min_factor:    untere Grenze als Faktor des konfigurierten Gains (z.B. 0.3 = 30%)
        self._gain_adapt_rate:   float = config.get("gain_adapt_rate",   0.15)
        self._gain_recover_rate: float = config.get("gain_recover_rate", 0.003)
        self._gain_min_factor:   float = config.get("gain_min_factor",   0.3)

        # Laufende Gain-Faktoren (starten bei 1.0 = voller Gain)
        self._pan_gain_factor:  float = 1.0
        self._tilt_gain_factor: float = 1.0

        # Letztes Fehler-Vorzeichen für Überschwing-Erkennung
        self._last_sign_x: float = 0.0
        self._last_sign_y: float = 0.0

        # Totzone: Anteil der halben Bildbreite/-höhe ohne Korrektur
        self.dead_zone: float = config.get("dead_zone", 0.05)

        # Speed-Limits
        self.pan_speed_max:  int = config.get("pan_speed_max",  18)
        self.tilt_speed_max: int = config.get("tilt_speed_max", 12)

        # EMA-Glättung für Fehler: 0.0 = kein Glätzen, 1.0 = einfrieren
        # Typisch 0.4–0.7: neuerer Frame hat mehr Gewicht als alter
        self.ema_alpha: float = config.get("ema_alpha", 0.5)

        # Wartezeit nach Zielverlust bevor Rückkehr gestartet wird
        self.return_delay: float = config.get("return_delay", 1.5)

        # Geschätzte Fahrzeit zur Startbox (für RETURNING-Timeout)
        self.return_travel_time: float = config.get("return_travel_time", 4.0)

        # VISCA-Preset-Index der Startbox (0-basiert)
        self.home_preset: int = config.get("home_preset", 0)

        # Bilddimensionen
        self.frame_w: int = config.get("frame_width",  1920)
        self.frame_h: int = config.get("frame_height", 1080)

        # Minimaler Abstand zwischen VISCA-Kommandos (Sekunden)
        self._cmd_interval: float = config.get("cmd_interval", 0.05)

        self._state: State = State.IDLE
        self._lost_at:    Optional[float] = None
        self._return_at:  Optional[float] = None
        self._last_cmd_time: float = 0.0

        # EMA-geglättete Fehlerwerte (initialisiert bei 0)
        self._smooth_err_x: float = 0.0
        self._smooth_err_y: float = 0.0

        # Letzter gesendeter Speed (für Duplikat-Unterdrückung)
        self._last_pan_speed:  int = 0
        self._last_tilt_speed: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def state(self) -> State:
        return self._state

    def set_frame_size(self, width: int, height: int):
        self.frame_w = width
        self.frame_h = height

    def update(self, vehicle: Optional[TrackedVehicle]):
        """
        Einmal pro Frame aufrufen mit dem aktuell verfolgten Fahrzeug
        (oder None wenn kein Ziel vorhanden).
        """
        now = time.monotonic()

        # ---- IDLE -------------------------------------------------------
        if self._state == State.IDLE:
            if vehicle is not None:
                logger.info("State: IDLE → TRACKING")
                self._state = State.TRACKING
                self._lost_at   = None
                self._return_at = None
                self._reset_smooth()
                self.camera.call_led(True)
                self._track(vehicle, now)
            return

        # ---- TRACKING ---------------------------------------------------
        if self._state == State.TRACKING:
            if vehicle is not None:
                if self._lost_at is not None:
                    # Ziel nach kurzem Verlust wiedergefunden → LED wieder an
                    self.camera.call_led(True)
                self._lost_at = None
                self._track(vehicle, now)
            else:
                # Ziel verschwunden
                if self._lost_at is None:
                    self._lost_at = now
                    self.camera.stop()
                    self.camera.call_led(False)
                    self._reset_smooth()
                    logger.info(f"Ziel verloren – warte {self.return_delay:.1f} s vor Rückkehr")

                if now - self._lost_at >= self.return_delay:
                    logger.info("State: TRACKING → RETURNING")
                    self._state = State.RETURNING
                    self._return_at = now
                    self._go_home()
            return

        # ---- RETURNING --------------------------------------------------
        if self._state == State.RETURNING:
            if vehicle is not None:
                logger.info("State: RETURNING → TRACKING (neues Fahrzeug)")
                self._state = State.TRACKING
                self._lost_at   = None
                self._return_at = None
                self._reset_smooth()
                self.camera.call_led(True)
                self._track(vehicle, now)
                return

            elapsed_return = now - (self._return_at or now)
            if elapsed_return >= self.return_travel_time:
                logger.info("State: RETURNING → IDLE")
                self._state     = State.IDLE
                self._lost_at   = None
                self._return_at = None

    # ------------------------------------------------------------------
    # Interne Hilfsmethoden
    # ------------------------------------------------------------------

    def _reset_smooth(self):
        """EMA-Zustand und adaptiven Gain zurücksetzen (bei Zustandswechsel)."""
        self._smooth_err_x = 0.0
        self._smooth_err_y = 0.0
        self._last_pan_speed  = 0
        self._last_tilt_speed = 0
        self._pan_gain_factor  = 1.0
        self._tilt_gain_factor = 1.0
        self._last_sign_x = 0.0
        self._last_sign_y = 0.0

    def _track(self, vehicle: TrackedVehicle, now: float):
        """
        Berechnet und sendet kontinuierliche Pan/Tilt-Kommandos.

        Strategie: Kontinuierliche Bewegung mit EMA-Glättung.
        - Normierter Fehler wird per EMA geglättet → keine abrupten Speed-Sprünge
        - Quadratische Kennlinie für sanfte Bewegung bei kleinen Fehlern
        - Kamera läuft kontinuierlich, kein Stop-Timer
        - Neuer Frame → neue Speed (oder Stop bei Totzone)
        - Duplikat-Unterdrückung: kein Befehl wenn Speed sich nicht ändert
        """
        if now - self._last_cmd_time < self._cmd_interval:
            return

        cx = self.frame_w / 2.0
        cy = self.frame_h / 2.0

        # Normierter Rohfehler: −1 .. +1
        raw_err_x = (vehicle.center_x - cx) / cx
        raw_err_y = (vehicle.center_y - cy) / cy

        # EMA-Glättung: smooth = alpha * raw + (1 - alpha) * smooth_prev
        # Hoher alpha → schnelle Reaktion; niedriger alpha → mehr Glättung
        a = self.ema_alpha
        self._smooth_err_x = a * raw_err_x + (1.0 - a) * self._smooth_err_x
        self._smooth_err_y = a * raw_err_y + (1.0 - a) * self._smooth_err_y

        err_x = self._smooth_err_x
        err_y = self._smooth_err_y

        # Totzone anwenden
        if abs(err_x) < self.dead_zone:
            err_x = 0.0
        if abs(err_y) < self.dead_zone:
            err_y = 0.0

        if err_x == 0.0 and err_y == 0.0:
            if self._last_pan_speed != 0 or self._last_tilt_speed != 0:
                self.camera.stop()
                self._last_pan_speed  = 0
                self._last_tilt_speed = 0
                self._last_cmd_time = now
                logger.debug("Track: in Totzone → Stop")
            return

        # -- Adaptiver Gain -----------------------------------------------
        # Vorzeichen des aktuellen Fehlers
        sign_x = math.copysign(1.0, err_x) if err_x != 0.0 else 0.0
        sign_y = math.copysign(1.0, err_y) if err_y != 0.0 else 0.0

        # Überschwingen erkannt wenn Vorzeichen wechselt (und vorher bekannt)
        if self._last_sign_x != 0.0 and sign_x != 0.0 and sign_x != self._last_sign_x:
            self._pan_gain_factor = max(
                self._gain_min_factor,
                self._pan_gain_factor * (1.0 - self._gain_adapt_rate)
            )
            logger.debug(f"Pan Überschwingen → pan_gain_factor={self._pan_gain_factor:.3f}")
        else:
            # Kein Überschwingen → langsam erholen
            self._pan_gain_factor = min(1.0, self._pan_gain_factor + self._gain_recover_rate)

        if self._last_sign_y != 0.0 and sign_y != 0.0 and sign_y != self._last_sign_y:
            self._tilt_gain_factor = max(
                self._gain_min_factor,
                self._tilt_gain_factor * (1.0 - self._gain_adapt_rate)
            )
            logger.debug(f"Tilt Überschwingen → tilt_gain_factor={self._tilt_gain_factor:.3f}")
        else:
            self._tilt_gain_factor = min(1.0, self._tilt_gain_factor + self._gain_recover_rate)

        if sign_x != 0.0:
            self._last_sign_x = sign_x
        if sign_y != 0.0:
            self._last_sign_y = sign_y
        # -----------------------------------------------------------------

        # Quadratische Kennlinie mit adaptivem Gain
        eff_pan_gain  = self.pan_gain  * self._pan_gain_factor
        eff_tilt_gain = self.tilt_gain * self._tilt_gain_factor

        pan_speed  = int(round(math.copysign(err_x**2,  err_x) * eff_pan_gain))
        tilt_speed = int(round(math.copysign(err_y**2, -err_y) * eff_tilt_gain))

        # Auf Limits klemmen
        pan_speed  = max(-self.pan_speed_max,  min(self.pan_speed_max,  pan_speed))
        tilt_speed = max(-self.tilt_speed_max, min(self.tilt_speed_max, tilt_speed))

        # Duplikat-Unterdrückung: Speed-Befehl nur senden wenn sich etwas ändert
        if pan_speed == self._last_pan_speed and tilt_speed == self._last_tilt_speed:
            return

        self.camera.move(pan_speed, tilt_speed)
        self._last_pan_speed  = pan_speed
        self._last_tilt_speed = tilt_speed
        self._last_cmd_time = now

        logger.debug(f"Track raw=({raw_err_x:+.2f},{raw_err_y:+.2f}) "
                     f"smooth=({err_x:+.2f},{err_y:+.2f}) "
                     f"gain=({self._pan_gain_factor:.2f},{self._tilt_gain_factor:.2f}) "
                     f"cmd=pan{pan_speed:+d} tilt{tilt_speed:+d}")

    def _go_home(self):
        """Kamera zur Startbox zurückschicken."""
        home_pos = self._api._home_position if self._api is not None else None

        self.camera.stop()
        time.sleep(0.15)

        if home_pos is not None:
            pan, tilt = home_pos
            self.camera.goto_position(pan, tilt)
            logger.info(f"Rückkehr zu gespeicherter Position pan={pan} tilt={tilt}")
        else:
            self.camera.recall_preset(self.home_preset)
            logger.info(f"Rückkehr zu VISCA-Preset {self.home_preset} (kein Software-Preset gespeichert)")
