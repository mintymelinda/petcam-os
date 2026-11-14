#!/bin/bash -e
APP_VERSION="$(tr -d '[:space:]' < files/usr/local/lib/petcam/VERSION)"
if [[ ! "$APP_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Invalid PetCam VERSION: $APP_VERSION" >&2
    exit 1
fi

RELEASE_DIR="${ROOTFS_DIR}/opt/petcam/releases/${APP_VERSION}"
install -D -o root -g root -m 0644 files/etc/petcam/update-signing.pub "${ROOTFS_DIR}/etc/petcam/update-signing.pub"
install -d -m 0755 "$RELEASE_DIR/lib" "$RELEASE_DIR/systemd" "$RELEASE_DIR/etc/petcam"
install -d -m 0755 "$RELEASE_DIR/bin"
MEDIAMTX_VERSION="1.21.3"
TARGET_ARCH="$(chroot "$ROOTFS_DIR" dpkg --print-architecture)"
case "$TARGET_ARCH" in
    armhf) MEDIAMTX_ARCH="armv6"; MEDIAMTX_SHA256="cbacd828dd7c8bb67954eee2b0b5590b1f1f6994faa6fcd86a3cf0ba50213095" ;;
    arm64) MEDIAMTX_ARCH="arm64"; MEDIAMTX_SHA256="27f038237cb5f77a724f6afc58eca2798f2bc4edf9989895631a80eeb41a4fe2" ;;
    *) echo "Unsupported PetCam architecture for MediaMTX: $TARGET_ARCH" >&2; exit 1 ;;
esac
MEDIAMTX_ARCHIVE="mediamtx_v${MEDIAMTX_VERSION}_linux_${MEDIAMTX_ARCH}.tar.gz"
MEDIAMTX_TMP="$(mktemp -d)"
trap 'rm -rf "$MEDIAMTX_TMP"' EXIT
python3 - "$MEDIAMTX_TMP/$MEDIAMTX_ARCHIVE" "https://github.com/bluenviron/mediamtx/releases/download/v${MEDIAMTX_VERSION}/${MEDIAMTX_ARCHIVE}" <<'PY'
import sys
import urllib.request
urllib.request.urlretrieve(sys.argv[2], sys.argv[1])
PY
echo "$MEDIAMTX_SHA256  $MEDIAMTX_TMP/$MEDIAMTX_ARCHIVE" | sha256sum --check
tar -xzf "$MEDIAMTX_TMP/$MEDIAMTX_ARCHIVE" -C "$MEDIAMTX_TMP" mediamtx
install -m 0755 "$MEDIAMTX_TMP/mediamtx" "$RELEASE_DIR/bin/mediamtx"

install -d -m 0755 "${ROOTFS_DIR}/mnt/petcam-network"
install -m 0644 files/usr/local/lib/petcam/VERSION "$RELEASE_DIR/VERSION"
install -m 0644 files/usr/local/lib/petcam/camera.py "$RELEASE_DIR/lib/camera.py"
install -m 0644 files/usr/local/lib/petcam/csi_stream.py "$RELEASE_DIR/lib/csi_stream.py"
install -m 0644 files/usr/local/lib/petcam/rtmp_output.py "$RELEASE_DIR/lib/rtmp_output.py"
install -m 0644 files/etc/systemd/system/petcam-rtmp.service "$RELEASE_DIR/systemd/petcam-rtmp.service"
install -D -m 0644 files/etc/systemd/system/petcam-rtmp.service "${ROOTFS_DIR}/etc/systemd/system/petcam-rtmp.service"
install -m 0640 files/etc/petcam/rtmp.json "$RELEASE_DIR/etc/petcam/rtmp.json"
install -D -m 0640 files/etc/petcam/rtmp.json "${ROOTFS_DIR}/etc/petcam/rtmp.json"
install -m 0644 files/usr/local/lib/petcam/timelapse.py "$RELEASE_DIR/lib/timelapse.py"
install -m 0644 files/usr/local/lib/petcam/motion_detection.py "$RELEASE_DIR/lib/motion_detection.py"
install -m 0644 files/etc/systemd/system/petcam-motion.service "$RELEASE_DIR/systemd/petcam-motion.service"
install -D -m 0644 files/etc/systemd/system/petcam-motion.service "${ROOTFS_DIR}/etc/systemd/system/petcam-motion.service"
install -m 0644 files/usr/local/lib/petcam/web_manager.py "$RELEASE_DIR/lib/web_manager.py"
install -m 0644 files/usr/local/lib/petcam/web_auth.py "$RELEASE_DIR/lib/web_auth.py"
install -m 0644 files/usr/local/lib/petcam/factory_reset.py "$RELEASE_DIR/lib/factory_reset.py"
install -m 0644 files/usr/local/lib/petcam/health_monitor.py "$RELEASE_DIR/lib/health_monitor.py"
install -m 0644 files/usr/local/lib/petcam/config.py "$RELEASE_DIR/lib/config.py"
install -m 0644 files/usr/local/lib/petcam/network_diagnostics.py "$RELEASE_DIR/lib/network_diagnostics.py"
install -m 0644 files/usr/local/lib/petcam/wifi_metrics.py "$RELEASE_DIR/lib/wifi_metrics.py"
install -m 0644 files/usr/local/lib/petcam/home_assistant.py "$RELEASE_DIR/lib/home_assistant.py"
install -m 0644 files/usr/local/lib/petcam/light_sensor.py "$RELEASE_DIR/lib/light_sensor.py"
install -m 0644 files/etc/systemd/system/petcam-light-sensor.service "$RELEASE_DIR/systemd/petcam-light-sensor.service"
install -D -m 0644 files/etc/systemd/system/petcam-light-sensor.service "${ROOTFS_DIR}/etc/systemd/system/petcam-light-sensor.service"
install -m 0640 files/etc/petcam/light-sensor.conf "$RELEASE_DIR/etc/petcam/light-sensor.conf"
install -D -m 0640 files/etc/petcam/light-sensor.conf "${ROOTFS_DIR}/etc/petcam/light-sensor.conf"
install -m 0644 files/etc/systemd/system/petcam-timelapse.service "$RELEASE_DIR/systemd/petcam-timelapse.service"
install -D -m 0644 files/etc/systemd/system/petcam-timelapse.service "${ROOTFS_DIR}/etc/systemd/system/petcam-timelapse.service"
install -m 0644 files/etc/systemd/system/petcam-home-assistant.service "$RELEASE_DIR/systemd/petcam-home-assistant.service"
install -m 0644 files/etc/petcam/home-assistant.conf "$RELEASE_DIR/etc/petcam/home-assistant.conf"
install -D -m 0644 files/etc/systemd/system/petcam-home-assistant.service "${ROOTFS_DIR}/etc/systemd/system/petcam-home-assistant.service"
install -D -m 0640 files/etc/petcam/home-assistant.conf "${ROOTFS_DIR}/etc/petcam/home-assistant.conf"
install -m 0755 files/usr/local/lib/petcam/updater.py "${ROOTFS_DIR}/usr/local/bin/petcam-update"
printf '{"schema_version":1,"application":"petcam-os","version":"%s","architecture":"armhf","ustreamer_commit":"11fd828ab00c01a39bd0843ef442ef0b6d3cac41"}\n' "$APP_VERSION" > "$RELEASE_DIR/manifest.json"
chmod 0644 "$RELEASE_DIR/manifest.json"
