# Building PetCam OS

## GitHub Actions (recommended)

Open Actions → Build PetCam OS → Run workflow. The build uses the official Raspberry Pi pi-gen project on an Ubuntu GitHub-hosted runner, adds the PetCam stage, runs the camera-discovery unit tests, and uploads the generated image from pi-gen/deploy as a workflow artifact.

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

The resulting image is written to pi-gen/deploy/.

## First boot and Wi-Fi

Wi-Fi country is deliberately not baked into the image. Select the regulatory country for the place where the camera will operate during Raspberry Pi Imager's OS Customisation before writing the card. Configure hostname petcam, Wi-Fi credentials, WLAN country, an administrator account, and optional SSH. Imager applies the country during first boot. To change it later, run sudo raspi-config → Localisation Options → WLAN Country.

The camera stream has no authentication and is intended only for a trusted LAN.

## Camera discovery and configuration

At boot, PetCam enumerates V4L2 video nodes and parses each node's advertised formats, discrete/stepwise/continuous frame sizes, and discrete or ranged frame intervals. Automatic selection prefers native MJPEG, then chooses a starting mode within a conservative pixel-throughput budget associated with the detected Pi family. The budget only guides automatic selection; it is not a hard maximum.

Inspect all advertised modes:

~~~bash
sudo petcam-camera discover
~~~

Preview a supported manual mode:

~~~bash
sudo petcam-camera select --resolution 1920x1080 --fps 30
~~~

Set the chosen values in /etc/petcam/petcam.conf and restart:

~~~bash
sudo nano /etc/petcam/petcam.conf
sudo systemctl restart petcam-stream
~~~

Supported settings:

- DEVICE: optional V4L2 node, such as /dev/video0
- RESOLUTION: auto or an advertised WIDTHxHEIGHT
- FPS: auto or a frame rate advertised for that format and resolution
- PORT: HTTP port, default 8080

Automatic FPS uses µStreamer's no-application-cap request (--desired-fps=0); the camera/driver determines the delivered cadence. Unsupported explicit modes fail clearly rather than silently falling back. If no native MJPEG mode is available, PetCam may select a raw V4L2 mode and use CPU JPEG encoding, with a performance warning.

## Runtime diagnostics

~~~bash
sudo petcam-status
systemctl status petcam-stream
journalctl -u petcam-stream -b
~~~

The status report includes systemd service state, a local HTTP endpoint health probe, detected and active camera details, capture resolution and format, the latest captured_fps value from µStreamer's performance log, a one-second MJPEG bitrate sample measured by opening a temporary local stream client, aggregate network-interface transmit throughput, cumulative transmitted bytes, and SoC temperature. The local MJPEG sample is stream-specific; the network-interface rate includes other interface traffic.

Dropped frames are reported as unavailable when the active UVC driver/µStreamer build does not expose a reliable cumulative counter. PetCam does not estimate or fabricate a dropped-frame count. Capture FPS and service/device errors remain available for diagnosing stream quality.

## Hardware validation

Software discovery only establishes what a camera advertises; it cannot guarantee sustained performance on a specific Pi, USB hub, power supply, or wireless network. Test each intended mode for several minutes while observing petcam-status and the service journal. A powered USB OTG hub is recommended for cameras with substantial power draw.

## OBS

Use the network stream URL:

~~~
http://petcam.local:8080/stream
~~~

If OBS cannot consume the MJPEG endpoint directly, add it through a VLC Video Source or use the µStreamer page at http://petcam.local:8080/ in a Browser Source. If mDNS does not resolve, substitute the Pi's DHCP address.

Do not expose port 8080 to the public internet.
