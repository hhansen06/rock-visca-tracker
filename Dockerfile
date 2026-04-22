# =============================================================================
# rally_tracker — Production Dockerfile für RK3588 (aarch64)
#
# Multi-stage Build:
#   Stage 1 "rknn-wheel"  – baut/kopiert RKNN Lite2 aus dem lokalen venv
#   Stage 2 "runtime"     – schlankes Laufzeit-Image
#
# Build-Voraussetzung:
#   Das rknn-toolkit-lite2 Wheel wird NICHT von PyPI gezogen (proprietär),
#   sondern als Pre-built Wheel aus dem bestehenden venv extrahiert.
#   Das Wheel-File muss unter build/rknn_toolkit_lite2-*.whl liegen
#   (wird von scripts/extract_wheels.sh erzeugt).
#
# Build:
#   ./scripts/extract_wheels.sh          # einmalig Wheels aus venv extrahieren
#   docker build -t rally_tracker .
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: Python-Abhängigkeiten bauen / installieren
# -----------------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS builder

# System-Pakete für Build (werden nicht ins Runtime-Image übernommen)
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        g++ \
        libglib2.0-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Standard-Abhängigkeiten
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# RKNN Lite2 Wheel (proprietär, aus lokalem venv extrahiert)
# Wenn die Datei nicht existiert, wird der RKNN-Backend nicht verfügbar sein —
# der CPU-Fallback greift dann automatisch.
COPY build/rknn_toolkit_lite2*.whl* ./
RUN if ls rknn_toolkit_lite2*.whl 1>/dev/null 2>&1; then \
        pip install --no-cache-dir --prefix=/install rknn_toolkit_lite2*.whl; \
        echo "RKNN Lite2 installiert."; \
    else \
        echo "WARNUNG: Kein RKNN Wheel gefunden — CPU-Fallback aktiv."; \
    fi


# -----------------------------------------------------------------------------
# Stage 2: Runtime-Image
# -----------------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="rally_tracker" \
      org.opencontainers.image.description="Autonomous PTZ camera tracking for rally livestreams" \
      org.opencontainers.image.source="https://github.com/yourorg/rally_tracker"

# --- System-Laufzeitpakete ---------------------------------------------------
# GStreamer-Stack + RK3588-spezifische Plugins
RUN apt-get update && apt-get install -y --no-install-recommends \
        # GStreamer core + bindings
        python3-gi \
        python3-gi-cairo \
        gir1.2-gstreamer-1.0 \
        gir1.2-gst-plugins-base-1.0 \
        gstreamer1.0-tools \
        # GStreamer Plugin-Sets
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-plugins-ugly \
        # V4L2 Utilities (für HDMI-RX Auto-Detection)
        v4l-utils \
        # Shared Libraries
        libglib2.0-0 \
        libgstreamer1.0-0 \
        libgstreamer-plugins-base1.0-0 \
    && rm -rf /var/lib/apt/lists/*

# --- PyGObject ins Python-Pfad symlinken ------------------------------------
# python3-gi wird system-weit installiert; damit der venv-Python sie findet,
# verlinken wir die gi-Bindungen in site-packages.
RUN PYVER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')") && \
    SRC="/usr/lib/python3/dist-packages/gi" && \
    DST="/usr/local/lib/python${PYVER}/site-packages/gi" && \
    if [ -d "$SRC" ] && [ ! -e "$DST" ]; then ln -s "$SRC" "$DST"; fi

# --- mpph264enc: Rockchip MPP GStreamer Plugin --------------------------------
# Dieses Plugin liegt auf dem Host-System im BSP (Board Support Package) und
# ist NICHT Teil der Standard-Debian-Pakete.
# Es wird als Bind-Mount zur Laufzeit eingebunden:
#   /usr/lib/aarch64-linux-gnu/gstreamer-1.0/libgstmpph264enc.so
# → wird in docker-compose.yml als Read-only Volume gemountet.
#
# Alternativ: Wenn das Plugin als .deb auf dem Host installiert ist, kann es
# hier per COPY eingebunden werden:
#   COPY --from=host /usr/lib/aarch64-linux-gnu/gstreamer-1.0/libgstrkmpp.so \
#        /usr/lib/aarch64-linux-gnu/gstreamer-1.0/
#
# Wir setzen GST_PLUGIN_PATH so, dass auch /opt/gst-plugins durchsucht wird
# (dort kann das Plugin per Volume-Mount landet).
ENV GST_PLUGIN_PATH=/opt/gst-plugins:/usr/lib/aarch64-linux-gnu/gstreamer-1.0

# --- Installierte Python-Pakete aus Stage 1 übernehmen ----------------------
COPY --from=builder /install /usr/local

# --- App-Code ----------------------------------------------------------------
WORKDIR /app

# Nur den eigentlichen Quellcode kopieren (keine venvs, keine Modelle)
COPY main.py capture.py detector.py tracker.py visca.py api.py mqtt.py ./

# YOLOv8-Modelle (werden zur Build-Zeit eingebettet — alternativ per Volume)
# .pt-Modelle werden nur beim CPU-Fallback benötigt
COPY *.rknn ./
COPY *.pt   ./

# --- Nicht-root User ---------------------------------------------------------
# Der User braucht Zugriff auf /dev/ttyUSB0 (dialout) und /dev/video* (video)
# Die Gruppen werden vom Host zur Laufzeit per --group-add übergeben.
RUN groupadd -r appuser && useradd -r -g appuser appuser
RUN chown -R appuser:appuser /app

# --- Healthcheck -------------------------------------------------------------
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:${RT_API_PORT:-8080}/status')" \
    || exit 1

# --- Entrypoint --------------------------------------------------------------
USER appuser
ENTRYPOINT ["python3", "main.py"]
CMD []
