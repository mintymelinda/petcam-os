# PetCam OS

A minimal, reproducible Raspberry Pi OS image for Raspberry Pi boards, USB UVC webcams, and Raspberry Pi CSI camera modules. It serves camera video over the local network for OBS through one consistent MJPEG endpoint.

## Supported hardware

- Raspberry Pi Zero W (initial low-resource target); automatic hardware profiles also recognize Zero 2 W, Pi 1/2/3, Pi 4/400, and Pi 5.
- USB UVC webcams connected through the board's USB host/OTG port. A powered hub is recommended, especially for bus-powered webcams.
- Raspberry Pi CSI camera modules supported by the Raspberry Pi camera stack, including OV5647 (Camera Module v1), IMX219/IMX355 (v2 sensor revisions), IMX708 (v3), IMX477 (HQ), and other sensors supported by the installed `libcamera` pipeline.
- USB and CSI cameras share the same image and stream URL; no separate image is needed.
- OBS on another computer on the same LAN.

USB camera modes are discovered from V4L2. CSI camera modules are discovered through `rpicam-hello --list-cameras`, with sensor modes and frame rates reported by the Raspberry Pi camera stack.

## Image build

The image is built with the official [Raspberry Pi pi-gen](https://github.com/RPi-Distro/pi-gen) tool. The build host uses Docker; Docker is not installed on the Pi.

The workflow builds a 32-bit Raspberry Pi OS Lite image and adds PetCam's camera streamer service. PetCam application files are installed into a versioned release under `/opt/petcam/releases/`, with `/opt/petcam/current` pointing to the active release. The flashable image is published by GitHub Actions.

## Camera stream

PetCam selects the appropriate capture backend automatically. USB UVC cameras use [µStreamer](https://github.com/pikvm/ustreamer), preferring native MJPEG passthrough and falling back to CPU JPEG encoding for supported raw formats when needed. CSI modules use Raspberry Pi's `rpicam-vid` camera stack with MJPEG output. Both backends expose the same `/stream` endpoint internally; the authenticated manager is served separately over HTTPS on port 8443, while port 8080 is reserved for the public OBS stream endpoint.

- Stream endpoint (OBS): http://petcam.local:8080/stream
- Authenticated management page: https://petcam.local:8443/

If mDNS is unavailable, use the Pi's DHCP address instead.

### Web camera management

Open https://petcam.local:8443/ to manage the active camera. The manager uses a per-device self-signed HTTPS certificate; your browser will show a certificate warning until you explicitly trust that device certificate. The lightweight built-in page is served by Python's standard library; it adds no Node.js runtime, frontend framework, database, or background camera polling. Use **Refresh cameras** after plugging in or removing a camera, select an advertised mode, preview it, and apply it. The selected USB/CSI camera and mode are saved in /etc/petcam/petcam.conf and survive reboots. Applying a selection briefly restarts the capture backend; the public stream URL remains unchanged. The capture backend listens only on loopback, while the management interface proxies the stream and exposes the camera controls.

Camera discovery runs when the page is opened or refreshed, not continuously in the background. The page is intentionally small to conserve memory and CPU on the original Zero W.

### Discover and choose camera modes

~~~bash
sudo petcam-camera discover
sudo petcam-camera discover-csi
sudo petcam-camera select --format MJPG --resolution 1920x1080 --fps 30
sudo petcam-camera select-csi --camera 0 --resolution 1920x1080 --fps 30
sudo petcam-status
~~~

discover reports USB V4L2 devices and detected CSI modules. discover-csi reports CSI sensor modes. Use select for USB and select-csi for CSI to preview a chosen configuration. Automatic mode prefers MJPEG and uses a conservative starting pixel-throughput budget for the detected Pi model. Explicit camera-supported modes are not blocked; a warning is emitted when a selection exceeds the model's automatic starting budget.

Edit /etc/petcam/petcam.conf to override:

~~~bash
SOURCE=auto          # auto, usb, or csi
# USB only: pin a specific V4L2 node
# DEVICE=/dev/v4l/by-id/your-camera-video-index0
# CSI only: camera index from petcam-camera discover-csi
CAMERA=0
FORMAT=MJPG           # USB only
RESOLUTION=1920x1080
FPS=30
CSI_QUALITY=70        # CSI MJPEG quality, 1..100
PORT=8081           # internal camera backend; manager HTTPS is on 8443, OBS stream on 8080
~~~

SOURCE=auto prefers a usable USB camera, then falls back to CSI. Set SOURCE=usb or SOURCE=csi to force a backend. FORMAT applies to USB only; CSI output is MJPEG. Use RESOLUTION=auto and FPS=auto to return to automatic selection. For USB, FPS=auto asks µStreamer not to impose an application-side frame-rate cap. For CSI, automatic selection chooses a sensor-advertised frame rate. Unsupported explicit modes fail clearly rather than silently falling back. Restart after changes:

~~~bash
sudo systemctl restart petcam-stream
~~~

### Runtime diagnostics

sudo petcam-status reports service and HTTP endpoint health, USB/CSI detection counts, active backend and camera, selected resolution and format, latest capture FPS (from µStreamer or the CSI capture process), a one-second local MJPEG stream bitrate sample, aggregate network-interface transmit throughput, cumulative transmitted bytes, and SoC temperature. It also reports the periodic stream monitor's state, failure reason, and recovery count. The monitor runs every 30 seconds, allows a startup grace period, waits up to 15 seconds for an actual backend frame, checks the public OBS proxy separately, and uses bounded restart backoff. A missing camera is reported as disconnected and is not treated as a service crash. The local stream sample opens one temporary loopback viewer; interface throughput is aggregate traffic, not video-only.

The current capture interfaces do not provide a reliable cumulative dropped-frame counter across all supported USB and CSI drivers. PetCam reports this as unavailable rather than inventing a value. Compare reported capture FPS with the selected mode's advertised frame interval and inspect the service journal for device/stream errors.

## Application updates

The image includes a signed, root-owned application updater that stages versioned releases, atomically switches the active release, restarts PetCam services, and restores the previous release if the web manager fails its health check. It does not update Raspberry Pi OS, firmware, or the boot partition. See [docs/update-engine.md](docs/update-engine.md) for the release-signing contract and operational details.

New images pin the project's Ed25519 update-verification public key and check the latest signed application manifest directly from GitHub Releases. Each tagged release automatically publishes a versioned application archive, SHA-256 manifest, and detached Ed25519 signature alongside the flashable image. The updater verifies the signature and archive before installation, and rolls back if the web manager fails its health check. Raspberry Pi Imager's catalogue is not involved in update discovery or installation. Existing installations need the one-time public-key/configuration bootstrap documented in [docs/update-engine.md](docs/update-engine.md). The web manager provides manual update checks, release details, install confirmation, and progress/status reporting; automatic checks are an optional browser-local preference, and updates are never installed automatically.

## First boot and Wi-Fi

PetCam OS is being prepared for inclusion in the official Raspberry Pi Imager catalogue. The release pipeline generates an Imager OS sublist using Raspberry Pi's published schema, public HTTPS release-asset URLs, verified compressed and extracted SHA-256 hashes, supported device tags, and `init_format: cloudinit-rpi`. The catalogue submission is pending Raspberry Pi review; until accepted, PetCam OS will not appear automatically in Imager's default OS list.

Once listed, select PetCam OS in Raspberry Pi Imager and use its normal Customisation step to set hostname `petcam`, configure Wi-Fi, select the WLAN regulatory country where the Pi will operate, create the administrator account, and enable SSH if desired. The image intentionally has no hard-coded Wi-Fi country. Imager applies these settings on first boot. To change the country later, use sudo raspi-config → Localisation Options → WLAN Country.

Each versioned GitHub Release contains the compressed image and its generated `petcam-os-os-list.json` sublist. The stable catalogue sublist URL for submission is `https://github.com/mintymelinda/petcam-os/releases/latest/download/petcam-os-os-list.json`.

## Build locally

See docs/building.md. The GitHub Actions workflow is the recommended reproducible build path.

## Validation status

Camera mode discovery and automatic selection are software features; they do not prove a mode is sustainable on a particular Pi, USB hub, camera, or network. Run a sustained hardware test on the actual combination before treating a high-resolution/high-FPS mode as reliable.

## Security

The web manager listens on HTTPS port 8443 and requires a password for camera settings and software-update controls. Set the initial password over SSH or a local terminal with `sudo petcam-admin`; password hashes use PBKDF2-HMAC-SHA256 and are stored in `/etc/petcam/web-auth.json`. The management certificate and private key are generated uniquely on each device. Sessions are HTTP-only, Secure, SameSite=Strict, expire after eight hours, and are invalidated when the password changes or the service restarts.

Port 8080 is a separate unauthenticated HTTP stream-only endpoint for OBS compatibility; it serves only `/stream` and no management routes. The camera backend listens only on loopback at port 8081. Keep the device on a trusted private network; do not port-forward either public port to the internet.
