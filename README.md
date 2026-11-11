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

PetCam selects the appropriate capture backend automatically. USB UVC cameras use [µStreamer](https://github.com/pikvm/ustreamer), preferring native MJPEG passthrough and falling back to CPU JPEG encoding for supported raw formats when needed. CSI modules use Raspberry Pi's `rpicam-vid` camera stack with MJPEG output. Both backends expose the same `/stream` endpoint internally; the PetCam web interface provides the public port 8080 page and stream proxy.

- Stream endpoint: http://petcam.local:8080/stream
- Stream viewer/status page: http://petcam.local:8080/

If mDNS is unavailable, use the Pi's DHCP address instead.

### Web camera management

Open http://petcam.local:8080/ to view the live stream and manage the active camera. The lightweight built-in page is served by Python's standard library; it adds no Node.js runtime, frontend framework, database, or background camera polling. Use **Refresh cameras** after plugging in or removing a camera, select an advertised mode, preview it, and apply it. The selected USB/CSI camera and mode are saved in /etc/petcam/petcam.conf and survive reboots. Applying a selection briefly restarts the capture backend; the public stream URL remains unchanged. The capture backend listens only on loopback, while the management interface proxies the stream and exposes the camera controls.

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
PORT=8081           # internal camera backend; web interface remains on port 8080
~~~

SOURCE=auto prefers a usable USB camera, then falls back to CSI. Set SOURCE=usb or SOURCE=csi to force a backend. FORMAT applies to USB only; CSI output is MJPEG. Use RESOLUTION=auto and FPS=auto to return to automatic selection. For USB, FPS=auto asks µStreamer not to impose an application-side frame-rate cap. For CSI, automatic selection chooses a sensor-advertised frame rate. Unsupported explicit modes fail clearly rather than silently falling back. Restart after changes:

~~~bash
sudo systemctl restart petcam-stream
~~~

### Runtime diagnostics

sudo petcam-status reports service and HTTP endpoint health, USB/CSI detection counts, active backend and camera, selected resolution and format, latest capture FPS (from µStreamer or the CSI capture process), a one-second local MJPEG stream bitrate sample, aggregate network-interface transmit throughput, cumulative transmitted bytes, and SoC temperature. The local stream sample opens one temporary loopback viewer; interface throughput is aggregate traffic, not video-only.

The current capture interfaces do not provide a reliable cumulative dropped-frame counter across all supported USB and CSI drivers. PetCam reports this as unavailable rather than inventing a value. Compare reported capture FPS with the selected mode's advertised frame interval and inspect the service journal for device/stream errors.

## First boot and Wi-Fi

Raspberry Pi Imager 2 treats a locally selected image as having no customization support. The build artifact therefore includes `create_imager_manifest.py` alongside the compressed image. After extracting the artifact, double-click `prepare-imager.bat` on Windows. On Linux, run `./prepare-imager.sh` from the extracted folder. The helper finds the image, generates the local manifest with verified checksums, and opens it in Raspberry Pi Imager 2.

This creates `petcam-os.rpi-imager-manifest` with the image's local path, verified compressed and extracted SHA-256 hashes, supported Pi device tags, and `init_format: cloudinit-rpi`. Double-click the manifest to open it in Raspberry Pi Imager 2 (or choose it through App Options → Content Repository → Edit → Use custom file, then Apply & Restart). Select PetCam OS from the OS list; the Customisation step will then be available.

In Customisation, set hostname `petcam`, configure Wi-Fi, select the WLAN regulatory country where the Pi will operate, create the administrator account, and enable SSH if desired. The image intentionally has no hard-coded Wi-Fi country. Imager applies these settings on first boot. To change the country later, use sudo raspi-config → Localisation Options → WLAN Country.

The helper uses only Python's standard library and validates the exact image bytes through the manifest checksums. Keep the image and generated manifest together until Imager has finished writing.

## Build locally

See docs/building.md. The GitHub Actions workflow is the recommended reproducible build path.

## Validation status

Camera mode discovery and automatic selection are software features; they do not prove a mode is sustainable on a particular Pi, USB hub, camera, or network. Run a sustained hardware test on the actual combination before treating a high-resolution/high-FPS mode as reliable.

## Security

The web manager exposes the public stream and management page on port 8080. The camera backend listens only on loopback at port 8081. Keep the device on a trusted private network; do not port-forward port 8080 to the internet.
