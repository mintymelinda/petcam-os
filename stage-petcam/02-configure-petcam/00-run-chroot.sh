#!/bin/bash -e
# Runs inside the image root filesystem after packages and stage files are installed.
if ! id petcam >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/petcam --create-home --shell /usr/sbin/nologin petcam
fi
usermod -a -G video,i2c petcam

install -d -m 0755 /opt
git clone --depth 1 https://github.com/pikvm/ustreamer.git /opt/ustreamer
make -C /opt/ustreamer -j1 WITH_SYSTEMD=0 WITH_GPIO=0
make -C /opt/ustreamer install PREFIX=/usr/local
ldconfig

cat > /usr/local/bin/petcam-camera <<'SCRIPT'
#!/bin/sh
exec python3 /usr/local/lib/petcam/camera.py "$@"
SCRIPT
cat > /usr/local/bin/petcam-status <<'SCRIPT'
#!/bin/sh
exec python3 /usr/local/lib/petcam/camera.py status
SCRIPT
chmod 0755 /usr/local/bin/petcam-camera /usr/local/bin/petcam-status

install -d -m 0755 /usr/local/lib/petcam /etc/petcam
cat > /usr/local/lib/petcam/start-stream.sh <<'SCRIPT'
#!/bin/bash
set -euo pipefail
CONFIG=/etc/petcam/petcam.conf
if [[ -r "$CONFIG" ]]; then
    # shellcheck disable=SC1090
    source "$CONFIG"
fi
PORT="\${PORT:-8080}"
SOURCE="\${SOURCE:-auto}"
DEVICE="\${DEVICE:-}"
CAMERA="\${CAMERA:-auto}"
FORMAT="\${FORMAT:-auto}"
RESOLUTION="\${RESOLUTION:-auto}"
FPS="\${FPS:-auto}"
CSI_QUALITY="\${CSI_QUALITY:-70}"

start_usb() {
    args=(select --format "$FORMAT" --resolution "$RESOLUTION" --fps "$FPS")
    if [[ -n "$DEVICE" ]]; then args+=(--device "$DEVICE"); fi
    selection="$(/usr/local/bin/petcam-camera "\${args[@]}")"
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
    exec /usr/local/bin/ustreamer --device="$camera" --host=0.0.0.0 --port="$PORT" --format="$format" --encoder="$encoder" --resolution="$resolution" --desired-fps="$fps" --perf
}

start_csi() {
    args=(select-csi --resolution "$RESOLUTION" --fps "$FPS")
    if [[ "$CAMERA" != "auto" ]]; then args+=(--camera "$CAMERA"); fi
    selection="$(/usr/local/bin/petcam-camera "\${args[@]}")"
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
    exec python3 /usr/local/lib/petcam/csi_stream.py --camera "$camera" --resolution "$resolution" --fps "$fps" --quality "$CSI_QUALITY" --host 0.0.0.0 --port "$PORT"
}

case "$SOURCE" in
    usb) start_usb ;;
    csi) start_csi ;;
    auto)
        if [[ -z "$DEVICE" ]] && /usr/local/bin/petcam-camera select --format "$FORMAT" --resolution "$RESOLUTION" --fps "$FPS" >/dev/null 2>&1; then
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
chmod 0755 /usr/local/lib/petcam/start-stream.sh

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
PORT=8080
CONF
chmod 0644 /etc/petcam/petcam.conf

cat > /etc/systemd/system/petcam-stream.service <<'UNIT'
[Unit]
Description=PetCam USB UVC or Raspberry Pi CSI camera streamer
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=petcam
Group=video
SupplementaryGroups=video
ExecStart=/usr/local/lib/petcam/start-stream.sh
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

systemctl enable avahi-daemon.service
systemctl enable petcam-stream.service
