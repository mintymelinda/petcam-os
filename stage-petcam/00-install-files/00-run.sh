#!/bin/bash -e
APP_VERSION="$(tr -d '[:space:]' < files/usr/local/lib/petcam/VERSION)"
if [[ ! "$APP_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Invalid PetCam VERSION: $APP_VERSION" >&2
    exit 1
fi

RELEASE_DIR="${ROOTFS_DIR}/opt/petcam/releases/${APP_VERSION}"
install -D -o root -g root -m 0644 files/etc/petcam/update-signing.pub "${ROOTFS_DIR}/etc/petcam/update-signing.pub"
install -d -m 0755 "$RELEASE_DIR/lib" "$RELEASE_DIR/systemd" "$RELEASE_DIR/etc/petcam"
install -m 0644 files/usr/local/lib/petcam/VERSION "$RELEASE_DIR/VERSION"
install -m 0644 files/usr/local/lib/petcam/camera.py "$RELEASE_DIR/lib/camera.py"
install -m 0644 files/usr/local/lib/petcam/csi_stream.py "$RELEASE_DIR/lib/csi_stream.py"
install -m 0644 files/usr/local/lib/petcam/timelapse.py "$RELEASE_DIR/lib/timelapse.py"
install -m 0644 files/usr/local/lib/petcam/motion_detection.py "$RELEASE_DIR/lib/motion_detection.py"
install -m 0644 files/etc/systemd/system/petcam-motion.service "$RELEASE_DIR/systemd/petcam-motion.service"
install -D -m 0644 files/etc/systemd/system/petcam-motion.service "${ROOTFS_DIR}/etc/systemd/system/petcam-motion.service"
install -m 0644 files/usr/local/lib/petcam/web_manager.py "$RELEASE_DIR/lib/web_manager.py"
install -m 0644 files/usr/local/lib/petcam/web_auth.py "$RELEASE_DIR/lib/web_auth.py"
install -m 0644 files/usr/local/lib/petcam/factory_reset.py "$RELEASE_DIR/lib/factory_reset.py"
install -m 0644 files/usr/local/lib/petcam/health_monitor.py "$RELEASE_DIR/lib/health_monitor.py"
install -m 0644 files/usr/local/lib/petcam/config.py "$RELEASE_DIR/lib/config.py"
install -m 0644 files/usr/local/lib/petcam/camera_profiles.py "$RELEASE_DIR/lib/camera_profiles.py"
install -m 0644 files/usr/local/lib/petcam/home_assistant.py "$RELEASE_DIR/lib/home_assistant.py"
install -m 0644 files/etc/systemd/system/petcam-timelapse.service "$RELEASE_DIR/systemd/petcam-timelapse.service"
install -D -m 0644 files/etc/systemd/system/petcam-timelapse.service "${ROOTFS_DIR}/etc/systemd/system/petcam-timelapse.service"
install -m 0644 files/etc/systemd/system/petcam-home-assistant.service "$RELEASE_DIR/systemd/petcam-home-assistant.service"
install -m 0644 files/etc/petcam/home-assistant.conf "$RELEASE_DIR/etc/petcam/home-assistant.conf"
install -D -m 0644 files/etc/systemd/system/petcam-home-assistant.service "${ROOTFS_DIR}/etc/systemd/system/petcam-home-assistant.service"
install -D -m 0640 files/etc/petcam/home-assistant.conf "${ROOTFS_DIR}/etc/petcam/home-assistant.conf"
install -m 0755 files/usr/local/lib/petcam/updater.py "${ROOTFS_DIR}/usr/local/bin/petcam-update"
printf '{"schema_version":1,"application":"petcam-os","version":"%s","architecture":"armhf","ustreamer_commit":"11fd828ab00c01a39bd0843ef442ef0b6d3cac41"}\n' "$APP_VERSION" > "$RELEASE_DIR/manifest.json"
chmod 0644 "$RELEASE_DIR/manifest.json"
