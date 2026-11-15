# PetCam

PetCam is the application source for a Raspberry Pi camera streaming and management system. It supports USB UVC webcams and Raspberry Pi CSI cameras, exposes MJPEG streams for OBS, and provides an authenticated web manager.

## Features

- USB UVC and CSI camera discovery, mode selection, preview, and persistent configuration.
- Multiple independent camera streams with separate profiles and URLs.
- Authenticated web manager with transactional configuration changes.
- Runtime health monitoring, status reporting, network diagnostics, and support bundles.
- Optional timelapse capture, motion-triggered snapshots, Home Assistant MQTT discovery, and RTMP/RTMPS publishing.
- Signed application updates with staged installation and rollback.

## Repository contents

- Runtime Python application modules: `stage-petcam/00-install-files/files/usr/local/lib/petcam/`
- Runtime configuration, systemd unit definitions, and service helpers: `stage-petcam/00-install-files/files/`
- Unit tests: `tests/`
- Feature and operations documentation: `docs/`
- Application version: `VERSION`

The source files remain in their existing layout so the test suite and runtime packaging references continue to resolve. The repository intentionally contains no Raspberry Pi OS image builder, pi-gen stages, image build workflow, or image publication tooling.

## Documentation

- [Home Assistant MQTT integration](docs/home-assistant.md)
- [Optional RTMP output](docs/rtmp-output.md)
- [Motion detection](docs/motion-detection.md)
- [Timelapse capture](docs/timelapse.md)
- [Application update engine](docs/update-engine.md)

## Camera stream and management

PetCam selects a capture backend automatically. USB UVC cameras use µStreamer, preferring native MJPEG passthrough and using CPU JPEG encoding for supported raw formats when necessary. CSI modules use Raspberry Pi's `rpicam-vid` stack and default to MJPEG. When OBS RTSP output is enabled, MJPEG consumers are disabled and a supported Raspberry Pi hardware H.264 encoder is used when available; software H.264 encoding is not used.

- OBS stream: `http://petcam.local:8080/stream`
- Authenticated manager: `https://petcam.local:8443/`
- Internal capture backend: loopback only, default port `8081`

The manager is a lightweight Python standard-library application. It does not require Node.js, a frontend framework, or a database. Device configuration and runtime data are stored outside the application source tree.
