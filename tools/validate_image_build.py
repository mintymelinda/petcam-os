#!/usr/bin/env python3
"""Validate a pi-gen image and produce its Raspberry Pi Imager catalogue."""

from __future__ import annotations

import json
import lzma
import os
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


def validate_image(image: Path) -> None:
    if not image.is_file() or image.suffixes[-2:] != [".img", ".xz"]:
        raise SystemExit(f"pi-gen did not produce a valid .img.xz file: {image}")
    try:
        with lzma.open(image, "rb") as stream:
            while stream.read(1024 * 1024):
                pass
    except (lzma.LZMAError, EOFError, OSError) as error:
        raise SystemExit(f"Generated image failed XZ integrity validation: {error}") from error


def main() -> int:
    image = Path(require_environment("IMAGE_PATH")).resolve()
    image_url = require_environment("IMAGE_URL")
    version = require_environment("RELEASE_VERSION")
    schema_path = Path(require_environment("IMAGE_SCHEMA_PATH")).resolve()

    if not version.replace(".", "").isdigit() or version.count(".") != 2:
        raise SystemExit(f"Release version must be MAJOR.MINOR.PATCH: {version}")
    if not image_url.startswith("https://"):
        raise SystemExit("IMAGE_URL must use HTTPS")
    if not schema_path.is_file():
        raise SystemExit(f"Raspberry Pi Imager schema is missing: {schema_path}")

    validate_package_manifest()
    validate_image(image)

    catalogue_path = ROOT / "petcam-os-os-list.json"
    os_list_tool = ROOT / "tools/create_imager_os_list.py"
    import subprocess

    subprocess.run(
        [
            sys.executable,
            str(os_list_tool),
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
