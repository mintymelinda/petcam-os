# Building PetCam OS

## GitHub Actions (recommended)

Open Actions → Build PetCam OS → Run workflow. The build uses the official Raspberry Pi pi-gen project on an Ubuntu GitHub-hosted runner, adds the PetCam stage (including `rpicam-apps-lite` for CSI modules), runs USB and CSI camera-discovery unit tests, and uploads the generated image from pi-gen/deploy as a workflow artifact.

The first build can take a while. The output is a compressed Raspberry Pi OS image suitable for Raspberry Pi Imager.

## Local build

Use a Linux x86-64 host with Docker, Git, and enough free disk space (allow at least 30 GB).

~~~bash
git clone https://github.com/RPi-Distro/pi-gen.git
git clone https://github.com/mintymelinda/petcam-os.git
cp petcam-os/config pi-gen/config
cp -a petcam-os/stage-petcam pi-gen/stage-petcam
touch pi-gen/stage2/SKIP_IMAGES
chmod 0755 pi-gen/stage-petcam/prerun.sh
chmod 0755 pi-gen/stage-petcam/00-install-files/00-run.sh
cd petcam-os
python3 -m unittest discover -s tests -v
cd ../pi-gen
sudo ./build-docker.sh
~~~

The resulting image is written to pi-gen/deploy/. PetCam's application is installed under `/opt/petcam/releases/<version>/`, and `/opt/petcam/current` selects the active release. `/etc/petcam/petcam.conf` and `/var/lib/petcam/` remain outside the versioned application tree so future application updates can preserve device configuration and state.

## Raspberry Pi Imager catalogue

Tagged releases build the image, rename it to a stable versioned asset, generate a Raspberry Pi Imager OS sublist, validate that JSON against Raspberry Pi's published schema, and attach both files to a GitHub Release. The generated entry includes the public HTTPS image URL, compressed and extracted SHA-256 hashes and sizes, supported device tags, `architecture: armhf`, and `init_format: cloudinit-rpi`.

The stable sublist URL intended for the official catalogue submission is:

```
https://github.com/mintymelinda/petcam-os/releases/latest/download/petcam-os-os-list.json
```

Raspberry Pi must approve the catalogue submission before PetCam appears in Imager's default OS list. Until then, the sublist can be loaded manually through App Options → Content Repository → Edit → Use custom URL, but that is a maintainer/testing route, not the intended end-user installation experience.

Once PetCam is listed, users select it from Imager's normal OS list and configure hostname `petcam`, Wi-Fi, WLAN country, an administrator account, and optional SSH in the standard Customisation step. WLAN country is not baked into the image.

The management UI is served over HTTPS on port 8443 and requires a password configured from SSH or a local terminal using `sudo petcam-admin`. Each device generates its own self-signed certificate. Port 8080 is a separate unauthenticated stream-only endpoint for OBS; it exposes only `/stream`, not management or update controls. Keep both ports on a trusted LAN and do not port-forward them to the internet.

## Camera discovery and configuration

At boot, PetCam detects both camera interfaces. USB UVC devices are enumerated through V4L2, parsing advertised formats, discrete/stepwise/continuous frame sizes, and discrete or ranged frame intervals. CSI modules are enumerated with `rpicam-hello --list-cameras`, and the camera stack's advertised sensor modes and frame rates are parsed. The same image supports both interfaces; both expose the same MJPEG HTTP stream endpoint. USB automatic selection prefers native MJPEG. CSI uses `rpicam-vid` to capture and encode MJPEG.

The current starting budgets are pixel-rate heuristics, not benchmark guarantees:

| Board family | Automatic selection budget |
| --- | ---: |
| Pi 1 | 9.2 megapixels/second |
| Pi Zero W, Pi 2 | 27.6 megapixels/second |
| Pi Zero 2 W, Pi 3 | 62.2 megapixels/second |
| Pi 4 / Pi 400 | 124.4 megapixels/second |
| Pi 5 | 248.8 megapixels/second |

For USB, if every native-MJPEG mode exceeds the detected board's budget, PetCam starts with the least demanding advertised MJPEG mode and logs a warning. For CSI, the same budget guides sensor-mode selection. If no CSI mode fits, the least demanding advertised mode is selected with a warning. Explicit supported modes are never blocked.

Inspect USB and CSI camera discovery:

~~~bash
sudo petcam-camera discover
sudo petcam-camera discover-csi
~~~

Preview a supported USB mode:

~~~bash
sudo petcam-camera select --format MJPG --resolution 1920x1080 --fps 30
~~~

Preview a supported CSI mode (camera index is shown by `discover-csi`):

~~~bash
sudo petcam-camera select-csi --camera 0 --resolution 1920x1080 --fps 30
~~~

The same shared parser is used by the CLI, web manager, camera status command, and stream startup script. Validate the active configuration with:

~~~bash
sudo petcam-config validate --check-hardware
~~~

For manual changes, create a separate candidate file and apply it transactionally:

~~~bash
cp /etc/petcam/petcam.conf /tmp/petcam.conf.new
sudo nano /tmp/petcam.conf.new
sudo petcam-config apply /tmp/petcam.conf.new
~~~

The web manager uses the same transaction. It validates the requested mode, stages the configuration, restarts capture, and waits for a real MJPEG frame before committing. If restart or frame readiness fails, it restores the previous file and restarts the previous configuration. The last successful configuration is retained at \`/etc/petcam/petcam.conf.last-good\`. Stream startup validates the file before sourcing it, so malformed lines, unknown keys, and shell expressions are rejected before they can disrupt or execute in the service.

The camera backend listens on loopback port 8081. The HTTPS manager on port 8443 proxies the preview after authentication; a separate HTTP endpoint on port 8080 exposes only the MJPEG stream for OBS.

Supported settings:

- SOURCE: `auto` (prefer USB, fall back to CSI), `usb`, or `csi`
- DEVICE: optional USB V4L2 node; prefer a stable path under /dev/v4l/by-id/ when available
- CAMERA: CSI camera index from `petcam-camera discover-csi`, or `auto`
- FORMAT: USB only; `auto` or a µStreamer-supported V4L2 fourcc such as MJPG, YUYV, or UYVY
- RESOLUTION: `auto` or an advertised mode for the selected backend
- FPS: `auto` or an advertised frame rate for the selected backend
- CSI_QUALITY: CSI MJPEG quality from 1 to 100, default 70
- PORT: internal camera-backend HTTP port, default 8081 (loopback only); manager HTTPS is on 8443 and OBS stream-only HTTP is on 8080

For USB, automatic FPS uses µStreamer's no-application-cap request (`--desired-fps=0`). For CSI, automatic selection chooses an advertised sensor mode within the model's starting pixel-rate budget, or the least demanding advertised mode with a warning if none fit. Unsupported explicit modes fail clearly rather than silently falling back. If a USB camera has no native MJPEG mode, PetCam may select a supported raw V4L2 mode and use CPU JPEG encoding, with a performance warning. CSI is encoded to MJPEG by `rpicam-vid`; this encoder workload can be significant on older Pi boards, so verify sustained capture on the target hardware and reduce resolution, FPS, or `CSI_QUALITY` if needed.

## Runtime diagnostics

~~~bash
sudo petcam-status
systemctl status petcam-stream
journalctl -u petcam-stream -b
~~~

The status report includes systemd service state, a local HTTP endpoint health probe, USB and CSI discovery counts, active backend and camera details, capture resolution and format, latest capture FPS (µStreamer performance log for USB or the CSI frame counter for CSI), a one-second MJPEG bitrate sample measured by opening a temporary local stream client, aggregate network-interface transmit throughput, cumulative transmitted bytes, and SoC temperature. It also includes the periodic stream monitor's state, reason, and recovery count. The web manager runs a lightweight background monitor that checks the backend and public OBS stream every 30 seconds; it waits for a real MJPEG body byte rather than trusting an open socket, distinguishes a missing camera from a stalled capture process, restarts only the affected service, and applies exponential backoff capped at five minutes. The local MJPEG sample is stream-specific; the network-interface rate includes other interface traffic.

Dropped frames are reported as unavailable when the active capture backend does not expose a reliable cumulative counter. PetCam does not estimate or fabricate a dropped-frame count. Capture FPS and service/device errors remain available for diagnosing stream quality.

## Hardware validation

Software discovery only establishes what a camera advertises; it cannot guarantee sustained performance on a specific Pi, USB hub, power supply, or wireless network. Test each intended mode for several minutes while observing petcam-status and the service journal. A powered USB OTG hub is recommended for USB cameras with substantial power draw. CSI modules require a compatible ribbon cable and camera connector; ensure the cable's contacts face the correct direction for the specific board.

## OBS

Use the network stream URL:

~~~
http://petcam.local:8080/stream
~~~

If OBS cannot consume the MJPEG endpoint directly, add it through a VLC Video Source. The authenticated management UI is separate from the OBS stream endpoint. If mDNS does not resolve, substitute the Pi's DHCP address.

Do not expose port 8080 to the public internet.
