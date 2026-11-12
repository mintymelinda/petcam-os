#!/usr/bin/env python3
"""Restore PetCam application settings to first-run defaults."""

import getpass
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ETC = Path("/etc/petcam")
STATE = Path("/var/lib/petcam")
CAMERA_DEFAULTS = """# SOURCE=auto selects USB when available, then falls back to CSI.
SOURCE=auto
DEVICE=
USB_PHYSICAL_PATH=
CAMERA=auto
CSI_DEVICE_PATH=
FORMAT=auto
RESOLUTION=auto
FPS=auto
CSI_QUALITY=70
PORT=8081
"""
HOME_ASSISTANT_DEFAULTS = """# Optional Home Assistant MQTT discovery publisher. Disabled by default.
MQTT_ENABLED=false
MQTT_HOST=
MQTT_PORT=1883
MQTT_USERNAME=
MQTT_PASSWORD=
MQTT_TLS=false
MQTT_CLIENT_ID=
MQTT_DISCOVERY_PREFIX=homeassistant
MQTT_TOPIC_PREFIX=petcam
MQTT_PUBLISH_STREAM_URL=false
MQTT_STREAM_URL=
"""
TIMELAPSE_DEFAULTS = {
    "enabled": False, "interval_seconds": 300, "schedule_start": "00:00",
    "schedule_end": "23:59", "retention_days": 30, "max_storage_mb": 2048,
    "min_free_mb": 256,
}
MOTION_DEFAULTS = {
    "enabled": False, "sample_interval_seconds": 2, "sensitivity": 50,
    "minimum_changed_percent": 2.5, "confirm_samples": 2,
    "min_event_interval_seconds": 60, "quiet_hours_enabled": False,
    "quiet_hours_start": "22:00", "quiet_hours_end": "07:00",
    "retention_days": 14, "max_storage_mb": 512, "min_free_mb": 256,
    "max_events": 100,
}
SERVICES = (
    "petcam-motion.service", "petcam-timelapse.service",
    "petcam-home-assistant.service", "petcam-stream.service",
    "petcam-web.service",
)


def run(*command):
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL)


def write_file(path, content, mode=0o640):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".reset-tmp")
    temporary.write_text(content, encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def reset():
    if os.geteuid() != 0:
        raise PermissionError("Run with sudo: sudo petcam-admin")

    # Stop every PetCam worker before clearing its configuration and data.
    profiles = sorted((ETC / "cameras.d").glob("*.conf"))
    for profile in profiles:
        profile_id = profile.stem
        if profile_id and all(c in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in profile_id):
            subprocess.run(
                ["/usr/bin/systemctl", "stop", f"petcam-stream@{profile_id}.service"],
                check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    for service in SERVICES:
        subprocess.run(
            ["/usr/bin/systemctl", "stop", service],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    # Preserve the OS, networking, SSH, device identity, TLS certificate/key,
    # updater trust configuration, and installed application releases.
    (ETC / "web-auth.json").unlink(missing_ok=True)
    (ETC / "petcam.conf.last-good").unlink(missing_ok=True)
    (ETC / "cameras.json").unlink(missing_ok=True)
    (ETC / "cameras.lock").unlink(missing_ok=True)
    for path in (ETC / "cameras.d",):
        shutil.rmtree(path, ignore_errors=True)
        path.mkdir(parents=True, exist_ok=True)
        os.chown(path, -1, __import__("grp").getgrnam("petcam").gr_gid)
        os.chmod(path, 0o750)

    write_file(ETC / "petcam.conf", CAMERA_DEFAULTS)
    os.chown(ETC / "petcam.conf", __import__("pwd").getpwnam("petcam").pw_uid,
             __import__("grp").getgrnam("petcam").gr_gid)
    write_file(ETC / "home-assistant.conf", HOME_ASSISTANT_DEFAULTS)
    os.chown(ETC / "home-assistant.conf", __import__("pwd").getpwnam("petcam").pw_uid,
             __import__("grp").getgrnam("petcam").gr_gid)

    for name, defaults in (("timelapse", TIMELAPSE_DEFAULTS), ("motion", MOTION_DEFAULTS)):
        directory = STATE / name
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True, exist_ok=True)
        write_file(directory / "config.json", json.dumps(defaults, indent=2) + "\n")
        os.chown(directory / "config.json", __import__("pwd").getpwnam("petcam").pw_uid,
                 __import__("grp").getgrnam("petcam").gr_gid)
        os.chown(directory, __import__("pwd").getpwnam("petcam").pw_uid,
                 __import__("grp").getgrnam("petcam").gr_gid)
        os.chmod(directory, 0o750)

    (STATE / "health.json").unlink(missing_ok=True)
    run("/usr/bin/systemctl", "daemon-reload")
    for service in ("petcam-stream.service", "petcam-web.service",
                    "petcam-timelapse.service", "petcam-motion.service",
                    "petcam-home-assistant.service"):
        run("/usr/bin/systemctl", "start", service)


def main():
    if os.geteuid() != 0:
        raise SystemExit("Run with sudo: sudo petcam-admin")
    print("This will erase PetCam settings, administrator credentials, camera profiles,")
    print("timelapse frames, motion snapshots, and PetCam integration settings.")
    print("It will preserve Raspberry Pi OS, Wi-Fi/networking, SSH, TLS identity,")
    print("and installed software. The browser will require a new admin password.")
    if input('Type "RESET" to continue: ').strip() != "RESET":
        print("Factory reset cancelled.")
        return 1
    try:
        reset()
    except Exception as error:
        print(f"Factory reset failed: {error}", file=sys.stderr)
        return 1
    print("PetCam has been reset. Open https://petcam.local:8443/ to create a new admin password.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
