# Timelapse capture

PetCam can save periodic JPEG frames without starting a second camera process. The worker reads the first complete JPEG from the existing OBS/MJPEG stream at `http://127.0.0.1:8080/stream`, then closes the connection. It never opens the USB or CSI camera directly.

## Enable and configure

Open the authenticated PetCam manager and use the **Timelapse** section. Timelapse is disabled by default. Settings include:

- Capture interval (60 seconds to 24 hours).
- Daily local-time start/end window, including windows that cross midnight.
- Maximum image width (0 keeps the live stream's native width).
- JPEG quality (0 preserves the source JPEG when no resize is requested; resizing with quality 0 uses quality 95, while 1–95 explicitly re-encodes).
- Retention period, storage cap, and minimum free-space threshold.
- Storage directory, restricted to a location under `/var/lib/petcam`.

The worker runs at low scheduling priority and captures only one frame per interval. It does not run while disabled or outside the configured schedule. Resolution and quality changes happen after the frame is read from the live stream; they do not change the live stream settings.

## Storage and recovery

Frames use UTC filenames such as `20261112T143015.123456Z.jpg`. Writes use a temporary file followed by an atomic rename, so interrupted writes are not exposed as completed frames. Retention and storage limits remove the oldest frames first. Captures are skipped when free space is below the configured threshold. A camera disconnect is recorded as a timelapse error and retried on the next interval; the live stream service is independent.

Frames are stored in the configured directory, by default `/var/lib/petcam/timelapse/frames`. Keep this data directory on storage with enough capacity for the configured retention period.

## Assemble a video

On a computer with FFmpeg, copy the frames into a working directory and run:

```sh
ffmpeg -framerate 24 -pattern_type glob -i '*.jpg' -c:v libx264 -pix_fmt yuv420p timelapse.mp4
```

The frame rate controls the playback speed, not the capture interval. If frames are missing or the capture interval varies, use a generated concat file with per-frame durations instead of the glob pattern.
