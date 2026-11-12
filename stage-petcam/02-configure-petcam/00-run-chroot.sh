#!/bin/bash -e
# Runs inside the image root filesystem after packages and stage files are installed.
groupadd --system --force petcam
if ! id petcam >/dev/null 2>&1; then
    useradd --system --gid petcam --home-dir /var/lib/petcam --create-home --shell /usr/sbin/nologin petcam
fi
usermod -g petcam -a -G video,i2c petcam

APP_VERSION="$(cat /opt/petcam/releases/*/VERSION)"
install -d -m 0755 /opt
git init /opt/ustreamer
git -C /opt/ustreamer fetch --depth 1 https://github.com/pikvm/ustreamer.git 11fd828ab00c01a39bd0843ef442ef0b6d3cac41
git -C /opt/ustreamer checkout --detach FETCH_HEAD
make -C /opt/ustreamer -j1 WITH_SYSTEMD=0 WITH_GPIO=0
make -C /opt/ustreamer install PREFIX="/opt/petcam/releases/$APP_VERSION/vendor"
rm -rf /opt/ustreamer
ln -sfn "releases/$APP_VERSION" /opt/petcam/current

cat > /usr/local/bin/petcam-camera <<'SCRIPT'
#!/bin/sh
exec python3 /opt/petcam/current/lib/camera.py "$@"
SCRIPT
cat > /usr/local/bin/petcam-status <<'SCRIPT'
#!/bin/sh
exec python3 /opt/petcam/current/lib/camera.py status
SCRIPT
cat > /usr/local/bin/petcam-admin <<'SCRIPT'
#!/bin/sh
exec python3 /opt/petcam/current/lib/web_manager.py --set-password
SCRIPT
cat > /usr/local/bin/petcam-config <<'SCRIPT'
#!/bin/sh
exec python3 /opt/petcam/current/lib/config.py "$@"
SCRIPT
cat > /usr/local/sbin/petcam-stream-service <<'SCRIPT'
#!/bin/sh
set -eu
action="\${1:-}"
profile="\${2:-}"
case "$action" in start|stop|restart|enable|disable) ;; *) echo "Usage: petcam-stream-service start|stop|restart|enable|disable PROFILE" >&2; exit 2 ;; esac
case "$profile" in ""|*[!a-z0-9-]*) echo "Invalid camera profile identifier." >&2; exit 2 ;; esac
[ -f "/etc/petcam/cameras.d/$profile.conf" ] || { echo "Camera profile does not exist." >&2; exit 1; }
if [ "$action" = enable ]; then exec /usr/bin/systemctl enable --now "petcam-stream@$profile.service"; fi
if [ "$action" = disable ]; then exec /usr/bin/systemctl disable --now "petcam-stream@$profile.service"; fi
exec /usr/bin/systemctl "$action" "petcam-stream@$profile.service"
SCRIPT
chmod 0755 /usr/local/sbin/petcam-stream-service
cat > /usr/local/bin/petcam-diagnostics <<'SCRIPT'
#!/bin/sh
exec python3 /opt/petcam/current/lib/network_diagnostics.py "$@"
SCRIPT
chmod 0755 /usr/local/bin/petcam-camera /usr/local/bin/petcam-status /usr/local/bin/petcam-admin /usr/local/bin/petcam-config /usr/local/bin/petcam-diagnostics

install -d -m 0755 "/opt/petcam/releases/$APP_VERSION/lib" /etc/petcam

cat > "/opt/petcam/releases/$APP_VERSION/lib/start-stream.sh" <<'SCRIPT'
#!/bin/bash
set -euo pipefail
CONFIG="${PETCAM_CONFIG_PATH:-/etc/petcam/petcam.conf}"
python3 /opt/petcam/current/lib/config.py validate --check-hardware
if [[ -r "$CONFIG" ]]; then
    # shellcheck disable=SC1090
    source "$CONFIG"
fi
PORT="${PORT:-8081}"
SOURCE="${SOURCE:-auto}"
DEVICE="${DEVICE:-}"
CAMERA="${CAMERA:-auto}"
FORMAT="${FORMAT:-auto}"
RESOLUTION="${RESOLUTION:-auto}"
FPS="${FPS:-auto}"
CSI_QUALITY="${CSI_QUALITY:-70}"

start_usb() {
    args=(select --format "$FORMAT" --resolution "$RESOLUTION" --fps "$FPS")
    if [[ -n "$DEVICE" ]]; then args+=(--device "$DEVICE"); fi
    selection="$(/usr/local/bin/petcam-camera "${args[@]}")"
    json_value() {
        python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1" <<< "$selection"
    }
    camera="$(json_value device)"
    resolution="$(json_value resolution)"
    fps="$(json_value fps)"
    format="$(json_value format)"
    encoder="$(json_value encoder)"
    native_mjpeg="$(json_value native_mjpeg)"
    warning="$(json_value performance_warning)"
    tier="$(json_value tier)"
    if [[ "$native_mjpeg" != "True" && "$native_mjpeg" != "true" ]]; then
        echo "WARNING: USB camera has no native MJPEG mode; CPU JPEG encoding will be used." >&2
    fi
    if [[ "$warning" == "True" || "$warning" == "true" ]]; then
        echo "WARNING: selected USB mode exceeds the automatic performance budget or requires CPU encoding." >&2
    fi
    echo "PetCam backend: usb; hardware tier: $tier"
    echo "Starting USB camera $camera at $resolution, format=$format, requested_fps=$fps, encoder=$encoder"
    exec /opt/petcam/current/vendor/bin/ustreamer --device="$camera" --host=127.0.0.1 --port="$PORT" --format="$format" --encoder="$encoder" --resolution="$resolution" --desired-fps="$fps" --perf
}

start_csi() {
    args=(select-csi --resolution "$RESOLUTION" --fps "$FPS")
    if [[ -n "${PETCAM_PROFILE_ID:-}" ]]; then
        profile_device_path="$(python3 -c 'import json,sys; print(next(p for p in json.load(open("/etc/petcam/cameras.json", encoding="utf-8")) if p["id"] == sys.argv[1]).get("device_path", ""))' "$PETCAM_PROFILE_ID")"
        if [[ -n "$profile_device_path" ]]; then
            args+=(--device-path "$profile_device_path")
        elif [[ "$CAMERA" != "auto" ]]; then
            args+=(--camera "$CAMERA")
        fi
    elif [[ "$CAMERA" != "auto" ]]; then
        args+=(--camera "$CAMERA")
    fi
    selection="$(/usr/local/bin/petcam-camera "${args[@]}")"
    json_value() {
        python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1" <<< "$selection"
    }
    camera="$(json_value camera)"
    resolution="$(json_value resolution)"
    fps="$(json_value fps)"
    warning="$(json_value performance_warning)"
    tier="$(json_value tier)"
    if [[ "$warning" == "True" || "$warning" == "true" ]]; then
        echo "WARNING: selected CSI mode exceeds the automatic performance budget." >&2
    fi
    echo "PetCam backend: csi; hardware tier: $tier"
    echo "Starting CSI camera index=$camera at $resolution, requested_fps=$fps, MJPEG quality=$CSI_QUALITY"
    exec python3 /opt/petcam/current/lib/csi_stream.py --camera "$camera" --resolution "$resolution" --fps "$fps" --quality "$CSI_QUALITY" --host 127.0.0.1 --port "$PORT"
}

case "$SOURCE" in
    usb) start_usb ;;
    csi) start_csi ;;
    auto)
        usb_probe=(select --format "$FORMAT" --resolution "$RESOLUTION" --fps "$FPS")
        if [[ -n "$DEVICE" ]]; then usb_probe+=(--device "$DEVICE"); fi
        if /usr/local/bin/petcam-camera "${usb_probe[@]}" >/dev/null 2>&1; then
            start_usb
        else
            echo "No usable USB camera selected; trying Raspberry Pi CSI camera stack."
            start_csi
        fi
        ;;
    *)
        echo "SOURCE must be auto, usb, or csi." >&2
        exit 2
        ;;
esac
SCRIPT
chmod 0755 "/opt/petcam/releases/$APP_VERSION/lib/start-stream.sh"

cat > /etc/petcam/petcam.conf <<'CONF'
# SOURCE=auto selects USB when available, then falls back to CSI; force usb or csi if preferred.
SOURCE=auto
# USB camera path override, preferably /dev/v4l/by-id/...
DEVICE=
# CSI camera index from petcam-camera discover-csi
CAMERA=auto
FORMAT=auto
RESOLUTION=auto
FPS=auto
CSI_QUALITY=70
PORT=8081
CONF
# The sticky directory permits atomic replacement of petcam-owned config files
# without allowing the service account to replace root-owned trust or TLS files.
chown root:petcam /etc/petcam
chmod 1770 /etc/petcam
chown petcam:petcam /etc/petcam/petcam.conf
chmod 0640 /etc/petcam/petcam.conf

cat > /etc/petcam/update.conf <<'UPDATE'
# Stable GitHub Releases endpoint; independent of the Raspberry Pi Imager catalogue.
MANIFEST_URL=https://github.com/mintymelinda/petcam-os/releases/latest/download/petcam-update-manifest.json
PUBLIC_KEY=/etc/petcam/update-signing.pub
UPDATE
chown root:root /etc/petcam/update.conf
chmod 0600 /etc/petcam/update.conf
install -d -o root -g root -m 0755 /var/lib/petcam/update
install -d -o root -g root -m 0755 /var/lib/petcam/update/staging

cat > /etc/systemd/system/petcam-update.service <<'UNIT'
[Unit]
Description=Install a verified PetCam application release
Documentation=https://github.com/mintymelinda/petcam-os
After=network-online.target
Wants=network-online.target
ConditionPathExists=/etc/petcam/update.conf

[Service]
Type=oneshot
User=root
Group=root
ExecStart=/usr/local/bin/petcam-update install
TimeoutStartSec=180
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/opt/petcam /var/lib/petcam/update
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/petcam-stream.service <<'UNIT'
[Unit]
Description=PetCam USB UVC or Raspberry Pi CSI camera streamer
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=petcam
Group=video
SupplementaryGroups=video i2c
ExecStart=/opt/petcam/current/lib/start-stream.sh
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/petcam

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable avahi-daemon.service
systemctl enable petcam-stream.service

cat > /etc/sudoers.d/petcam-web <<'SUDO'
petcam ALL=(root) NOPASSWD: /usr/bin/systemctl restart petcam-stream.service, /usr/local/sbin/petcam-stream-service, /usr/local/bin/petcam-update check, /usr/bin/systemctl start petcam-update.service, /usr/bin/journalctl -u petcam-update.service -n 20 --no-pager -o cat
SUDO
chmod 0440 /etc/sudoers.d/petcam-web
visudo -cf /etc/sudoers.d/petcam-web

cat > /usr/local/sbin/petcam-create-web-cert <<'SCRIPT'
#!/bin/sh
set -eu
if [ -s /etc/petcam/web-manager.crt ] && [ -s /etc/petcam/web-manager.key ]; then
    exit 0
fi
umask 077
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -sha256 -nodes -days 825 \
    -keyout /etc/petcam/web-manager.key -out /etc/petcam/web-manager.crt \
    -subj "/CN=petcam.local" -addext "subjectAltName=DNS:petcam.local,DNS:petcam"
chown root:petcam /etc/petcam/web-manager.key /etc/petcam/web-manager.crt
chmod 0640 /etc/petcam/web-manager.key
chmod 0644 /etc/petcam/web-manager.crt
SCRIPT
chmod 0755 /usr/local/sbin/petcam-create-web-cert

cat > /etc/systemd/system/petcam-web-cert.service <<'UNIT'
[Unit]
Description=Create a unique PetCam HTTPS certificate on first boot
Before=petcam-web.service

[Service]
Type=oneshot
User=root
ExecStart=/usr/local/sbin/petcam-create-web-cert
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/petcam-stream@.service <<'UNIT'
[Unit]
Description=PetCam independent camera stream %i
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=petcam
Group=video
SupplementaryGroups=video i2c
Environment=PETCAM_CONFIG_PATH=/etc/petcam/cameras.d/%i.conf
Environment=PETCAM_PROFILE_ID=%i
ExecStart=/opt/petcam/current/lib/start-stream.sh
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/petcam

[Install]
WantedBy=multi-user.target
UNIT

install -d -o petcam -g petcam -m 0750 /etc/petcam/cameras.d

cat > /etc/systemd/system/petcam-web.service <<'UNIT'
[Unit]
Description=PetCam lightweight camera management web interface
Requires=petcam-web-cert.service
After=network-online.target petcam-stream.service petcam-web-cert.service
Wants=network-online.target

[Service]
Type=simple
User=petcam
Group=petcam
SupplementaryGroups=video i2c
ExecStart=/usr/bin/python3 /opt/petcam/current/lib/web_manager.py
Restart=always
RestartSec=3
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/etc/petcam /var/lib/petcam

[Install]
WantedBy=multi-user.target
UNIT
systemctl enable avahi-daemon.service
systemctl enable petcam-stream.service
systemctl enable petcam-web-cert.service
systemctl enable petcam-web.service
