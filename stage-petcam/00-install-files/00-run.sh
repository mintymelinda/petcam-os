#!/bin/bash -e
APP_VERSION="$(tr -d '[:space:]' < files/usr/local/lib/petcam/VERSION)"
if [[ ! "$APP_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Invalid PetCam VERSION: $APP_VERSION" >&2
    exit 1
fi

RELEASE_DIR="${ROOTFS_DIR}/opt/petcam/releases/${APP_VERSION}"
install -D -o root -g root -m 0644 files/etc/petcam/update-signing.pub "${ROOTFS_DIR}/etc/petcam/update-signing.pub"
install -d -m 0755 "$RELEASE_DIR/lib"
install -m 0644 files/usr/local/lib/petcam/VERSION "$RELEASE_DIR/VERSION"
install -m 0644 files/usr/local/lib/petcam/camera.py "$RELEASE_DIR/lib/camera.py"
install -m 0644 files/usr/local/lib/petcam/csi_stream.py "$RELEASE_DIR/lib/csi_stream.py"
install -m 0644 files/usr/local/lib/petcam/web_manager.py "$RELEASE_DIR/lib/web_manager.py"
install -m 0644 files/usr/local/lib/petcam/web_auth.py "$RELEASE_DIR/lib/web_auth.py"
install -m 0644 files/usr/local/lib/petcam/health_monitor.py "$RELEASE_DIR/lib/health_monitor.py"
install -m 0644 files/usr/local/lib/petcam/config.py "$RELEASE_DIR/lib/config.py"
install -m 0755 files/usr/local/lib/petcam/updater.py "${ROOTFS_DIR}/usr/local/bin/petcam-update"
printf '{"schema_version":1,"application":"petcam-os","version":"%s","architecture":"armhf","ustreamer_commit":"11fd828ab00c01a39bd0843ef442ef0b6d3cac41"}\n' "$APP_VERSION" > "$RELEASE_DIR/manifest.json"
chmod 0644 "$RELEASE_DIR/manifest.json"
