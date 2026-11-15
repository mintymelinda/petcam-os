# OBS camera output

PetCam exposes two local-network sources for OBS. It does not publish to Twitch, YouTube, or another streaming platform; OBS remains responsible for scenes, audio, encoding, and destination stream keys.

## HTTP Browser Source

Use the page URL as an OBS Browser Source:

```
http://PETCAM_HOST:8080/
```

The page contains a transparent, full-size image element backed by the existing MJPEG HTTP stream. PetCam forwards the MJPEG multipart response unchanged. There is no dashboard preview connection and no additional camera capture process. This source is available while the camera is operating in MJPEG mode. Selecting an advertised USB H.264 mode disables the HTTP Browser Source and forces RTSP output on. The manager hides the Browser Source links while H.264 is active or selected, and the HTTP endpoint returns 503 with instructions to use RTSP. Timelapse and motion detection must be disabled before applying H.264 because both consume MJPEG frames.

## RTSP Media Source

For MJPEG capture, enable **System → OBS Output → Enable OBS RTSP feed** to publish the MJPEG packet-copy path. Selecting a USB H.264 mode automatically enables and locks RTSP output on. Add this address as an OBS Media Source:

```
rtsp://PETCAM_HOST:8554/petcam
```

When H.264 is selected, PetCam captures the advertised V4L2 H.264 mode and packet-copies the MPEG-TS transport stream into RTSP. Otherwise, the MJPEG path packet-copies the original JPEG frames into RTSP. Neither path decodes, scales, changes frame rate, or re-encodes video. Output controls are limited to enable/disable because the camera's advertised capture mode determines the stream format.

## MJPEG-only features

Timelapse and motion detection continue to consume the MJPEG capture stream. They are not moved to H.264 and do not start a second camera capture session. Both features must remain disabled while H.264 capture is selected; PetCam rejects attempts to enable either one until the camera is returned to MJPEG mode.

## Network and troubleshooting

The HTTP Browser Source listener is unauthenticated and intended for a trusted local network. Do not expose port 8080 to the public internet. The RTSP server accepts publishers only from localhost and permits read-only clients on the LAN; do not expose port 8554 publicly.

Useful SSH checks:

```bash
sudo systemctl status petcam-stream.service
sudo systemctl status petcam-rtmp.service
sudo journalctl -u petcam-rtmp.service -n 100 --no-pager
sudo /usr/bin/python3 /opt/petcam/current/lib/rtmp_output.py --check
```

The feed is video-only. Add microphones and other audio sources in OBS.
