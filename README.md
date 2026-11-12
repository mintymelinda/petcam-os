# PetCam OS

A minimal Raspberry Pi OS image for Raspberry Pi boards, USB UVC webcams, and Raspberry Pi CSI camera modules. PetCam serves camera video over the local network as an MJPEG stream for OBS.

## Features

- USB UVC and CSI camera discovery, mode selection, preview, and persistent configuration.
- Multiple independent camera streams, each with its own profile and OBS URL.
- Authenticated HTTPS camera manager with transactional configuration changes.
- Runtime health monitoring, status reporting, network diagnostics, and redacted support bundles.
- Optional scheduled timelapse JPEG capture and motion-triggered event snapshots.
- Optional read-only Home Assistant MQTT discovery.
- Signed application updates with staged installation and rollback.

See the feature guides:
- [Building and installing](docs/building.md)
- [Home Assistant MQTT integration](docs/home-assistant.md)
- [Motion detection](docs/motion-detection.md)
- [Timelapse capture](docs/timelapse.md)
- [Application update engine](docs/update-engine.md)

## Supported hardware

The published image is 32-bit Raspberry Pi OS (armhf). The Imager device list covers these Raspberry Pi families:

- Raspberry Pi 1 family, including the original Zero and Zero W.
- Raspberry Pi 2 family.
- Raspberry Pi 3 family, including Zero 2 W.
- Raspberry Pi 4 family, including Pi 400.
- Raspberry Pi 5 family.

PetCam's hardware-profile logic recognizes these board generations and uses the detected model to choose conservative automatic camera-mode budgets. These budgets are selection heuristics, not guarantees that every camera mode will run reliably.

Camera support depends on the operating system's V4L2 and Raspberry Pi camera-stack support:

- USB UVC webcams connected through the board's USB host/OTG port. A powered hub is recommended, especially for bus-powered webcams.
- CSI camera modules supported by the installed Raspberry Pi `rpicam` camera stack, including common OV5647, IMX219, IMX355, IMX708, and IMX477 sensors.
- USB and CSI cameras use the same image and public MJPEG stream interface; no separate image is required.

USB modes are discovered through V4L2. CSI cameras and advertised sensor modes are discovered through `rpicam-hello --list-cameras`.

## Image build and release

PetCam uses the official [Raspberry Pi pi-gen](https://github.com/RPi-Distro/pi-gen) build system. Docker is used on the build host; Docker is not installed on the Pi.

The GitHub Actions workflow validates Python and shell syntax and runs the unit test suite for pull requests, pushes to `main`, and manual workflow dispatches. A full image build and publication happens for version tags matching `vMAJOR.MINOR.PATCH`. Tagged releases publish the compressed image, generated Raspberry Pi Imager OS sublist, and signed application-update assets. See [docs/building.md](docs/building.md) for the exact workflow and local build instructions.

The image installs PetCam application files into versioned releases under `/opt/petcam/releases/`, with `/opt/petcam/current` selecting the active release. Device configuration and runtime data live outside that versioned tree.

## Camera stream and management

PetCam selects a capture backend automatically. USB UVC cameras use [µStreamer](https://github.com/pikvm/ustreamer), preferring native MJPEG passthrough and using CPU JPEG encoding for supported raw formats when necessary. CSI modules use Raspberry Pi's `rpicam-vid` stack and are encoded to MJPEG.

- OBS stream: `http://petcam.local:8080/stream`
- Authenticated manager: `https://petcam.local:8443/`
- Internal capture backend: loopback only, default port `8081`

If mDNS is unavailable, use the Pi's DHCP address instead.

The manager is a lightweight Python standard-library application. It does not require Node.js, a frontend framework, or a database. It discovers cameras when the page is opened or refreshed, rather than continuously polling for hardware. Use **Refresh cameras** after connecting or removing a camera, choose an advertised mode, preview it, and apply it. Changes are validated and applied transactionally: PetCam waits for a real MJPEG frame and restores the previous configuration if the new stream does not become ready.

The active camera settings are stored in `/etc/petcam/petcam.conf` and survive reboots. USB identities use persistent V4L2 by-id paths or physical USB topology; CSI identities use device-tree paths where available. This avoids relying solely on transient `/dev/videoN` or CSI enumeration numbers.

### Discover and select camera modes

```bash
sudo petcam-camera discover
sudo petcam-camera discover-csi
sudo petcam-camera select --format MJPG --resolution 1920x1080 --fps 30
sudo petcam-camera select-csi --camera 0 --resolution 1920x1080 --fps 30
sudo petcam-status
```

Use `select` for USB and `select-csi` for CSI. Automatic selection prefers native USB MJPEG and applies a conservative model-specific pixel-throughput budget. Explicit supported modes are not blocked solely for exceeding that automatic budget; PetCam emits a performance warning. Unsupported explicit modes fail rather than silently falling back.

The configuration supports `SOURCE=auto|usb|csi`, USB `DEVICE` and `FORMAT`, CSI `CAMERA`, `RESOLUTION`, `FPS`, `CSI_QUALITY`, and the internal backend `PORT`. The default source is `auto`, which tries a usable USB camera before CSI. See [docs/building.md](docs/building.md) for the settings and safe transactional CLI workflow.

### Multiple independent streams

The manager can add cameras as independent streams without replacing or interrupting the primary `/stream` endpoint. Each additional camera receives a profile in `/etc/petcam/cameras.d/`, a `petcam-stream@…` systemd instance, and its own URL under `http://petcam.local:8080/stream/<profile-id>`. Additional streams can be previewed, reconfigured, and removed independently.

Before starting or reconfiguring a stream, PetCam checks combined capture pixel rate, estimated raw USB bandwidth, and frame-buffer memory against conservative limits for the detected board. Requests exceeding those limits are rejected with an explanation.

## Optional camera features

### Timelapse

Timelapse is disabled by default. When enabled, its worker periodically reads a JPEG from the existing stream, applies optional resize/quality settings, and stores frames under `/var/lib/petcam/timelapse/`. It does not start another camera process. See [docs/timelapse.md](docs/timelapse.md).

### Motion detection

Motion detection is disabled by default. Its worker samples the existing stream, compares reduced grayscale frames, and saves full-resolution event snapshots when configured thresholds are met. It does not record continuous video or open the camera device directly. See [docs/motion-detection.md](docs/motion-detection.md).

### Home Assistant

An optional MQTT 3.1.1 publisher exposes health and stream-status entities through Home Assistant MQTT discovery. It is disabled by default, runs as a separate service, and does not expose camera controls. See [docs/home-assistant.md](docs/home-assistant.md).

## Runtime status and diagnostics

Run:

```bash
sudo petcam-status
sudo petcam-diagnostics
```

The status report includes service and endpoint health, USB/CSI detection counts, active backend and camera mode, capture FPS where available, a short local MJPEG bitrate sample, aggregate interface transmit throughput, cumulative transmitted bytes, SoC temperature, and stream-monitor recovery information.

The diagnostics command checks the capture backend, HTTPS manager, OBS stream proxy, listeners and bind addresses, mDNS resolution, network interfaces, IP addresses, and default routes. It prints usable OBS browser-source URLs. The manager exposes the same checks and can generate a redacted support bundle; from SSH, run `sudo petcam-diagnostics --bundle` (optionally with `--output PATH`). Secret values are redacted and authentication files and private keys are excluded.

These probes run on the Pi and cannot prove that a remote OBS client can reach it. Check remote firewall rules, Wi-Fi client isolation, VLAN segmentation, and mDNS support from the client computer. A cumulative dropped-frame counter is reported as unavailable when the active driver does not expose a reliable value; PetCam does not estimate one.

## Application updates

The image includes a root-owned updater for signed PetCam application releases. It downloads a manifest from GitHub Releases, verifies its Ed25519 signature and archive digest, stages a versioned release, switches `/opt/petcam/current` atomically, and rolls back if the web manager health check fails. It does not update Raspberry Pi OS, the kernel, firmware, or boot partition.

Version tags matching `vMAJOR.MINOR.PATCH` trigger the image and signed application-release pipeline. Raspberry Pi Imager's catalogue is not involved in update discovery or installation. Existing devices created before the update trust key and manifest URL were added to the image need the one-time public-key/configuration bootstrap in [docs/update-engine.md](docs/update-engine.md). Updates are checked and installed manually; a browser-local preference can check when the manager is opened, but does not schedule background checks or install updates automatically.

## First boot and Raspberry Pi Imager

The tagged-release workflow generates a Raspberry Pi Imager OS sublist using the published schema, HTTPS release asset URLs, compressed and extracted SHA-256 hashes, supported 32-bit Pi device tags, and `init_format: cloudinit-rpi`. The generated sublist is attached to each versioned GitHub Release. Inclusion in Raspberry Pi Imager's default catalogue is subject to Raspberry Pi's catalogue review and should not be assumed until it is approved.

When installing with Raspberry Pi Imager, use its normal customisation step to set the hostname, configure Wi-Fi, select the WLAN regulatory country, create the administrator account, and optionally enable SSH. PetCam does not bake in a Wi-Fi country. To change it later, use `sudo raspi-config` → **Localisation Options** → **WLAN Country**.

## Security and network exposure

On first boot, open the manager from another device on the same network:

- `https://petcam.local:8443/` (use the Pi's DHCP address if mDNS is unavailable).
- Create the PetCam manager administrator password in the browser. The password must be at least 12 characters.
- The first-run setup is one-time and signs the browser in automatically. SSH is not required.

This manager password is separate from the Linux administrator account configured in Raspberry Pi Imager. The `sudo petcam-admin` command remains available as a local/SSH recovery path; it is not part of normal first-run setup.

Password hashes use PBKDF2-HMAC-SHA256 and are stored in `/etc/petcam/web-auth.json`. Each device generates its own self-signed HTTPS certificate, so the browser will show a certificate warning until the certificate is explicitly trusted. Sessions are HTTP-only, Secure, SameSite=Strict, expire after eight hours, and are invalidated when the password changes or the manager service restarts.

The manager listens on HTTPS port `8443`. Port `8080` is a separate unauthenticated HTTP stream-only endpoint for OBS; it exposes stream paths, not management or update routes. The camera capture backend listens only on loopback. Keep the device on a trusted private network and do not port-forward either public port to the internet.

Camera mode discovery and automatic selection do not prove sustained performance on a particular Pi, camera, USB hub, power supply, or network. Validate the intended configuration on the actual hardware.
