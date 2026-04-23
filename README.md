# rally_tracker – Installationsanleitung

## Voraussetzungen

- Raspberry Pi (getestet mit Pi 4 / Pi 5, Raspberry Pi OS Bookworm 64-bit)
- Python 3.11+
- Onboard HDMI-RX (z.B. Orange Pi 5 / RK3588) **oder** USB-Capture-Card
- Tandberg Precision HD PTZ, per USB-Seriell-Adapter an `/dev/ttyUSB0`

---

## 1. Systemabhängigkeiten

```bash
sudo apt update
sudo apt install -y python3-pip python3-venv v4l-utils libopencv-dev
```

---

## 2. Python-Umgebung

```bash
cd ~/stream
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Der erste Start lädt das YOLOv8n-Modell (~6 MB) automatisch herunter.

---

## 3. Seriellen Port freischalten

```bash
sudo usermod -aG dialout $USER
# danach einmal neu anmelden (oder: newgrp dialout)
```

---

## 4. Startbox speichern

Kamera manuell (Fernbedienung o. ä.) in die gewünschte Startposition fahren,
dann:

```bash
python main.py --save-home
# → Meldung abwarten, Enter drücken
```

---

## 5. Tracker starten

```bash
python main.py
```

Optionen:
```
--config my.yaml     Alternatives Konfigfile
--device /dev/video2 Video-Device manuell vorgeben
--save-home          Startbox-Position speichern
```

---

## 6. Konfiguration (`config.yaml`)

| Parameter | Bedeutung | Standard |
|-----------|-----------|---------|
| `visca.port` | Serieller Port | `/dev/ttyUSB0` |
| `detector.model` | YOLOv8-Modell | `yolov8n.pt` |
| `detector.confidence` | Erkennungsschwelle | `0.45` |
| `tracker.pan_gain` | P-Verstärkung Pan | `12.0` |
| `tracker.tilt_gain` | P-Verstärkung Tilt | `8.0` |
| `tracker.dead_zone` | Totzone (% Bildhälfte) | `0.05` |
| `tracker.return_delay` | Wartezeit nach Verlust (s) | `1.5` |
| `tracker.home_preset` | VISCA-Preset-Index (0-basiert) | `0` |

---

## 7. REST API (mit `curl` steuern)

Die API läuft standardmäßig auf `0.0.0.0:8080` (siehe `config.yaml` → `api`).

```yaml
api:
  enabled: true
  host: 0.0.0.0
  port: 8080
```

Für `curl` kannst du lokal z. B. so arbeiten:

```bash
API="http://127.0.0.1:8080"
```

Von einem anderen Rechner im LAN:

```bash
API="http://<IP-DES-TRACKERS>:8080"
```

### 7.1 Status lesen

```bash
curl -s "$API/status"
```

Beispielantwort:

```json
{"state":"IDLE","tracking_enabled":true,"target":null}
```

### 7.2 Tracking ein-/ausschalten

Tracking einschalten:

```bash
curl -s -X POST "$API/tracking/enable"
```

Antwort:

```json
{"tracking_enabled":true}
```

Tracking ausschalten (stoppt Kamera + LED aus):

```bash
curl -s -X POST "$API/tracking/disable"
```

Antwort:

```json
{"tracking_enabled":false}
```

### 7.3 Kamera bewegen (`/move`)

Endpoint:

- `POST /move`
- JSON-Body: `pan`, `tilt`, optional `pan_speed`, `tilt_speed`

Limits (werden serverseitig geklemmt):

- `pan`: `-880 .. 880`
- `tilt`: `-300 .. 300`
- `pan_speed`: `1 .. 24`
- `tilt_speed`: `1 .. 20`

Beispiel:

```bash
curl -s -X POST "$API/move" \
  -H "Content-Type: application/json" \
  -d '{"pan":120,"tilt":-40,"pan_speed":12,"tilt_speed":8}'
```

Antwort:

```json
{"pan":120,"tilt":-40,"pan_speed":12,"tilt_speed":8}
```

Relative (inkrementelle) Bewegung:

```bash
curl -s -X POST "$API/move/relative" \
  -H "Content-Type: application/json" \
  -d '{"delta_pan":40,"delta_tilt":-20,"pan_speed":10,"tilt_speed":8}'
```

Hinweis: `delta_pan`/`delta_tilt` sind Offsets zur aktuellen Position.
Als Alias funktionieren auch `pan`/`tilt` im selben Endpoint.

Beispielantwort:

```json
{"ok":true,"delta_pan":40,"delta_tilt":-20,"from_pan":100,"from_tilt":-10,"pan":140,"tilt":-30,"pan_speed":10,"tilt_speed":8}
```

### 7.4 Sofort stoppen

```bash
curl -s -X POST "$API/stop"
```

Antwort:

```json
{"ok":true}
```

### 7.5 Zoom steuern

Zoom-In starten (Standard `speed=3`):

```bash
curl -s -X POST "$API/zoom/in"
```

Zoom-In mit expliziter Geschwindigkeit (`0..7`):

```bash
curl -s -X POST "$API/zoom/in" \
  -H "Content-Type: application/json" \
  -d '{"speed":5}'
```

Zoom-Out starten:

```bash
curl -s -X POST "$API/zoom/out" \
  -H "Content-Type: application/json" \
  -d '{"speed":4}'
```

Zoom stoppen:

```bash
curl -s -X POST "$API/zoom/stop"
```

Beispielantworten:

```json
{"zoom":"in","speed":5}
{"zoom":"out","speed":4}
{"zoom":"stop"}
```

### 7.6 White Balance / AE / Iris

White Balance Auto:

```bash
curl -s -X POST "$API/wb/auto"
```

White Balance Table Manual:

```bash
curl -s -X POST "$API/wb/table/manual"
```

White Balance Table Direct (Index):

```bash
curl -s -X POST "$API/wb/table/direct" \
  -H "Content-Type: application/json" \
  -d '{"index":12}'
```

AE Auto / Manual:

```bash
curl -s -X POST "$API/ae/auto"
curl -s -X POST "$API/ae/manual"
```

Iris Direct (`0..50`, typischerweise nur in AE Manual sinnvoll):

```bash
curl -s -X POST "$API/iris/direct" \
  -H "Content-Type: application/json" \
  -d '{"position":24}'
```

Weitere Bildparameter:

```bash
# Gain direct
curl -s -X POST "$API/gain/direct" -H "Content-Type: application/json" -d '{"position":12}'

# Backlight / Mirror / Flip
curl -s -X POST "$API/backlight/on"
curl -s -X POST "$API/backlight/off"
curl -s -X POST "$API/mirror/on"
curl -s -X POST "$API/mirror/off"
curl -s -X POST "$API/flip/on"
curl -s -X POST "$API/flip/off"

# Gamma
curl -s -X POST "$API/gamma/auto"
curl -s -X POST "$API/gamma/manual"
curl -s -X POST "$API/gamma/direct" -H "Content-Type: application/json" -d '{"table":4}'
```

Zoom/Focus Direct:

```bash
curl -s -X POST "$API/zoom/direct" -H "Content-Type: application/json" -d '{"position":1200}'
curl -s -X POST "$API/zoomfocus/direct" -H "Content-Type: application/json" -d '{"zoom":1200,"focus":800}'

curl -s -X POST "$API/focus/stop"
curl -s -X POST "$API/focus/far" -H "Content-Type: application/json" -d '{"speed":4}'
curl -s -X POST "$API/focus/near" -H "Content-Type: application/json" -d '{"speed":4}'
curl -s -X POST "$API/focus/direct" -H "Content-Type: application/json" -d '{"position":800}'
curl -s -X POST "$API/focus/auto"
curl -s -X POST "$API/focus/manual"
```

Pan/Tilt Jog + Direct + PTZF:

```bash
curl -s -X POST "$API/pt/stop"
curl -s -X POST "$API/pt/reset"
curl -s -X POST "$API/pt/up" -H "Content-Type: application/json" -d '{"pan_speed":3,"tilt_speed":5}'
curl -s -X POST "$API/pt/down-right" -H "Content-Type: application/json" -d '{"pan_speed":4,"tilt_speed":4}'

curl -s -X POST "$API/pt/direct" -H "Content-Type: application/json" -d '{"pan":120,"tilt":-40,"pan_speed":10,"tilt_speed":8}'
curl -s -X POST "$API/ptzf/direct" -H "Content-Type: application/json" -d '{"pan":120,"tilt":-40,"zoom":1200,"focus":800}'
```

### 7.7 Home-Position speichern und anfahren

Aktuelle Kamera-Position als Home speichern:

```bash
curl -s -X POST "$API/preset/save"
```

Erfolgsantwort:

```json
{"saved":true,"pan":34,"tilt":-12}
```

Home-Position anfahren:

```bash
curl -s -X POST "$API/preset/recall"
```

Erfolgsantwort:

```json
{"recalled":true,"pan":34,"tilt":-12}
```

Wenn noch nichts gespeichert wurde:

```json
{"recalled":false,"error":"no preset saved"}
```

### 7.8 Praktische `curl`-One-Liner

Status hübsch formatiert (mit `jq`):

```bash
curl -s "$API/status" | jq
```

Tracking aus, Position setzen, Tracking wieder an:

```bash
curl -s -X POST "$API/tracking/disable" && \
curl -s -X POST "$API/move" -H "Content-Type: application/json" -d '{"pan":0,"tilt":0}' && \
curl -s -X POST "$API/tracking/enable"
```

---

## 8. Systemd-Dienst (optional, Autostart)

```ini
# /etc/systemd/system/rally-tracker.service
[Unit]
Description=Rally PTZ Tracker
After=network.target

[Service]
User=pi
WorkingDirectory=/home/pi/stream
ExecStart=/home/pi/stream/.venv/bin/python main.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now rally-tracker
```

---

## 9. Ablauf im Betrieb

```
IDLE  ──► (Fahrzeug erkannt) ──► TRACKING  ──► (Fahrzeug weg) ──► RETURNING ──► IDLE
                                     ▲                                 │
                                     └─── (neues Fahrzeug während Rückkehr) ──────┘
```

---

## 10. MQTT-Steuerung (komplette Software)

MQTT ist in `config.yaml` standardmäßig deaktiviert und kann so aktiviert werden:

```yaml
mqtt:
  enabled: true
  host: 127.0.0.1
  port: 1883
  username: ""
  password: ""
  topic_prefix: rally_tracker
  qos: 1
  status_interval: 5.0
```

### 10.1 Topics

Publish (vom Tracker):

- `<prefix>/online` (retained): `{"online": true|false}`
- `<prefix>/status` (retained): Tracker-State + Tracking-Flag + Target
- `<prefix>/detection`: Detektions-Events
- `<prefix>/camera/position`: aktuelle Pan/Tilt-Position
- `<prefix>/system` (retained): SoC-Temperatur + Lüfterdrehzahl

Subscribe (Kommandos an Tracker):

- `<prefix>/cmd/tracking` → Payload `enable` oder `disable`
- `<prefix>/cmd/move` → JSON `{"pan":...,"tilt":...,"pan_speed":...,"tilt_speed":...}`
- `<prefix>/cmd/move/relative` → JSON `{"delta_pan":...,"delta_tilt":...,"pan_speed":...,"tilt_speed":...}`
- `<prefix>/cmd/stop` → beliebiger Payload
- `<prefix>/cmd/zoom/in` → optional JSON `{"speed":0..7}` oder Payload `0..7`
- `<prefix>/cmd/zoom/out` → optional JSON `{"speed":0..7}` oder Payload `0..7`
- `<prefix>/cmd/zoom/stop` → beliebiger Payload
- `<prefix>/cmd/wb/auto` → beliebiger Payload
- `<prefix>/cmd/wb/table/manual` → beliebiger Payload
- `<prefix>/cmd/wb/table/direct` → JSON `{"index":...}` oder Payload int
- `<prefix>/cmd/ae/auto` → beliebiger Payload
- `<prefix>/cmd/ae/manual` → beliebiger Payload
- `<prefix>/cmd/iris/direct` → JSON `{"position":0..50}` oder Payload int
- `<prefix>/cmd/gain/direct` → JSON `{"position":...}` oder Payload int
- `<prefix>/cmd/backlight/on` / `<prefix>/cmd/backlight/off`
- `<prefix>/cmd/mirror/on` / `<prefix>/cmd/mirror/off`
- `<prefix>/cmd/flip/on` / `<prefix>/cmd/flip/off`
- `<prefix>/cmd/gamma/auto` / `<prefix>/cmd/gamma/manual` / `<prefix>/cmd/gamma/direct`
- `<prefix>/cmd/zoom/direct` → JSON `{"position":...}` oder Payload int
- `<prefix>/cmd/zoomfocus/direct` → JSON `{"zoom":...,"focus":...}`
- `<prefix>/cmd/focus/stop|far|near|direct|auto|manual`
- `<prefix>/cmd/pt/stop|reset|up|down|left|right|up-left|up-right|down-left|down-right|direct`
- `<prefix>/cmd/ptzf/direct` → JSON `{"pan":...,"tilt":...,"zoom":...,"focus":...}`
- `<prefix>/cmd/preset/save` → beliebiger Payload
- `<prefix>/cmd/preset/recall` → beliebiger Payload

`<prefix>` ist standardmäßig `rally_tracker`.

### 10.2 Befehle mit `mosquitto_pub`

Beispiele (lokaler Broker):

```bash
BROKER=127.0.0.1
PORT=1883
PREFIX=rally_tracker
```

Tracking ein/aus:

```bash
mosquitto_pub -h "$BROKER" -p "$PORT" -t "$PREFIX/cmd/tracking" -m "enable"
mosquitto_pub -h "$BROKER" -p "$PORT" -t "$PREFIX/cmd/tracking" -m "disable"
```

Absolute Bewegung:

```bash
mosquitto_pub -h "$BROKER" -p "$PORT" -t "$PREFIX/cmd/move" \
  -m '{"pan":120,"tilt":-40,"pan_speed":12,"tilt_speed":8}'
```

Relative (inkrementelle) Bewegung:

```bash
mosquitto_pub -h "$BROKER" -p "$PORT" -t "$PREFIX/cmd/move/relative" \
  -m '{"delta_pan":40,"delta_tilt":-20,"pan_speed":10,"tilt_speed":8}'
```

Stop:

```bash
mosquitto_pub -h "$BROKER" -p "$PORT" -t "$PREFIX/cmd/stop" -m "1"
```

Zoom:

```bash
mosquitto_pub -h "$BROKER" -p "$PORT" -t "$PREFIX/cmd/zoom/in" -m '{"speed":5}'
mosquitto_pub -h "$BROKER" -p "$PORT" -t "$PREFIX/cmd/zoom/out" -m "4"
mosquitto_pub -h "$BROKER" -p "$PORT" -t "$PREFIX/cmd/zoom/stop" -m "1"
```

White Balance / AE / Iris:

```bash
mosquitto_pub -h "$BROKER" -p "$PORT" -t "$PREFIX/cmd/wb/auto" -m "1"
mosquitto_pub -h "$BROKER" -p "$PORT" -t "$PREFIX/cmd/wb/table/manual" -m "1"
mosquitto_pub -h "$BROKER" -p "$PORT" -t "$PREFIX/cmd/wb/table/direct" -m '{"index":12}'
mosquitto_pub -h "$BROKER" -p "$PORT" -t "$PREFIX/cmd/ae/auto" -m "1"
mosquitto_pub -h "$BROKER" -p "$PORT" -t "$PREFIX/cmd/ae/manual" -m "1"
mosquitto_pub -h "$BROKER" -p "$PORT" -t "$PREFIX/cmd/iris/direct" -m '{"position":24}'
mosquitto_pub -h "$BROKER" -p "$PORT" -t "$PREFIX/cmd/gain/direct" -m '{"position":12}'
mosquitto_pub -h "$BROKER" -p "$PORT" -t "$PREFIX/cmd/backlight/on" -m "1"
mosquitto_pub -h "$BROKER" -p "$PORT" -t "$PREFIX/cmd/mirror/off" -m "1"
mosquitto_pub -h "$BROKER" -p "$PORT" -t "$PREFIX/cmd/flip/on" -m "1"
mosquitto_pub -h "$BROKER" -p "$PORT" -t "$PREFIX/cmd/gamma/direct" -m '{"table":4}'
```

Zoom/Focus/PT/PTZF:

```bash
mosquitto_pub -h "$BROKER" -p "$PORT" -t "$PREFIX/cmd/zoom/direct" -m '{"position":1200}'
mosquitto_pub -h "$BROKER" -p "$PORT" -t "$PREFIX/cmd/zoomfocus/direct" -m '{"zoom":1200,"focus":800}'
mosquitto_pub -h "$BROKER" -p "$PORT" -t "$PREFIX/cmd/focus/far" -m '{"speed":4}'
mosquitto_pub -h "$BROKER" -p "$PORT" -t "$PREFIX/cmd/focus/manual" -m "1"
mosquitto_pub -h "$BROKER" -p "$PORT" -t "$PREFIX/cmd/pt/up-left" -m '{"pan_speed":4,"tilt_speed":4}'
mosquitto_pub -h "$BROKER" -p "$PORT" -t "$PREFIX/cmd/pt/direct" -m '{"pan":120,"tilt":-40,"pan_speed":10,"tilt_speed":8}'
mosquitto_pub -h "$BROKER" -p "$PORT" -t "$PREFIX/cmd/ptzf/direct" -m '{"pan":120,"tilt":-40,"zoom":1200,"focus":800}'
```

Preset/Home:

```bash
mosquitto_pub -h "$BROKER" -p "$PORT" -t "$PREFIX/cmd/preset/save" -m "1"
mosquitto_pub -h "$BROKER" -p "$PORT" -t "$PREFIX/cmd/preset/recall" -m "1"
```

### 10.3 Status live beobachten

```bash
mosquitto_sub -h "$BROKER" -p "$PORT" -t "$PREFIX/#" -v
```

Beispiel für System-Topic:

```json
{"soc_temp_c":62.4,"fan_rpm":3180,"ts":1713896508.12}
```

Wenn ein Wert auf der Hardware nicht verfügbar ist, wird `null` gesendet
(z. B. `{"soc_temp_c":61.9,"fan_rpm":null,...}`).
