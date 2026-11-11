# PetCam OS

A minimal, reproducible Raspberry Pi OS image for a Raspberry Pi Zero W and a USB UVC camera (initial target: Razer Kiyo). It serves the camera as native MJPEG over the local network for OBS.

## Target hardware

- Original Raspberry Pi Zero W (ARMv6, 512 MB RAM)
- USB UVC webcam connected through a micro-USB OTG adapter; use a powered hub for the Razer Kiyo
- microSD card and 2.4 GHz Wi-Fi network
- OBS on another computer on the same LAN

## Image build

The image is built with the official [Raspberry Pi pi-gen](https://github.com/RPi-Distro/pi-gen) tool. The build host uses Docker; Docker is not installed on the Pi.

The initial workflow builds a 32-bit Raspberry Pi OS Lite image and adds PetCam's camera streamer service. The image artifact is published by GitHub Actions.

## Camera stream

PetCam uses [µStreamer](https://github.com/pikvm/ustreamer) in native MJPEG mode. It does not transcode or re-encode camera frames on the Zero W. The service chooses a V4L2 device that advertises MJPEG and starts with a conservative 1280x720 at 30 FPS profile. Actual supported modes depend on the webcam, USB bandwidth, power, and network conditions.

- Stream endpoint: `http://petcam.local:8080/stream`
- Status/UI: `http://petcam.local:8080/`

If mDNS is unavailable, use the Pi's DHCP address instead.

## First boot and Wi-Fi

Use Raspberry Pi Imager's OS customization to set the hostname to `petcam`, configure the 2.4 GHz Wi-Fi network, enable SSH if desired, and create an administrator account before writing the image. The first boot expands the filesystem as usual.

## Build locally

See [docs/building.md](docs/building.md). The GitHub Actions workflow is the recommended reproducible build path.

## Status

This is the first image-builder scaffold. It still needs a hardware validation pass on the actual Zero W + Kiyo combination before the image should be considered release-ready. In particular, verify the camera's V4L2 modes and sustained stream stability on the target board.

## Security

The streamer binds to the LAN interface on port 8080. Keep the device on a trusted private network; do not port-forward the stream to the internet.
