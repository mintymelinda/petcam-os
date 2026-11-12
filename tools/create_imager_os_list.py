#!/usr/bin/env python3
"""Generate a Raspberry Pi Imager OS sublist for a published PetCam release."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import lzma
from pathlib import Path

DEVICES = ["pi0", "pi0w", "pi02w", "pi1", "pi2", "pi3", "pi4", "pi5", "pi400", "cm1", "cm3", "cm4", "cm5"]


def digest_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def extracted_metadata(image: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with lzma.open(image, "rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def create_os_list(image: Path, image_url: str, version: str, output: Path) -> None:
    if not image.is_file() or image.suffixes[-2:] != [".img", ".xz"]:
        raise ValueError("image must be an existing .img.xz file")
    if not image_url.startswith("https://"):
        raise ValueError("image URL must use HTTPS")
    download_size, download_hash = digest_file(image)
    extract_size, extract_hash = extracted_metadata(image)
    entry = {
        "name": "PetCam OS",
        "description": "Camera-agnostic Raspberry Pi OS for LAN MJPEG streaming",
        "icon": "https://raw.githubusercontent.com/mintymelinda/petcam-os/main/assets/petcam-icon.svg",
        "website": "https://github.com/mintymelinda/petcam-os",
        "url": image_url,
        "release_date": dt.datetime.now(dt.timezone.utc).date().isoformat(),
        "extract_size": extract_size,
        "extract_sha256": extract_hash,
        "image_download_size": download_size,
        "image_download_sha256": download_hash,
        "devices": DEVICES,
        "init_format": "cloudinit-rpi",
        "architecture": "armhf",
    }
    output.write_text(json.dumps({"os_list": [entry]}, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path, help="Published PetCam .img.xz image")
    parser.add_argument("--image-url", required=True, help="Public HTTPS release asset URL")
    parser.add_argument("--version", required=True, help="PetCam release version")
    parser.add_argument("--output", type=Path, default=Path("petcam-os-os-list.json"))
    args = parser.parse_args()
    create_os_list(args.image, args.image_url, args.version, args.output)


if __name__ == "__main__":
    main()
