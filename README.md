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

## 7. Systemd-Dienst (optional, Autostart)

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

## Ablauf im Betrieb

```
IDLE  ──► (Fahrzeug erkannt) ──► TRACKING  ──► (Fahrzeug weg) ──► RETURNING ──► IDLE
                                     ▲                                 │
                                     └─── (neues Fahrzeug während Rückkehr) ──────┘
```
