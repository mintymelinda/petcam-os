# OBS camera output (RTSP)

PetCam can expose its camera as a local-network RTSP feed for OBS. **PetCam does not publish to Twitch, YouTube, or another streaming platform.** OBS remains the final production and publishing application: use it for scenes, overlays, audio, encoding, and destination stream keys.

The OBS feed is disabled by default. Enable it in **System → OBS Output**, save the settings, and add a **Media Source** in OBS using:

```
rtsp://PETCAM_HOST:8554/petcam
```

Replace `PETCAM_HOST` with the PetCam hostname or LAN IP address. The dashboard displays the address using the hostname currently used to open the manager. If OBS runs on another computer, use a hostname or IP address reachable from that computer.

## Video path

For CSI cameras, PetCam uses one Picamera2 camera session with simultaneous encoders:

- MJPEG continues to serve the existing dashboard preview and stable MJPEG URL.
- Native H.264 is published to the local RTSP server without decoding or re-encoding on supported hardware.

USB cameras and unsupported CSI configurations use the MJPEG-to-FFmpeg H.264 fallback. The RTSP endpoint remains the same in either case. Raspberry Pi 4 and earlier can use dedicated H.264/MJPEG encoder hardware; Raspberry Pi 5 uses software encoders through FFmpeg libraries.

MediaMTX provides the RTSP server. PetCam bundles a version-pinned MediaMTX binary for 32-bit armhf images (ARMv6-compatible, including older Pi Zero hardware), with release SHA-256 verification during image creation. The server accepts publishers only from localhost and allows read-only clients on the local network.

## Configure

Open **System → OBS Output**:

- **Enable OBS RTSP feed** — starts or stops the PetCam publisher.
- **OBS Media Source URL** — the address to paste into OBS.
- **Fallback video encoder** — used only for USB cameras or when native CSI H.264 cannot be used.
- **Output resolution, FPS, and bitrate** — controls the output stream. Native CSI mode requires the output resolution not to exceed the selected capture mode and output FPS to divide capture FPS evenly; unsupported combinations fall back to MJPEG transcoding.

No external server URL or stream key is needed in PetCam. Configure your Twitch/YouTube destination and stream key only in OBS.

## OBS setup

1. Confirm the PetCam and OBS computers are on the same trusted LAN.
2. In OBS, add **Sources → + → Media Source**.
3. Disable **Local File**.
4. Paste the RTSP URL shown in PetCam's System tab.
5. Apply the source settings and confirm video appears.
6. Configure audio, scenes, and streaming destination in OBS as usual.

If OBS cannot connect, confirm TCP port **8554** is reachable from the OBS computer and that the PetCam RTSP service is running. Do not expose the RTSP port directly to the public internet.

## Performance and troubleshooting

RTSP publishing is additional work on the Pi. The publisher service has CPU and memory limits. Start with 640×360 or 854×480 at 10–15 FPS on slower hardware. Native H.264 stream copy avoids a second encode when the CSI capture path supports the requested output.

Useful SSH checks:

```bash
sudo systemctl status mediamtx.service
sudo systemctl status petcam-rtmp.service
sudo journalctl -u mediamtx.service -n 100 --no-pager
sudo journalctl -u petcam-rtmp.service -n 100 --no-pager
sudo /usr/bin/python3 /opt/petcam/current/lib/rtmp_output.py --check
```

The feed is video-only. OBS should handle microphone and other audio sources. The existing MJPEG preview and endpoint remain available whether the OBS RTSP feed is enabled or disabled.
