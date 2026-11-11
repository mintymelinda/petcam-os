# PetCam OS

A minimal, reproducible Raspberry Pi OS image for Raspberry Pi boards and USB UVC cameras. It serves camera video over the local network for OBS, preferring native MJPEG passthrough and avoiding transcoding whenever possible.

## Supported hardware

- Raspberry Pi Zero W (initial low-resource target); automatic hardware profiles also recognize Zero 2 W, Pi 1/2/3, Pi 4/400, and Pi 5.
- USB UVC cameras connected through the board's USB host/OTG port. A powered hub is recommended, especially for bus-powered webcams.
- OBS on another computer on the same LAN.

The image is not tied to a specific webcam. Actual resolutions, formats, and frame intervals are discovered from each camera's V4L2 capabilities at boot.

## Image build

The image is built with the official [Raspberry Pi pi-gen](https://github.com/RPi-Distro/pi-gen) tool. The build host uses Docker; Docker is not installed on the Pi.

The workflow builds a 32-bit Raspberry Pi OS Lite image and adds PetCam's camera streamer service. The flashable image is published by GitHub Actions.

## Camera stream

PetCam uses [µStreamer](https://github.com/pikvm/ustreamer). It prefers native MJPEG modes and passes pre-encoded frames through without decoding or re-encoding. If a camera has no MJPEG mode, it can fall back to a raw V4L2 format with CPU JPEG encoding; this is substantially more demanding and is warned about.

- Stream endpoint: http://petcam.local:8080/stream
- µStreamer status/UI: http://petcam.local:8080/

If mDNS is unavailable, use the Pi's DHCP address instead.

### Discover and choose camera modes

~~~bash
sudo petcam-camera discover
sudo petcam-camera select --resolution 1920x1080 --fps 30
sudo petcam-status
~~~

discover reports all detected V4L2 devices, formats, resolutions, and advertised frame intervals. select previews a chosen configuration. Automatic mode prefers MJPEG and uses a conservative starting pixel-throughput budget for the detected Pi model. Explicit camera-supported modes are not blocked; a warning is emitted when a selection exceeds the model's automatic starting budget.

Edit /etc/petcam/petcam.conf to override:

~~~bash
# Optional: pin a specific V4L2 node
# DEVICE=/dev/video0
RESOLUTION=1920x1080
FPS=30
PORT=8080
~~~

Use RESOLUTION=auto and FPS=auto to return to automatic selection. FPS=auto asks µStreamer not to impose an application-side frame-rate cap. Unsupported explicit modes fail clearly rather than silently falling back. Restart after changes:

~~~bash
sudo systemctl restart petcam-stream
~~~

### Runtime diagnostics

sudo petcam-status reports service state, the latest capture FPS reported by µStreamer performance logs, sampled aggregate network-interface transmit throughput, cumulative transmitted bytes, and SoC temperature. Network throughput is interface-wide, not isolated to the video stream.

The standard UVC/µStreamer interface does not provide a reliable cumulative dropped-frame counter for every camera driver. PetCam reports this as unavailable rather than inventing a value. Compare reported capture FPS with the selected mode's advertised frame interval and inspect the service journal for device/stream errors.

## First boot and Wi-Fi

Use Raspberry Pi Imager's OS customization before writing the image to set the hostname to petcam, configure Wi-Fi, select the WLAN regulatory country where the Pi will operate, enable SSH if desired, and create an administrator account. The image intentionally has no hard-coded Wi-Fi country. Imager applies the selection during first boot. To change it later, use sudo raspi-config → Localisation Options → WLAN Country.

## Build locally

See docs/building.md. The GitHub Actions workflow is the recommended reproducible build path.

## Validation status

Camera mode discovery and automatic selection are software features; they do not prove a mode is sustainable on a particular Pi, USB hub, camera, or network. Run a sustained hardware test on the actual combination before treating a high-resolution/high-FPS mode as reliable.

## Security

The streamer binds to the LAN interface on port 8080. Keep the device on a trusted private network; do not port-forward the stream to the internet.
