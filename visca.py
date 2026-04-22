"""
VISCA Serial Camera Control for Tandberg Precision HD PTZ
Protocol: VISCA over RS-232, 9600 baud, 8N1
"""

import serial
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class VISCACamera:
    """Controls a VISCA-compatible PTZ camera via serial port."""

    # VISCA address: camera 1 = 0x01
    CAMERA_ADDR = 0x01

    # Pan/Tilt speed limits
    PAN_SPEED_MIN = 0x01
    PAN_SPEED_MAX = 0x18   # 24 decimal
    TILT_SPEED_MIN = 0x01
    TILT_SPEED_MAX = 0x14  # 20 decimal

    # Pan/Tilt direction bytes
    DIR_STOP  = 0x03
    DIR_UP    = 0x01
    DIR_DOWN  = 0x02
    DIR_LEFT  = 0x01
    DIR_RIGHT = 0x02

    def __init__(self, port: str = "/dev/ttyUSB0", baudrate: int = 9600, timeout: float = 1.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._serial: Optional[serial.Serial] = None

    def connect(self) -> bool:
        """Open serial connection to camera."""
        try:
            self._serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.timeout
            )
            logger.info(f"Connected to VISCA camera on {self.port}")
            # Address set command (broadcast)
            self._send_raw(bytes([0x88, 0x30, 0x01, 0xFF]))
            time.sleep(0.1)
            # Home-Position anfahren
            self.pan_tilt_home()
            return True
        except serial.SerialException as e:
            logger.error(f"Failed to connect to {self.port}: {e}")
            return False

    def disconnect(self):
        """Close serial connection."""
        if self._serial and self._serial.is_open:
            self.stop()
            self._serial.close()
            logger.info("VISCA connection closed")

    def _flush_rx(self):
        """Leert den Empfangspuffer (ACK / Completion Responses der Kamera)."""
        try:
            if self._serial and self._serial.in_waiting:
                self._serial.read(self._serial.in_waiting)
        except serial.SerialException:
            pass  # Disconnect während read — ignorieren, _send_raw erkennt es

    def _reconnect(self) -> bool:
        """Versucht einmalig die Serial-Verbindung neu aufzubauen."""
        logger.warning(f"VISCA: Verbindung verloren — versuche Reconnect auf {self.port}")
        try:
            if self._serial:
                try:
                    self._serial.close()
                except Exception:
                    pass
            self._serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.timeout,
            )
            time.sleep(0.1)
            self._flush_rx()
            logger.info("VISCA: Reconnect erfolgreich")
            return True
        except serial.SerialException as e:
            logger.error(f"VISCA: Reconnect fehlgeschlagen: {e}")
            self._serial = None
            return False

    def _send_raw(self, data: bytes):
        """Send raw bytes to camera. Bei Disconnect einmalig reconnecten."""
        if not self._serial or not self._serial.is_open:
            if not self._reconnect():
                logger.debug("VISCA: Befehl verworfen (kein Serial)")
                return
        try:
            self._flush_rx()
            self._serial.write(data)
            self._serial.flush()
            self._flush_rx()
        except serial.SerialException as e:
            logger.warning(f"VISCA: Sendefehler ({e}) — versuche Reconnect")
            if self._reconnect():
                try:
                    self._serial.write(data)
                    self._serial.flush()
                    self._flush_rx()
                except serial.SerialException as e2:
                    logger.error(f"VISCA: Befehl nach Reconnect fehlgeschlagen: {e2}")

    def _cmd(self, payload: list[int]) -> bytes:
        """Build a VISCA command packet."""
        addr_byte = 0x80 | self.CAMERA_ADDR
        return bytes([addr_byte] + payload + [0xFF])

    # ------------------------------------------------------------------
    # Pan/Tilt commands
    # ------------------------------------------------------------------

    def pan_tilt(self, pan_speed: int, tilt_speed: int, pan_dir: int, tilt_dir: int):
        """
        Send a Pan/Tilt drive command.
        pan_dir:  0x01=left, 0x02=right, 0x03=stop
        tilt_dir: 0x01=up,   0x02=down,  0x03=stop

        Wenn eine Achse DIR_STOP bekommt, wird ihre Speed auf MIN gesetzt
        (VISCA ignoriert den Speed-Wert bei DIR_STOP, aber wir bleiben konform).
        """
        ps = max(self.PAN_SPEED_MIN,  min(pan_speed,  self.PAN_SPEED_MAX))
        ts = max(self.TILT_SPEED_MIN, min(tilt_speed, self.TILT_SPEED_MAX))
        cmd = self._cmd([0x01, 0x06, 0x01, ps, ts, pan_dir, tilt_dir])
        self._send_raw(cmd)

    def stop(self):
        """Stop all pan/tilt movement immediately."""
        cmd = self._cmd([0x01, 0x06, 0x01,
                         self.PAN_SPEED_MIN, self.TILT_SPEED_MIN,
                         self.DIR_STOP, self.DIR_STOP])
        self._send_raw(cmd)

    def pan_tilt_home(self):
        """
        Pan-tilt Home (VISCA 8x 01 06 04 FF).
        Fährt die Kamera zur mechanischen Mittelposition.
        Nicht-blockierend – Caller muss ggf. warten.
        """
        cmd = self._cmd([0x01, 0x06, 0x04])
        self._flush_rx()
        self._serial.write(cmd)
        self._serial.flush()
        logger.info("Pan/Tilt Home gesendet")
        time.sleep(0.05)
        self._flush_rx()

    def pan_tilt_reset(self):
        """
        Pan-tilt Reset / Initialize (VISCA 8x 01 06 05 FF).
        Fährt alle Endanschläge ab zur Kalibrierung des Positionszählers,
        kehrt danach zur Mittelposition zurück (~15 s auf Tandberg PrecisionHD).
        Wird automatisch in connect() aufgerufen.
        """
        self._flush_rx()
        cmd = self._cmd([0x01, 0x06, 0x05])
        self._serial.write(cmd)
        self._serial.flush()
        logger.info("Pan/Tilt Reset (full sweep) gesendet – warte ~15 s …")
        time.sleep(15.0)
        self._flush_rx()
        logger.info("Pan/Tilt Reset abgeschlossen")

    def move(self, pan_speed: int, tilt_speed: int):
        """
        Bewegt die Kamera mit vorzeichenbehafteten Geschwindigkeiten.

          pan_speed:  negativ = links,  positiv = rechts, 0 = Pan stopp
          tilt_speed: negativ = unten,  positiv = oben,   0 = Tilt stopp

        Wenn BEIDE Achsen 0 sind, wird ein vollständiger Stop gesendet.
        Wenn nur eine Achse 0 ist, wird für diese Achse DIR_STOP gesetzt
        und die andere Achse bewegt sich – korrekt nach VISCA-Spec.
        """
        if pan_speed == 0 and tilt_speed == 0:
            self.stop()
            return

        # Pan-Richtung
        if pan_speed > 0:
            pd = self.DIR_RIGHT
            ps = min(pan_speed, self.PAN_SPEED_MAX)
        elif pan_speed < 0:
            pd = self.DIR_LEFT
            ps = min(-pan_speed, self.PAN_SPEED_MAX)
        else:
            pd = self.DIR_STOP
            ps = self.PAN_SPEED_MIN   # ignoriert bei STOP, muss aber > 0 sein

        # Tilt-Richtung
        if tilt_speed > 0:
            td = self.DIR_UP
            ts = min(tilt_speed, self.TILT_SPEED_MAX)
        elif tilt_speed < 0:
            td = self.DIR_DOWN
            ts = min(-tilt_speed, self.TILT_SPEED_MAX)
        else:
            td = self.DIR_STOP
            ts = self.TILT_SPEED_MIN  # ignoriert bei STOP, muss aber > 0 sein

        self.pan_tilt(ps, ts, pd, td)

    # ------------------------------------------------------------------
    # Absolute position (Pan/Tilt goto)
    # ------------------------------------------------------------------

    def goto_position(self, pan_pos: int, tilt_pos: int,
                      pan_speed: int = 10, tilt_speed: int = 10):
        """
        Absolute Pan/Tilt-Position anfahren (VISCA 06 02).
        Werte sind vorzeichenbehaftete 16-Bit-Integer, kodiert als 4 Nibbles.
        Typischer Bereich: Pan −880..+880, Tilt −300..+300.
        """
        def encode(val: int) -> list[int]:
            v = val & 0xFFFF
            return [(v >> 12) & 0x0F,
                    (v >> 8)  & 0x0F,
                    (v >> 4)  & 0x0F,
                    v         & 0x0F]

        ps = max(self.PAN_SPEED_MIN,  min(pan_speed,  self.PAN_SPEED_MAX))
        ts = max(self.TILT_SPEED_MIN, min(tilt_speed, self.TILT_SPEED_MAX))

        payload = [0x01, 0x06, 0x02, ps, ts] + encode(pan_pos) + encode(tilt_pos)
        cmd = self._cmd(payload)
        self._send_raw(cmd)
        logger.debug(f"goto_position pan={pan_pos} tilt={tilt_pos}")

    # ------------------------------------------------------------------
    # Preset recall
    # ------------------------------------------------------------------

    def recall_preset(self, preset: int):
        """Recall a stored preset (0-based index)."""
        cmd = self._cmd([0x01, 0x04, 0x3F, 0x02, preset & 0xFF])
        self._send_raw(cmd)
        logger.info(f"Preset {preset} abgerufen")

    def store_preset(self, preset: int):
        """Store current position as a preset."""
        cmd = self._cmd([0x01, 0x04, 0x3F, 0x01, preset & 0xFF])
        self._send_raw(cmd)
        logger.info(f"Preset {preset} gespeichert")

    # ------------------------------------------------------------------
    # Zoom
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Tandberg-specific
    # ------------------------------------------------------------------

    def call_led(self, on: bool):
        """
        Schaltet die Call-LED (oben auf der Kamera) ein oder aus.
        Call_LED_On:  8x 01 33 01 01 FF
        Call_LED_Off: 8x 01 33 01 00 FF
        Ist beim Kamera-Start immer aus — kein explizites Ausschalten nötig.
        """
        val = 0x01 if on else 0x00
        cmd = self._cmd([0x01, 0x33, 0x01, val])
        self._send_raw(cmd)
        logger.debug(f"Call LED {'an' if on else 'aus'}")

    def zoom_stop(self):
        cmd = self._cmd([0x01, 0x04, 0x07, 0x00])
        self._send_raw(cmd)

    def zoom_in(self, speed: int = 3):
        s = max(0, min(speed, 7))
        cmd = self._cmd([0x01, 0x04, 0x07, 0x20 | s])
        self._send_raw(cmd)

    def zoom_out(self, speed: int = 3):
        s = max(0, min(speed, 7))
        cmd = self._cmd([0x01, 0x04, 0x07, 0x30 | s])
        self._send_raw(cmd)

    # ------------------------------------------------------------------
    # Inquiry
    # ------------------------------------------------------------------

    def inquire_pantilt(self) -> Optional[tuple[int, int]]:
        """Aktuelle Pan/Tilt-Position abfragen. Gibt (pan, tilt) oder None zurück."""
        # Laut VISCA-Doku:
        #   Cmd:  8x 09 06 12 FF
        #   Resp: y0 50 0p 0q 0r 0s 0t 0u 0v 0w FF  (11 Bytes)
        #         pqrs = Pan (vorzeichenbehaftet 16-Bit, als 4 Nibbles)
        #         tuvw = Tilt
        if not self._serial or not self._serial.is_open:
            logger.warning("inquire_pantilt: kein Serial")
            return None
        try:
            # Puffer leeren, dann Inquiry senden
            if self._serial.in_waiting:
                self._serial.read(self._serial.in_waiting)
            cmd = bytes([0x80 | self.CAMERA_ADDR, 0x09, 0x06, 0x12, 0xFF])
            self._serial.write(cmd)
            self._serial.flush()
            # Kamera braucht etwas Zeit – großzügig warten
            time.sleep(0.2)
            waiting = self._serial.in_waiting
            resp = self._serial.read(max(waiting, 11))
            logger.debug(f"inquire_pantilt raw ({len(resp)} B): {resp.hex(' ')}")
            # Antwort-Frame suchen: y0 50 ... FF
            # Suche nach 0x50 als zweitem Byte eines 11-Byte-Blocks
            for i in range(len(resp) - 10):
                if resp[i] == 0x90 and resp[i+1] == 0x50 and resp[i+10] == 0xFF:
                    def decode(nibbles) -> int:
                        v = (nibbles[0] << 12) | (nibbles[1] << 8) | (nibbles[2] << 4) | nibbles[3]
                        if v > 0x7FFF:
                            v -= 0x10000
                        return v
                    pan  = decode(resp[i+2:i+6])
                    tilt = decode(resp[i+6:i+10])
                    logger.debug(f"inquire_pantilt: pan={pan} tilt={tilt}")
                    return pan, tilt
            logger.warning(f"inquire_pantilt: kein gültiger Frame in Antwort: {resp.hex(' ')}")
        except Exception as e:
            logger.warning(f"inquire_pantilt fehlgeschlagen: {e}")
        return None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.disconnect()
