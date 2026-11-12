#!/usr/bin/env python3
"""Create a Raspberry Pi Imager 2 local manifest for a PetCam OS image."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import lzma
from pathlib import Path


def image_metadata(image: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with lzma.open(image, "rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def create_manifest(image: Path, output: Path, icon: str | None = None) -> None:
    image = image.resolve()
    output = output.resolve()
    compressed_hash = hashlib.sha256()
    with image.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            compressed_hash.update(chunk)

    extract_size, extract_hash = image_metadata(image)
    entry = {
        "name": "PetCam OS",
        "description": "Camera-agnostic Raspberry Pi OS for LAN MJPEG streaming",
        "icon": icon or "https://raw.githubusercontent.com/mintymelinda/petcam-os/main/assets/petcam-icon.svg",
        "url": image.as_uri(),
        "extract_size": extract_size,
        "extract_sha256": extract_hash,
        "image_download_size": image.stat().st_size,
        "image_download_sha256": compressed_hash.hexdigest(),
        "release_date": datetime.date.fromtimestamp(image.stat().st_mtime).isoformat(),
        "init_format": "cloudinit-rpi",
        "devices": ["pi0", "pi0w", "pi02w", "pi1", "pi2", "pi3", "pi4", "pi5", "pi400", "cm1", "cm3", "cm4", "cm5"],
    }

    output.write_text(
        json.dumps({"os_list": [entry]}, indent=2) + chr(10),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path, help="PetCam .img.xz image")
    parser.add_argument(
        "--output",
        type=Path,
        help="Manifest output path (defaults beside the image)",
    )
    parser.add_argument("--icon", help="Optional icon URL")
    args = parser.parse_args()

    if not args.image.is_file() or args.image.suffix != ".xz":
        parser.error("image must be an existing .img.xz file")
    output = args.output or args.image.with_name("petcam-os.rpi-imager-manifest")
    create_manifest(args.image, output, args.icon)


if __name__ == "__main__":
    main()
