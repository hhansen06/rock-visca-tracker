# Rally Tracker — Installation Guide

This guide covers installation of the Rally Tracker system on RK3588-based hardware using the pre-built DEB package.

## System Requirements

- **Hardware**: RK3588 SoC (Rock 5B or similar)
- **OS**: Debian 12 (Bookworm) or compatible, arm64 architecture
- **Camera**: USB VISCA camera (e.g., Sony PTZ camera)
- **Network**: Ethernet or WiFi for MQTT/REST API access

## Installation

### 1. Download DEB Package

Download the latest release from GitHub:

```bash
# Replace X.Y.Z with the desired version
VERSION="1.0.0"
wget https://github.com/hhansen06/rock-visca-tracker/releases/download/v${VERSION}/rally-tracker_${VERSION}_arm64.deb
```

### 2. Install Package

```bash
sudo dpkg -i rally-tracker_${VERSION}_arm64.deb
```

If there are missing dependencies, install them:

```bash
sudo apt-get update
sudo apt-get install -f
```

The package will:
- Install files to `/opt/rally_tracker/`
- Create a Python virtual environment with all dependencies
- Install RKNN Toolkit Lite2 (if included in package)
- Copy default config to `/etc/rally-tracker/config.yaml`
- Install and enable systemd service `rally-tracker.service`

### 3. Configure

Edit the configuration file:

```bash
sudo nano /etc/rally-tracker/config.yaml
```

Key settings to adjust:

```yaml
camera:
  device: "/dev/video0"          # Your camera device
  width: 1920
  height: 1080
  fps: 30

serial:
  port: "/dev/ttyUSB0"           # VISCA serial port
  baudrate: 9600

detection:
  mode: "vehicles"               # or "faces"
  model_path: "yolov8n.pt"       # Model file

mqtt:
  enabled: true
  broker: "localhost"            # Your MQTT broker
  port: 1883
  topic_prefix: "rally_tracker"

api:
  host: "0.0.0.0"
  port: 8080
```

### 4. Start Service

```bash
# Start service
sudo systemctl start rally-tracker

# Check status
sudo systemctl status rally-tracker

# View logs
journalctl -u rally-tracker -f
```

The service is already enabled and will start automatically on boot.

## Verification

### Check REST API

```bash
# Get system info
curl http://localhost:8080/info

# Get current state
curl http://localhost:8080/state
```

### Check MQTT

```bash
# Subscribe to status updates
mosquitto_sub -h localhost -t "rally_tracker/status/#" -v

# Send a command
mosquitto_pub -h localhost -t "rally_tracker/cmd/state" -m "IDLE"
```

### Check Video Stream

The H.264 stream is available on UDP:

```bash
# View stream with ffplay
ffplay -fflags nobuffer -flags low_delay udp://127.0.0.1:5000
```

Or with GStreamer:

```bash
gst-launch-1.0 udpsrc port=5000 ! h264parse ! v4l2h264dec ! autovideosink
```

## Service Management

```bash
# Start service
sudo systemctl start rally-tracker

# Stop service
sudo systemctl stop rally-tracker

# Restart service
sudo systemctl restart rally-tracker

# View status
sudo systemctl status rally-tracker

# View logs (last 50 lines)
journalctl -u rally-tracker -n 50

# Follow logs in real-time
journalctl -u rally-tracker -f

# Disable auto-start on boot
sudo systemctl disable rally-tracker

# Enable auto-start on boot
sudo systemctl enable rally-tracker
```

## Uninstallation

### Remove Package (Keep Config)

```bash
sudo apt-get remove rally-tracker
```

This will:
- Stop and disable the service
- Remove `/opt/rally_tracker/`
- Keep `/etc/rally-tracker/config.yaml`

### Purge Package (Remove Everything)

```bash
sudo apt-get purge rally-tracker
```

This will remove everything including configuration files.

## Updating

To update to a new version:

```bash
# Stop service
sudo systemctl stop rally-tracker

# Download new version
wget https://github.com/hhansen06/rock-visca-tracker/releases/download/vX.Y.Z/rally-tracker_X.Y.Z_arm64.deb

# Install (will upgrade)
sudo dpkg -i rally-tracker_X.Y.Z_arm64.deb

# Start service
sudo systemctl start rally-tracker
```

Your configuration in `/etc/rally-tracker/config.yaml` will be preserved.

## Troubleshooting

### Service Won't Start

Check logs for errors:

```bash
journalctl -u rally-tracker -n 100 --no-pager
```

Common issues:
- Camera device not found: Check `camera.device` in config
- Serial port permission denied: Add user to `dialout` group (service runs as root, so this shouldn't happen)
- MQTT broker unreachable: Check `mqtt.broker` and network connectivity

### No Video Stream

Check that camera is detected:

```bash
ls -l /dev/video*
v4l2-ctl --list-devices
```

Test GStreamer pipeline manually:

```bash
gst-launch-1.0 v4l2src device=/dev/video0 ! "video/x-raw,format=BGR,width=1920,height=1080,framerate=30/1" ! autovideosink
```

### RKNN Not Working (CPU Fallback)

Check if RKNN is installed:

```bash
/opt/rally_tracker/venv/bin/python -c "import rknnlite; print('RKNN OK')"
```

If RKNN is missing, the tracker will automatically fall back to CPU-based inference (slower).

### Permission Issues

The service runs as `root` to access hardware devices. If you need to run as a different user, edit the service file:

```bash
sudo nano /etc/systemd/system/rally-tracker.service
```

Change `User=root` to your desired user, then reload:

```bash
sudo systemctl daemon-reload
sudo systemctl restart rally-tracker
```

## Building from Source

If you want to build the DEB package yourself:

```bash
# Clone repository
git clone https://github.com/hhansen06/rock-visca-tracker.git
cd rock-visca-tracker

# Create venv and install RKNN (optional, for RKNN support)
python3 -m venv venv
source venv/bin/activate
# ... install rknn-toolkit-lite2 wheel manually ...

# Build DEB
./scripts/build-deb.sh 1.0.0

# Package will be in build/deb/
sudo dpkg -i build/deb/rally-tracker_1.0.0_arm64.deb
```

## Support

- **GitHub Issues**: https://github.com/hhansen06/rock-visca-tracker/issues
- **README**: Full API documentation in `README.md`
- **Config Example**: `/etc/rally-tracker/config.yaml` or `config.yaml.example` in repo

## License

See `LICENSE` file in the repository.
