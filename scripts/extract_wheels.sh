#!/usr/bin/env bash
# =============================================================================
# scripts/extract_wheels.sh
#
# Extrahiert das RKNN Toolkit Lite2 Wheel aus dem bestehenden venv und legt
# es unter build/ ab, damit der Docker-Build es verwenden kann.
#
# Einmalig ausführen, bevor das Image gebaut wird:
#   chmod +x scripts/extract_wheels.sh
#   ./scripts/extract_wheels.sh
#
# Voraussetzung: Das Paket ist im venv unter
#   venv/lib/python3.11/site-packages/rknnlite/  installiert.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
VENV_DIR="${PROJECT_ROOT}/venv"
BUILD_DIR="${PROJECT_ROOT}/build"

mkdir -p "$BUILD_DIR"

# --- Python aus dem venv verwenden -------------------------------------------
PYTHON="${VENV_DIR}/bin/python"
PIP="${VENV_DIR}/bin/pip"

if [[ ! -x "$PYTHON" ]]; then
    echo "FEHLER: Python nicht gefunden unter $PYTHON"
    echo "Bitte sicherstellen dass das venv unter venv/ existiert."
    exit 1
fi

echo "==> Python: $($PYTHON --version)"
echo "==> Extrahiere RKNN Toolkit Lite2 Wheel..."

# Wheel aus dem venv erzeugen (pip wheel packt installiertes Paket um)
if "$PIP" show rknn-toolkit-lite2 &>/dev/null; then
    "$PIP" wheel \
        --wheel-dir="$BUILD_DIR" \
        --no-deps \
        rknn-toolkit-lite2 2>/dev/null \
    || {
        # Fallback: direkt aus site-packages als Wheel zusammenbauen
        echo "  pip wheel fehlgeschlagen — versuche direkten Copy..."
        SITE_PKG=$("$PYTHON" -c "import site; print(site.getsitepackages()[0])")
        RKNN_DIR="${SITE_PKG}/rknnlite"
        if [[ -d "$RKNN_DIR" ]]; then
            # Einfaches zip-basiertes Wheel erzeugen
            WHEEL_NAME="rknn_toolkit_lite2-2.3.2-py3-none-linux_aarch64.whl"
            cd "$SITE_PKG"
            zip -r "${BUILD_DIR}/${WHEEL_NAME}" rknnlite/ rknn_toolkit_lite2*.dist-info/ 2>/dev/null || true
            echo "  Wheel manuell erstellt: ${BUILD_DIR}/${WHEEL_NAME}"
        else
            echo "  WARNUNG: rknnlite nicht im venv gefunden — RKNN-Backend wird im Container nicht verfügbar sein."
            echo "           CPU-Fallback ist aktiv."
            touch "${BUILD_DIR}/.no_rknn"
        fi
    }
    echo "==> Wheel(s) in ${BUILD_DIR}/:"
    ls -lh "${BUILD_DIR}"/*.whl 2>/dev/null || echo "  (keine Wheels)"
else
    echo "WARNUNG: rknn-toolkit-lite2 nicht im venv installiert."
    echo "         CPU-Fallback wird im Container verwendet."
    touch "${BUILD_DIR}/.no_rknn"
fi

# --- Modelle ins models/ Verzeichnis kopieren (falls nicht vorhanden) --------
MODELS_DIR="${PROJECT_ROOT}/models"
mkdir -p "$MODELS_DIR"

echo ""
echo "==> Kopiere Modell-Dateien nach models/..."
for f in "${PROJECT_ROOT}"/*.rknn "${PROJECT_ROOT}"/*.pt "${PROJECT_ROOT}"/*.onnx; do
    [[ -f "$f" ]] || continue
    fname="$(basename "$f")"
    if [[ ! -f "${MODELS_DIR}/${fname}" ]]; then
        cp "$f" "${MODELS_DIR}/"
        echo "  Kopiert: $fname"
    else
        echo "  Bereits vorhanden: $fname"
    fi
done

echo ""
echo "==> Fertig. Jetzt Image bauen:"
echo "    docker build -t rally_tracker ."
