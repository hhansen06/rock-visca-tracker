#!/usr/bin/env bash
# =============================================================================
# scripts/build-deb.sh
#
# Builds the rally-tracker DEB package for RK3588/arm64.
# 
# Usage:
#   ./scripts/build-deb.sh [VERSION]
#
# If VERSION is not provided, it will be extracted from git tag or default to 0.0.0-dev
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
BUILD_ROOT="${PROJECT_ROOT}/build/deb"

# Determine version
if [[ $# -ge 1 ]]; then
    VERSION="$1"
else
    # Try to extract from git tag
    if git describe --tags --exact-match 2>/dev/null; then
        VERSION=$(git describe --tags --exact-match | sed 's/^v//')
    else
        VERSION="0.0.0-dev"
        echo "WARNING: No git tag found, using version: $VERSION"
    fi
fi

echo "==================================================================="
echo "Building rally-tracker DEB package"
echo "==================================================================="
echo "Version: $VERSION"
echo "Build root: $BUILD_ROOT"
echo ""

# Clean previous build
rm -rf "$BUILD_ROOT"
mkdir -p "$BUILD_ROOT"

# Create package structure
PKG_DIR="$BUILD_ROOT/rally-tracker_${VERSION}_arm64"
mkdir -p "$PKG_DIR/DEBIAN"
mkdir -p "$PKG_DIR/opt/rally_tracker/wheels"
mkdir -p "$PKG_DIR/systemd"

# Copy DEBIAN control files
echo "Copying DEBIAN control files..."
cp "$PROJECT_ROOT/debian/control" "$PKG_DIR/DEBIAN/"
cp "$PROJECT_ROOT/debian/postinst" "$PKG_DIR/DEBIAN/"
cp "$PROJECT_ROOT/debian/prerm" "$PKG_DIR/DEBIAN/"
cp "$PROJECT_ROOT/debian/postrm" "$PKG_DIR/DEBIAN/"

# Make scripts executable
chmod 755 "$PKG_DIR/DEBIAN/postinst"
chmod 755 "$PKG_DIR/DEBIAN/prerm"
chmod 755 "$PKG_DIR/DEBIAN/postrm"

# Replace VERSION_PLACEHOLDER in control file
sed -i "s/VERSION_PLACEHOLDER/$VERSION/" "$PKG_DIR/DEBIAN/control"

# Copy application files
echo "Copying application files..."
cp "$PROJECT_ROOT"/*.py "$PKG_DIR/opt/rally_tracker/" || true
cp "$PROJECT_ROOT"/*.yaml "$PKG_DIR/opt/rally_tracker/" || true
cp "$PROJECT_ROOT"/requirements.txt "$PKG_DIR/opt/rally_tracker/"

# Copy model files
echo "Copying model files..."
cp "$PROJECT_ROOT"/*.rknn "$PKG_DIR/opt/rally_tracker/" 2>/dev/null || echo "  No .rknn models found"
cp "$PROJECT_ROOT"/*.pt "$PKG_DIR/opt/rally_tracker/" 2>/dev/null || echo "  No .pt models found"
cp "$PROJECT_ROOT"/*.onnx "$PKG_DIR/opt/rally_tracker/" 2>/dev/null || echo "  No .onnx models found"

# Copy systemd service
echo "Copying systemd service..."
cp "$PROJECT_ROOT/systemd/rally-tracker.service" "$PKG_DIR/systemd/"

# Extract RKNN wheels if venv exists
echo "Extracting RKNN wheels..."
if [[ -d "$PROJECT_ROOT/venv" ]]; then
    VENV_PYTHON="$PROJECT_ROOT/venv/bin/python"
    VENV_PIP="$PROJECT_ROOT/venv/bin/pip"
    
    if [[ -x "$VENV_PYTHON" ]] && "$VENV_PIP" show rknn-toolkit-lite2 &>/dev/null; then
        echo "  Found RKNN in venv, extracting..."
        "$VENV_PIP" wheel \
            --wheel-dir="$PKG_DIR/opt/rally_tracker/wheels" \
            --no-deps \
            rknn-toolkit-lite2 2>/dev/null || {
            echo "  pip wheel failed, trying manual extraction..."
            SITE_PKG=$("$VENV_PYTHON" -c "import site; print(site.getsitepackages()[0])")
            if [[ -d "$SITE_PKG/rknnlite" ]]; then
                cd "$SITE_PKG"
                WHEEL_NAME="rknn_toolkit_lite2-2.3.2-py3-none-linux_aarch64.whl"
                zip -r "$PKG_DIR/opt/rally_tracker/wheels/${WHEEL_NAME}" \
                    rknnlite/ rknn_toolkit_lite2*.dist-info/ 2>/dev/null || true
                echo "  Manual wheel created: ${WHEEL_NAME}"
            fi
            cd "$PROJECT_ROOT"
        }
    else
        echo "  RKNN not found in venv — CPU fallback will be used in package"
    fi
else
    echo "  No venv found — skipping RKNN extraction"
    echo "  Package will use CPU fallback for inference"
fi

# Build package
echo ""
echo "Building DEB package..."
cd "$BUILD_ROOT"
dpkg-deb --build --root-owner-group "rally-tracker_${VERSION}_arm64"

DEB_FILE="$BUILD_ROOT/rally-tracker_${VERSION}_arm64.deb"
if [[ -f "$DEB_FILE" ]]; then
    echo ""
    echo "==================================================================="
    echo "SUCCESS: DEB package built"
    echo "==================================================================="
    echo "File: $DEB_FILE"
    echo "Size: $(du -h "$DEB_FILE" | cut -f1)"
    echo ""
    echo "To install:"
    echo "  sudo dpkg -i $DEB_FILE"
    echo "  sudo apt-get install -f  # if dependencies missing"
    echo ""
    exit 0
else
    echo "ERROR: Failed to build DEB package"
    exit 1
fi
