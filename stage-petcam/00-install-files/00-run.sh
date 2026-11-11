#!/bin/bash -e
APP_VERSION="$(tr -d '[:space:]' < files/usr/local/lib/petcam/VERSION)"
if [[ ! "$APP_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Invalid PetCam VERSION: $APP_VERSION" >&2
    exit 1
fi

RELEASE_DIR="${ROOTFS_DIR}/opt/petcam/releases/${APP_VERSION}"
install -d -m 0755 "$RELEASE_DIR/lib"
install -m 0644 files/usr/local/lib/petcam/VERSION "$RELEASE_DIR/VERSION"
install -m 0644 files/usr/local/lib/petcam/camera.py "$RELEASE_DIR/lib/camera.py"
install -m 0644 files/usr/local/lib/petcam/csi_stream.py "$RELEASE_DIR/lib/csi_stream.py"
install -m 0644 files/usr/local/lib/petcam/web_manager.py "$RELEASE_DIR/lib/web_manager.py"
printf '{"schema_version":1,"application":"petcam-os","version":"%s","architecture":"armhf"}\n' "$APP_VERSION" > "$RELEASE_DIR/manifest.json"
chmod 0644 "$RELEASE_DIR/manifest.json"
