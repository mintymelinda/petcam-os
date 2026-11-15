#!/usr/bin/env python3
"""Validate a pi-gen image and produce its Raspberry Pi Imager catalogue."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from jsonschema import validate

ROOT = Path(__file__).resolve().parents[1]


def require_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Required environment variable is missing: {name}")
    return value


def validate_package_manifest() -> None:
    declared = (ROOT / "build/apt-packages.txt").read_text(encoding="utf-8").splitlines()
    stage = (
        ROOT / "stage-petcam-base/01-install-packages/00-packages"
    ).read_text(encoding="utf-8").splitlines()
    if declared != stage:
        raise SystemExit(
            "build/apt-packages.txt must exactly match the pi-gen package stage; "
            "update both from the same reviewed package list."
        )


def main() -> int:
    image = Path(require_environment("IMAGE_PATH")).resolve()
    image_url = require_environment("IMAGE_URL")
    version = require_environment("RELEASE_VERSION")
    schema_path = Path(require_environment("IMAGE_SCHEMA_PATH")).resolve()

    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise SystemExit(f"Release version must be MAJOR.MINOR.PATCH: {version}")
    if not image_url.startswith("https://"):
        raise SystemExit("IMAGE_URL must use HTTPS")
    if not schema_path.is_file():
        raise SystemExit(f"Raspberry Pi Imager schema is missing: {schema_path}")
    if not image.is_file() or image.suffixes[-2:] != [".img", ".xz"]:
        raise SystemExit(f"pi-gen did not produce a valid .img.xz file: {image}")

    validate_package_manifest()

    # create_imager_os_list streams the compressed image to calculate both
    # compressed and expanded hashes, also proving that the XZ stream is valid.
    catalogue_path = ROOT / "petcam-os-os-list.json"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/create_imager_os_list.py"),
            str(image),
            "--image-url",
            image_url,
            "--output",
            str(catalogue_path),
        ],
        check=True,
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    catalogue = json.loads(catalogue_path.read_text(encoding="utf-8"))
    validate(instance=catalogue, schema=schema)
    entry = catalogue["os_list"][0]
    if entry.get("architecture") != "armhf":
        raise SystemExit("The current supported image target must remain armhf.")
    if entry.get("init_format") != "cloudinit-rpi":
        raise SystemExit("The generated Imager catalogue must retain cloudinit-rpi setup.")
    print(f"Validated {image.name} ({image.stat().st_size} compressed bytes)")
    print("Validated armhf image metadata and Raspberry Pi Imager schema.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
