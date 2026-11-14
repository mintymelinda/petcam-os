# Timelapse capture

PetCam can save periodic JPEG frames without starting a second camera process. The worker reads the first complete JPEG from the existing OBS/MJPEG stream at `http://127.0.0.1:8080/stream`, then closes the connection. It never opens the USB or CSI camera directly.

## Enable and configure

Open the authenticated PetCam manager and use the **Timelapse** section. Timelapse is disabled by default. Settings include:

- Capture interval (60 seconds to 24 hours).
- Daily local-time start/end window, including windows that cross midnight.
- Maximum image width (0 keeps the live stream's native width).
- JPEG quality (0 preserves the source JPEG when no resize is requested; resizing with quality 0 uses quality 95, while 1–95 explicitly re-encodes).
- Retention period, storage cap, and minimum free-space threshold.
- Storage directory, either under `/var/lib/petcam` or on the dedicated network mount `/mnt/petcam-network`.

The worker runs at low scheduling priority and captures only one frame per interval. It does not run while disabled or outside the configured schedule. Resolution and quality changes happen after the frame is read from the live stream; they do not change the live stream settings.

## Storage and recovery

Frames use UTC filenames such as `20261112T143015.123456Z.jpg`. Writes use a temporary file followed by an atomic rename, so interrupted writes are not exposed as completed frames. Retention and storage limits remove the oldest frames first. Captures are skipped when free space is below the configured threshold. A camera disconnect is recorded as a timelapse error and retried on the next interval; the live stream service is independent.

Frames are stored in the configured directory, by default `/var/lib/petcam/timelapse/frames`. To use a network share, mount an SMB/CIFS or NFS export at `/mnt/petcam-network`, then set the storage directory to a folder beneath it in the Timelapse settings. The `petcam` service account must have write permission on the mounted share. The worker verifies that the dedicated mountpoint is an actual CIFS/NFS filesystem before every capture cycle. If the share is disconnected or not mounted at startup, capture pauses and reports `network_storage_unavailable`; it never falls back to the underlying SD-card directory.

### Example: SMB/CIFS share

Install/configure the share on the Pi (SSH or your normal system provisioning workflow):

1. Create `/etc/petcam/smb-credentials` owned by `root:root`, mode `0600`, containing `username=...` and `password=...` (and `domain=...` if required).
2. Add an `/etc/fstab` entry, replacing the server and share names:

   ```fstab
   //nas.example.local/camera /mnt/petcam-network cifs credentials=/etc/petcam/smb-credentials,uid=petcam,gid=petcam,iocharset=utf8,vers=3.0,_netdev,nofail 0 0
   ```

3. Reload systemd and start the mount (`sudo systemctl daemon-reload && sudo mount /mnt/petcam-network`). The timelapse worker starts independently if the share is unavailable and reports the missing mount; it will resume when the share is mounted. Confirm the share is mounted with `findmnt /mnt/petcam-network` and that `sudo -u petcam test -w /mnt/petcam-network` succeeds.
4. In the PetCam manager, set Storage directory to a folder such as `/mnt/petcam-network/petcam/timelapse` and save.

For NFS, mount the export at the same `/mnt/petcam-network` mountpoint and grant the Pi's `petcam` account write access on the export. Do not configure a remote share directly as a `//server/share` or `nfs://` storage path; Linux must mount it first.

## Assemble a video

On a computer with FFmpeg, copy the frames into a working directory and run:

```sh
ffmpeg -framerate 24 -pattern_type glob -i '*.jpg' -c:v libx264 -pix_fmt yuv420p timelapse.mp4
```

The frame rate controls the playback speed, not the capture interval. If frames are missing or the capture interval varies, use a generated concat file with per-frame durations instead of the glob pattern.
