# Optional RTMP output

RTMP publishing is an optional, independent output from the existing PetCam camera stream. It is disabled by default. New PetCam images include FFmpeg. On an existing installation that was upgraded without reflashing, install the dependency once with `sudo apt-get update && sudo apt-get install ffmpeg` before enabling RTMP.

For CSI cameras, the publisher receives the H.264 stream produced by the Picamera2 dual-encoder capture session and muxes it into FLV without re-encoding. USB cameras and unsupported CSI configurations use the existing loopback MJPEG endpoint at `http://127.0.0.1:8081/stream` and FFmpeg H.264 encoding. Neither path changes the public MJPEG endpoint used by browser preview and OBS.

## Configure in the manager

Open **System → RTMP Output** and configure:

- **Enable RTMP publishing** — starts or stops the separate `petcam-rtmp.service`.
- **RTMP server URL** — the server/application URL, such as `rtmps://stream.example.net/live`. Do not include the stream key in this URL.
- **Stream key** — stored in `/etc/petcam/rtmp.json`; the manager never returns the saved key to the browser. Leave the field blank to retain the existing key.
- **Video encoder** — used by the MJPEG fallback path. Native CSI publishing uses Picamera2's platform-selected H.264 encoder automatically.
- **Output resolution, FPS, and bitrate** — output is scaled to the selected dimensions while preserving aspect ratio.

Only RTMP and RTMPS URLs are accepted. The stream key is restricted to URL-safe characters. Settings are validated before the service is changed.

## Performance and troubleshooting

RTMP encoding is additional work on the Pi. The service runs separately from capture, with a CPU quota and memory limit, but software H.264 encoding can still reduce available headroom—especially on Raspberry Pi Zero/Zero W and other low-power boards. Start at 640×360 or 854×480, 10–15 FPS, and a modest bitrate on slower hardware. Use the hardware encoder only when it is listed by the installed FFmpeg build.

The System health panel reports RTMP publisher state and FFmpeg's measured output FPS separately from the camera's hardware capture FPS and MJPEG viewer-delivery FPS. A stopped publisher does not stop or interrupt the MJPEG camera feed.

Useful SSH checks:

```bash
sudo systemctl status petcam-rtmp.service
sudo journalctl -u petcam-rtmp.service -n 100 --no-pager
sudo /usr/bin/python3 /opt/petcam/current/lib/rtmp_output.py --check
```

The publisher is not an audio encoder and sends video only. It does not provide a microphone or audio-input path.


## Native CSI H.264 path

When RTMP is enabled for a CSI camera, the camera service switches to Picamera2 and opens the sensor only once. A main-stream MJPEG encoder continues to serve the established HTTP MJPEG endpoint, while a lores H.264 encoder produces the configured RTMP dimensions. Picamera2 sends the encoded H.264 packets through a local MPEG-TS/UDP hop. The RTMP publisher uses FFmpeg stream copy to mux those packets into FLV, avoiding a decode/re-encode cycle.

Picamera2 is installed from Raspberry Pi OS packages. Raspberry Pi 4 and earlier can use dedicated H.264/MJPEG encoder hardware; Raspberry Pi 5 uses FFmpeg software encoders. USB cameras remain on the existing MJPEG-to-FFmpeg path. If Picamera2 cannot start or the selected capture/RTMP frame rates are incompatible, PetCam falls back to the existing MJPEG publisher instead of taking down the camera preview.

Saving RTMP settings restarts the camera capture service to add or remove the secondary encoder. The MJPEG preview and OBS stream URL do not change.
