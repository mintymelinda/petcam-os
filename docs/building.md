# Building PetCam OS

## What the GitHub Actions workflow does

The workflow is named **Build PetCam OS image**.

- Pull requests, pushes to `main`, and manual workflow dispatches run validation: Python byte-compilation, shell syntax checks, and the unit test suite.
- A full Raspberry Pi OS image build and GitHub Release publication runs only when a version tag matching `vMAJOR.MINOR.PATCH` is pushed.
- The tagged release job checks out the official Raspberry Pi [pi-gen](https://github.com/RPi-Distro/pi-gen) project at a fixed commit, adds the PetCam stage, builds and shrinks the image, inventories the packages in the final root filesystem, generates and validates the Raspberry Pi Imager OS sublist, packages the exact application release from the built image, signs the update manifest, and uploads the resulting assets to the GitHub Release.

The pi-gen revision is pinned to `6e3ff62bfd9c690a6a3d1d7de259d5e0af40cb78`. This prevents upstream stage/package changes from silently changing release contents. Each release also includes a TSV inventory of installed packages (including versions and installed sizes) and a build-info file recording the pi-gen commit.

Running **Actions → Build PetCam OS image → Run workflow** does not build or upload a flashable image; it runs the validation job. Use a version tag to invoke the release build.

## pi-gen base package audit

PetCam currently builds on the standard `stage0`, `stage1`, and `stage2` stages. The pinned upstream revision already supplies several packages that the custom stage used to repeat:

- Stage 0 provides locale setup, Raspberry Pi firmware/kernel packages, and bootstrap certificates.
- Stage 1 supplies Raspberry Pi configuration, network base data, and time synchronisation.
- Stage 2 installs the standard system tools, including `build-essential`, `pkg-config`, `v4l-utils`, and `avahi-daemon`. It also installs `python-is-python3`, which brings in the Python 3 runtime dependencies.
- Stage 2's `00-packages-nr` installs `rpicam-apps-lite` without recommended packages.
- Stage 2's networking and cloud-init stages provide NetworkManager, Wi-Fi support, and the enabled cloud-init integration.

The PetCam package list therefore avoids repeating `avahi-daemon`, `build-essential`, `pkg-config`, `v4l-utils`, `python3-minimal`, and `rpicam-apps-lite`. The custom stage still explicitly installs its own build dependencies and application dependencies, including Git, the µStreamer development libraries, OpenSSL, and Pillow.

`avahi-utils` is deliberately retained even though the current application does not invoke its command-line tools. It provides useful `avahi-browse` and `avahi-resolve` commands for future mDNS and service-discovery diagnostics.

Each tagged release exports the final installed-package inventory from the built root filesystem. Use that inventory to identify the largest installed packages and compare changes when deliberately updating the pi-gen pin; do not infer final image contents solely from PetCam's package list.

## Local image build

Use a Linux x86-64 host with Docker, Git, and sufficient free disk space (allow at least 30 GB).

```bash
git clone https://github.com/RPi-Distro/pi-gen.git
git -C pi-gen checkout 6e3ff62bfd9c690a6a3d1d7de259d5e0af40cb78
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
```

The resulting image is written to `pi-gen/deploy/`. The PetCam application is installed under `/opt/petcam/releases/<version>/`, and `/opt/petcam/current` selects the active release. Device configuration under `/etc/petcam/` and runtime data under `/var/lib/petcam/` are outside the versioned application tree.

The release workflow and the local instructions use the same pinned pi-gen commit. Update that pin deliberately, review the upstream stage/package changes, and update the package audit expectations when changing it.

## Tagged release assets

A successful version-tag build publishes:

- `petcam-os-vMAJOR.MINOR.PATCH.img.xz`: filesystem- and partition-shrunk, compressed flashable image. The first-boot root-filesystem expansion remains enabled so the installed system grows to use the target card.
- `petcam-os-os-list.json`: generated Raspberry Pi Imager OS sublist.
- `petcam-MAJOR.MINOR.PATCH-armhf.tar.gz`: versioned PetCam application archive.
- `petcam-update-manifest.json` and `petcam-update-manifest.json.sig`: signed update metadata and detached Ed25519 signature.

The Imager sublist includes the HTTPS image URL, compressed and extracted SHA-256 hashes and sizes, supported 32-bit device tags for Pi 1 through Pi 5 families, `architecture: armhf`, and `init_format: cloudinit-rpi`. The workflow validates the generated JSON against Raspberry Pi's published schema before publishing it.

The stable sublist URL intended for catalogue submission is:

```
https://github.com/mintymelinda/petcam-os/releases/latest/download/petcam-os-os-list.json
```

Generating and publishing the sublist does not itself add PetCam to Raspberry Pi Imager's default catalogue; that requires Raspberry Pi's review and approval. Until then, the sublist can be loaded manually through Imager's custom content repository settings for testing.

## Runtime installation and camera layout

The image installs the application in a versioned release directory and selects it through `/opt/petcam/current`. Configuration and mutable state are kept separately:

- `/etc/petcam/petcam.conf`: primary camera configuration.
- `/etc/petcam/cameras.d/`: additional independent camera profiles.
- `/etc/petcam/home-assistant.conf`: optional MQTT publisher configuration.
- `/etc/petcam/web-auth.json`: manager password hash.
- `/var/lib/petcam/`: timelapse frames, motion events, and runtime state.

The camera capture process runs as the unprivileged `petcam` account. USB uses the bundled, version-pinned µStreamer binary; CSI uses the Raspberry Pi `rpicam` tools. The capture backend binds to loopback (default port 8081). The manager serves HTTPS on port 8443 and a separate stream-only HTTP proxy on port 8080.

## First boot and administrator setup

The PetCam manager is configured from a browser; an SSH session is not required for normal setup.

1. Flash the image and complete Raspberry Pi Imager customisation. Configure Wi-Fi and the WLAN country, and create the Linux administrator account. SSH is optional.
2. Power on the Pi and allow the first boot to finish.
3. From another device on the same network, open `https://petcam.local:8443/`. If mDNS is unavailable, use the Pi's DHCP address.
4. On a new image, the manager displays a one-time administrator setup form. Create and confirm a password of at least 12 characters. The browser is signed in automatically after setup.
5. Continue with camera discovery and configuration in the manager.

The PetCam manager password is separate from the Linux account created in Imager. The first-run endpoint refuses to replace an existing password, so it cannot be used to reset an established device. If the manager password is lost, run `sudo petcam-admin` locally or over SSH and type `RESET` to confirm a PetCam factory reset. This restores application settings and credentials to first-run defaults, removes camera profiles and PetCam-generated timelapse/motion data, and preserves Raspberry Pi OS, Wi-Fi/network configuration, hostname, SSH, TLS identity, updater trust, and installed software. Complete first-run setup on a trusted private network and do not expose the manager to the public internet.

The manager uses a per-device self-signed HTTPS certificate. The browser will warn that the certificate is not trusted until you explicitly trust it; verify that you are connecting to your own device before proceeding.

## Camera discovery and configuration

At runtime, USB UVC devices are enumerated through V4L2, including advertised formats, frame sizes, and frame intervals. CSI modules are enumerated with `rpicam-hello --list-cameras`, and sensor modes and frame rates are read from the camera stack. Both backends expose an MJPEG stream.

Automatic mode selection uses conservative model-specific pixel-throughput heuristics. These are starting budgets, not hardware performance guarantees:

| Board family | Automatic selection budget |
| --- | ---: |
| Pi 1 | 9.2 megapixels/second |
| Pi Zero W, Pi 2 | 27.6 megapixels/second |
| Pi Zero 2 W, Pi 3 | 62.2 megapixels/second |
| Pi 4 / Pi 400 | 124.4 megapixels/second |
| Pi 5 | 248.8 megapixels/second |

USB automatic selection prefers native MJPEG. If no native-MJPEG mode fits the starting budget, PetCam may select a less demanding mode and emit a warning; raw USB formats may require CPU JPEG encoding. CSI selection uses advertised sensor modes and warns when it must choose a mode above the starting budget. Explicit camera-supported modes are not rejected merely because they exceed the automatic budget.

Discover cameras and modes:

```bash
sudo petcam-camera discover
sudo petcam-camera discover-csi
```

Preview a supported USB mode:

```bash
sudo petcam-camera select --format MJPG --resolution 1920x1080 --fps 30
```

Preview a supported CSI mode (use the camera index reported by discovery):

```bash
sudo petcam-camera select-csi --camera 0 --resolution 1920x1080 --fps 30
```

The shared parser is used by the CLI, web manager, status command, and stream startup. Validate the current configuration:

```bash
sudo petcam-config validate --check-hardware
```

For manual changes, edit a candidate file and apply it transactionally:

```bash
sudo cp /etc/petcam/petcam.conf /tmp/petcam.conf.new
sudo nano /tmp/petcam.conf.new
sudo petcam-config apply /tmp/petcam.conf.new
```

The apply operation validates the candidate, stages it, restarts capture, and waits for an actual MJPEG frame. On failure it restores the previous configuration and restarts the previous stream. The last known-good configuration is retained at `/etc/petcam/petcam.conf.last-good`. Stream startup validates the file before sourcing it; malformed lines, unknown keys, and shell expressions are rejected.

Supported primary camera settings:

- `SOURCE`: `auto` (USB first, then CSI), `usb`, or `csi`.
- `DEVICE`: optional USB V4L2 device path; a persistent `/dev/v4l/by-id/` path is preferred.
- `USB_PHYSICAL_PATH`: stable USB topology path managed by the UI.
- `CAMERA`: CSI camera index or `auto`.
- `CSI_DEVICE_PATH`: stable CSI device-tree path managed by the UI.
- `FORMAT`: USB format; `auto` or a supported V4L2 format such as MJPG, YUYV, or UYVY.
- `RESOLUTION`: `auto` or an advertised width-by-height mode.
- `FPS`: `auto` or an advertised frame rate (up to 240).
- `CSI_QUALITY`: CSI MJPEG quality, 1–100; default 70.
- `PORT`: internal loopback backend port; default 8081. Ports 8080 and 8443 are reserved.

For USB, `FPS=auto` asks µStreamer not to impose an application-side frame-rate cap. For CSI, automatic selection chooses an advertised sensor frame rate. Unsupported explicit modes fail clearly rather than silently falling back.

## Multiple independent cameras

The web manager can add cameras as independent streams. Each additional camera receives a profile in `/etc/petcam/cameras.d/`, a systemd instance named `petcam-stream@<profile-id>.service`, and a public stream path `/stream/<profile-id>` on port 8080. Profiles can be previewed, reconfigured, and removed independently of the primary stream.

Camera identities prefer persistent USB by-id paths or physical USB topology and CSI device-tree paths. Before a stream is started or reconfigured, PetCam checks aggregate capture pixel rate, estimated raw USB bandwidth, and frame-buffer memory against conservative model limits. This is a protective admission check, not a guarantee of stable operation.

## Runtime diagnostics

```bash
sudo petcam-status
systemctl status petcam-stream
journalctl -u petcam-stream -b
sudo petcam-diagnostics
```

The status report includes service state, endpoint health, USB/CSI discovery counts, active backend and camera mode, latest capture FPS where available, a one-second local MJPEG bitrate sample, aggregate interface transmit throughput, cumulative transmitted bytes, SoC temperature, and periodic stream-monitor state/recovery count.

The health monitor runs every 30 seconds after a startup grace period. It waits for an actual backend frame, checks the OBS proxy separately, distinguishes a missing camera from a stalled process, and applies bounded restart backoff. The local bitrate sample opens a temporary stream client; interface throughput includes all traffic on that interface.

`sudo petcam-diagnostics` checks the backend, HTTPS manager, OBS proxy, listeners and bind addresses, mDNS resolution, interface link state, local addresses, and default routes. It prints browser-source URLs based on the device hostname and LAN addresses. The manager provides the same checks and can download a redacted support bundle. From SSH:

```bash
sudo petcam-diagnostics --bundle
# Optional explicit destination:
sudo petcam-diagnostics --bundle --output /tmp/petcam-support.tar.gz
```

The bundle contains diagnostics, status output, redacted camera configuration, and available service journal entries. Secret-keyed values and tokens are redacted; authentication files and private keys are excluded. These are local checks and cannot prove remote client reachability. Test mDNS, firewall rules, Wi-Fi client isolation, and VLAN segmentation from the OBS computer.

A cumulative dropped-frame counter is unavailable when the active USB/CSI driver does not expose a reliable value. PetCam reports it as unavailable rather than estimating it.

## Hardware validation

Camera discovery reports advertised modes; it does not prove that a particular Pi, camera, USB hub, power supply, or network can sustain them. Test the intended configuration on the actual hardware while monitoring capture FPS, stream health, and service logs. A powered USB OTG hub is recommended for webcams with substantial power draw. CSI modules require a compatible ribbon cable and camera connector.

## OBS

Use the network stream URL:

```
http://petcam.local:8080/stream
```

Additional camera profiles use `http://petcam.local:8080/stream/<profile-id>`. If mDNS does not resolve, use the Pi's DHCP address. If OBS cannot consume the MJPEG endpoint directly, add it through a VLC Video Source.

The authenticated manager is separate from the unauthenticated OBS stream endpoint. Do not expose port 8080 to the public internet.
