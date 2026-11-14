# Optional RTMP output

RTMP publishing is an optional, independent output from the existing PetCam camera stream. It is disabled by default.

The publisher reads the existing loopback MJPEG endpoint at `http://127.0.0.1:8081/stream`, encodes H.264 with FFmpeg, and sends an FLV stream to an RTMP or RTMPS destination. It does not open the camera device, create a second capture process, or change the public MJPEG endpoint used by browser preview and OBS.

## Configure in the manager

Open **System → RTMP Output** and configure:

- **Enable RTMP publishing** — starts or stops the separate `petcam-rtmp.service`.
- **RTMP server URL** — the server/application URL, such as `rtmps://stream.example.net/live`. Do not include the stream key in this URL.
- **Stream key** — stored in `/etc/petcam/rtmp.json`; the manager never returns the saved key to the browser. Leave the field blank to retain the existing key.
- **Video encoder** — software H.264 (`libx264`) or hardware H.264 (`h264_v4l2m2m`) when the board's FFmpeg build and kernel expose it.
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
