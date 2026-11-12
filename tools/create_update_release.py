#!/usr/bin/env python3
"""Build and sign a PetCam application update from an image release tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tarfile
from pathlib import Path

REQUIRED_FILES = (
    "VERSION",
    "manifest.json",
    "lib/camera.py",
    "lib/csi_stream.py",
    "lib/web_manager.py",
    "lib/web_auth.py",
    "lib/health_monitor.py",
    "lib/config.py",
    "lib/network_diagnostics.py",
    "lib/camera_profiles.py",
    "lib/start-stream.sh",
    "vendor/bin/ustreamer",
)
MAX_ARCHIVE = 300 * 1024 * 1024


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_release(release_dir: Path, version: str, archive_url: str,
                  private_key: Path, output_dir: Path) -> tuple[Path, Path, Path]:
    if not release_dir.is_dir():
        raise ValueError(f"Release directory does not exist: {release_dir}")
    if not archive_url.startswith("https://"):
        raise ValueError("Archive URL must use HTTPS")
    actual_version = (release_dir / "VERSION").read_text(encoding="utf-8").strip()
    if actual_version != version:
        raise ValueError(f"Release tree version {actual_version!r} does not match {version!r}")
    embedded = json.loads((release_dir / "manifest.json").read_text(encoding="utf-8"))
    if embedded.get("application") != "petcam-os" or embedded.get("version") != version:
        raise ValueError("Embedded release manifest does not match the requested application version")
    for relative in REQUIRED_FILES:
        path = release_dir / relative
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Release tree is missing a required regular file: {relative}")
    if not private_key.is_file():
        raise ValueError(f"Signing private key does not exist: {private_key}")

    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"petcam-{version}-armhf.tar.gz"
    manifest_path = output_dir / "petcam-update-manifest.json"
    signature_path = output_dir / "petcam-update-manifest.json.sig"
    with tarfile.open(archive, "w:gz", compresslevel=9) as bundle:
        for relative in REQUIRED_FILES:
            bundle.add(release_dir / relative, arcname=relative, recursive=False)
        # Include any future regular application assets while refusing links/special files.
        for path in sorted(release_dir.rglob("*")):
            relative = path.relative_to(release_dir).as_posix()
            if relative in REQUIRED_FILES or path.is_dir():
                continue
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"Release tree contains an unsupported entry: {relative}")
            bundle.add(path, arcname=relative, recursive=False)
    archive_size = archive.stat().st_size
    if archive_size < 1 or archive_size > MAX_ARCHIVE:
        archive.unlink(missing_ok=True)
        raise ValueError(f"Release archive size is outside the allowed range: {archive_size}")

    manifest = {
        "schema_version": 1,
        "application": "petcam-os",
        "version": version,
        "architecture": "armhf",
        "archive_url": archive_url,
        "archive_size": archive_size,
        "archive_sha256": sha256(archive),
    }
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.chmod(manifest_path, 0o644)
    subprocess.run(
        ["openssl", "pkeyutl", "-sign", "-inkey", str(private_key),
         "-rawin", "-in", str(manifest_path), "-out", str(signature_path)],
        check=True,
        capture_output=True,
    )
    os.chmod(signature_path, 0o644)
    return archive, manifest_path, signature_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--archive-url", required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    archive, manifest, signature = build_release(
        args.release_dir, args.version, args.archive_url, args.private_key, args.output_dir
    )
    print(f"Created {archive} ({archive.stat().st_size} bytes)")
    print(f"Created {manifest}")
    print(f"Created {signature}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
