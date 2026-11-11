# Building PetCam OS

## GitHub Actions (recommended)

Open **Actions → Build PetCam OS → Run workflow**. The build uses the official Raspberry Pi `pi-gen` project on an Ubuntu GitHub-hosted runner, adds the PetCam stage, and uploads the generated image from `pi-gen/deploy` as a workflow artifact.

The first build can take a while. The output is a compressed Raspberry Pi OS image suitable for Raspberry Pi Imager.

## Local build

Use a Linux x86-64 host with Docker, Git, and enough free disk space (allow at least 30 GB).

```bash
git clone https://github.com/RPi-Distro/pi-gen.git
git clone https://github.com/mintymelinda/petcam-os.git
cp petcam-os/config pi-gen/config
cp -a petcam-os/stage-petcam pi-gen/stage-petcam
cd pi-gen
sudo ./build-docker.sh
```

The resulting image is written to `pi-gen/deploy/`.

## Wi-Fi and initial administrator

In Raspberry Pi Imager, select the generated custom image. Before writing it, open OS Customisation and configure:

- Hostname: `petcam`
- Your 2.4 GHz Wi-Fi SSID and password
- Country: Canada (or your actual regulatory country)
- Create an administrator username and password
- Enable SSH if you want remote administration

Use a strong unique password. The camera stream itself has no authentication and is intended only for a trusted LAN.

## Hardware validation

The build can install the software, but it cannot prove a specific camera mode works on the Pi Zero W. On first boot, connect the Razer Kiyo through a powered USB OTG hub, then inspect:

```bash
v4l2-ctl --list-devices
v4l2-ctl -d /dev/video0 --list-formats-ext
systemctl status petcam-stream
journalctl -u petcam-stream -b
```

The default profile is 1280x720 at 30 FPS and requests the camera's native MJPEG format. If the camera exposes a different V4L2 node, set `DEVICE=/dev/videoN` in `/etc/petcam/petcam.conf`. Change `RESOLUTION` and `FPS` there if the selected mode is not supported, then run `sudo systemctl restart petcam-stream`.

## OBS

Use the network stream URL:

```
http://petcam.local:8080/stream
```

If your OBS source cannot consume the MJPEG endpoint directly, add it through a VLC Video Source or use the µStreamer page at `http://petcam.local:8080/` in a Browser Source. If mDNS does not resolve, substitute the Pi's DHCP address.

Do not expose port 8080 to the public internet.
