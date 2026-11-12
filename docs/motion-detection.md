# Motion detection and event snapshots

Motion detection is an opt-in feature. It is disabled on first boot and after upgrades unless the owner explicitly enables it in the authenticated PetCam manager.

## How it works

The worker reads the existing OBS-facing MJPEG stream at `http://127.0.0.1:8080/stream`. It does not open the camera device, create another camera process, or record continuous video. It samples one JPEG at the configured interval, reduces it to a 160×120 grayscale image, and compares it with the previous sample using Pillow's image-difference operations.

A motion candidate must exceed the minimum changed-pixel percentage for the configured number of consecutive samples. The worker then saves the current full-resolution JPEG as an event snapshot. A cooldown prevents repeated events from the same activity. Quiet hours suppress event generation; they do not stop the live stream or timelapse.

The worker is deliberately constrained by systemd: low scheduling priority, 10% CPU quota, 64 MiB maximum memory, and idle I/O priority. The feature remains disabled by default on every supported board until real-device benchmarks justify any default change.

## Settings

Open **Motion detection** in the authenticated PetCam manager.

- **Sensitivity:** adjusts the per-pixel difference threshold. Higher values detect smaller changes.
- **Minimum changed pixels:** ignores changes affecting less than this percentage of the reduced image.
- **Sampling interval:** 1–30 seconds between image comparisons.
- **Consecutive samples:** requires 1–5 repeated motion candidates before creating an event.
- **Minimum time between events:** 5 seconds to 24 hours.
- **Quiet hours:** optional local-time suppression window; overnight windows are supported.
- **Retention:** expired snapshots and their event-history entries are removed together.
- **Maximum storage / minimum free space:** oldest event images are pruned to the cap; new snapshots are skipped below the free-space floor.

Event images are timestamped UTC JPEGs under `/var/lib/petcam/motion/events`. The authenticated manager displays the latest 20 events. The JSON event index is atomically replaced and bounded by the configured event-history limit.

## Pi Zero W benchmark

The worker exposes a real-stream benchmark. Run this on the Pi Zero W with the camera and normal live stream active, first with motion disabled and then while the worker is enabled:

```sh
sudo -u petcam /usr/bin/python3 /opt/petcam/current/lib/motion_detection.py --benchmark-seconds 300 --sample-interval 2
```

The JSON result reports sample count, elapsed time, process CPU percentage, and peak resident memory. Run it more than once under representative camera resolution and lighting. Compare the stream's frame rate and `petcam-health` state with the worker disabled and enabled; also inspect:

```sh
systemctl show petcam-motion.service -p CPUUsageNSec -p MemoryPeak -p CPUQuotaPerSecUSec
journalctl -u petcam-motion.service --since "10 minutes ago"
```

A proposed acceptance target for Pi Zero W is no sustained stream-health degradation, average worker CPU below 5% of one core during steady monitoring, and peak RSS below 64 MiB. The service hard limits are stricter than those targets (10% CPU and 64 MiB RAM). **The CI suite does not emulate a Pi Zero W, so this hardware benchmark must be run on the actual board before considering any default-on policy.** Until then, motion remains opt-in.

## Failure handling

A camera disconnect updates worker status and causes a bounded reconnect delay; it does not restart or stop the live-stream service. Low disk space skips new snapshots. Atomic image writes prevent partial JPEGs from appearing in event history, and startup pruning removes abandoned temporary frames. No video recording is performed.
