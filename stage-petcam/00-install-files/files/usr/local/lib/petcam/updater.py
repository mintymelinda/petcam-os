#!/usr/bin/env python3
"""Verify, stage, activate, and roll back signed PetCam application releases."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

ROOT = Path("/opt/petcam")
RELEASES = ROOT / "releases"
CURRENT = ROOT / "current"
STATE = Path("/var/lib/petcam/update")
CONFIG = Path("/etc/petcam/update.conf")
PUBLIC_KEY = Path("/etc/petcam/update-signing.pub")
MAX_MANIFEST = 64 * 1024
MAX_ARCHIVE = 300 * 1024 * 1024
REQUIRED_FILES = (
    "VERSION",
    "manifest.json",
    "lib/camera.py",
    "lib/csi_stream.py",
    "lib/web_manager.py",
    "lib/start-stream.sh",
    "vendor/bin/ustreamer",
)
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


class UpdateError(RuntimeError):
    pass


def version_tuple(value: str) -> tuple[int, int, int]:
    if not VERSION_RE.fullmatch(value):
        raise UpdateError(f"Invalid release version: {value!r}")
    return tuple(int(part) for part in value.split("."))


def read_config(path: Path = CONFIG) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        raise UpdateError(f"Update configuration is missing: {path}")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise UpdateError(f"Invalid update configuration line: {raw!r}")
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    manifest_url = values.get("MANIFEST_URL", "")
    if not manifest_url:
        raise UpdateError("MANIFEST_URL is not configured; signed updates are disabled")
    parsed = urllib.parse.urlparse(manifest_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise UpdateError("MANIFEST_URL must be an HTTPS URL")
    return values


def fetch(url: str, limit: int) -> bytes:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise UpdateError("Update downloads must use HTTPS")
    request = urllib.request.Request(url, headers={"User-Agent": "PetCam-Updater/1"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            final_url = urllib.parse.urlparse(response.geturl())
            if final_url.scheme != "https":
                raise UpdateError("Update server redirected to a non-HTTPS URL")
            length = response.headers.get("Content-Length")
            if length and int(length) > limit:
                raise UpdateError("Update download exceeds the configured size limit")
            data = response.read(limit + 1)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise UpdateError(f"Unable to download update data: {exc}") from exc
    if len(data) > limit:
        raise UpdateError("Update download exceeds the configured size limit")
    return data


def verify_signature(manifest: bytes, signature: bytes, public_key: Path = PUBLIC_KEY) -> None:
    if not public_key.is_file():
        raise UpdateError(f"Trusted update signing key is missing: {public_key}")
    with tempfile.TemporaryDirectory(prefix="petcam-verify-") as temp:
        manifest_path = Path(temp) / "manifest.json"
        signature_path = Path(temp) / "manifest.sig"
        manifest_path.write_bytes(manifest)
        signature_path.write_bytes(signature)
        result = subprocess.run(
            [
                "openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(public_key),
                "-sigfile", str(signature_path), "-rawin", "-in", str(manifest_path),
            ],
            capture_output=True,
            check=False,
            text=True,
        )
    if result.returncode != 0:
        raise UpdateError("Release manifest signature verification failed")


def parse_manifest(raw: bytes) -> dict[str, object]:
    if len(raw) > MAX_MANIFEST:
        raise UpdateError("Release manifest is too large")
    try:
        manifest = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("Release manifest is not valid JSON") from exc
    required = {"schema_version", "application", "version", "architecture",
                "archive_url", "archive_size", "archive_sha256"}
    if not isinstance(manifest, dict) or not required.issubset(manifest):
        raise UpdateError("Release manifest is missing required fields")
    if manifest["schema_version"] != 1 or manifest["application"] != "petcam-os":
        raise UpdateError("Unsupported release manifest")
    version_tuple(str(manifest["version"]))
    machine = platform.machine().lower()
    if manifest["architecture"] != "armhf" or machine not in ("armv6l", "armv7l", "armhf"):
        raise UpdateError(f"Release does not target this PetCam armhf device (machine={machine})")
    archive_url = manifest["archive_url"]
    if not isinstance(archive_url, str) or urllib.parse.urlparse(archive_url).scheme != "https":
        raise UpdateError("Release archive URL must use HTTPS")
    size = manifest["archive_size"]
    if not isinstance(size, int) or size < 1 or size > MAX_ARCHIVE:
        raise UpdateError("Release archive size is invalid")
    digest = manifest["archive_sha256"]
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise UpdateError("Release archive SHA-256 is invalid")
    return manifest


def current_version() -> str | None:
    try:
        target = CURRENT.resolve(strict=True)
    except FileNotFoundError:
        return None
    version_file = target / "VERSION"
    if not version_file.is_file():
        raise UpdateError("Active release has no VERSION file")
    value = version_file.read_text(encoding="utf-8").strip()
    version_tuple(value)
    return value


def check_update(config: dict[str, str]) -> dict[str, object]:
    manifest_url = config["MANIFEST_URL"]
    raw = fetch(manifest_url, MAX_MANIFEST)
    signature = fetch(manifest_url + ".sig", 4096)
    verify_signature(raw, signature, Path(config.get("PUBLIC_KEY", str(PUBLIC_KEY))))
    manifest = parse_manifest(raw)
    installed = current_version()
    available = str(manifest["version"])
    return {
        "installed_version": installed,
        "available_version": available,
        "update_available": installed is None or version_tuple(available) > version_tuple(installed),
        "release_notes": str(manifest.get("release_notes", "")),
        "archive_size": manifest["archive_size"],
    }


def safe_extract(archive: bytes, destination: Path) -> None:
    with tempfile.NamedTemporaryFile(prefix="petcam-release-", suffix=".tar.gz") as handle:
        handle.write(archive)
        handle.flush()
        try:
            with tarfile.open(handle.name, "r:gz") as bundle:
                members = bundle.getmembers()
                if not members:
                    raise UpdateError("Release archive is empty")
                for member in members:
                    name = PurePosixPath(member.name)
                    if name.is_absolute() or not name.parts or any(part in ("", ".", "..") for part in name.parts):
                        raise UpdateError("Release archive contains an unsafe path")
                    if not (member.isdir() or member.isfile()):
                        raise UpdateError("Release archive contains a link or special file")
                    target = destination.joinpath(*name.parts)
                    if not target.resolve().is_relative_to(destination.resolve()):
                        raise UpdateError("Release archive escapes the staging directory")
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        target.chmod(0o755)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = bundle.extractfile(member)
                    if source is None:
                        raise UpdateError("Unable to read release archive member")
                    with source, target.open("xb") as output:
                        shutil.copyfileobj(source, output)
                    target.chmod(0o755 if member.mode & 0o111 else 0o644)
        except (tarfile.TarError, OSError) as exc:
            if isinstance(exc, UpdateError):
                raise
            raise UpdateError(f"Unable to extract release archive: {exc}") from exc


def validate_release(directory: Path, version: str) -> None:
    for relative in REQUIRED_FILES:
        path = directory / relative
        if not path.is_file() or path.is_symlink():
            raise UpdateError(f"Release is missing required regular file: {relative}")
    actual = (directory / "VERSION").read_text(encoding="utf-8").strip()
    if actual != version:
        raise UpdateError("Archive VERSION does not match the signed manifest")
    try:
        embedded = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateError("Release's embedded manifest is invalid") from exc
    if embedded.get("application") != "petcam-os" or embedded.get("version") != version:
        raise UpdateError("Release's embedded manifest does not match the signed release")
    (directory / "lib/start-stream.sh").chmod(0o755)
    (directory / "vendor/bin/ustreamer").chmod(0o755)


def atomic_activate(release: Path) -> str | None:
    previous = current_version()
    temporary = ROOT / ".current.new"
    try:
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(release.relative_to(ROOT))
        os.replace(temporary, CURRENT)
    finally:
        temporary.unlink(missing_ok=True)
    return previous


def service(action: str) -> None:
    result = subprocess.run(
        ["systemctl", action, "petcam-stream.service", "petcam-web.service"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise UpdateError(f"Unable to {action} PetCam services: {result.stderr.strip()}")


def healthy() -> bool:
    for unit in ("petcam-stream.service", "petcam-web.service"):
        if subprocess.run(
            ["systemctl", "is-active", "--quiet", unit], check=False
        ).returncode != 0:
            return False
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/", timeout=3) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError):
        return False


def install_update(config: dict[str, str]) -> dict[str, object]:
    status = check_update(config)
    version = str(status["available_version"])
    if not status["update_available"]:
        return {"status": "up-to-date", **status}

    manifest_url = config["MANIFEST_URL"]
    raw_manifest = fetch(manifest_url, MAX_MANIFEST)
    signature = fetch(manifest_url + ".sig", 4096)
    verify_signature(raw_manifest, signature, Path(config.get("PUBLIC_KEY", str(PUBLIC_KEY))))
    manifest = parse_manifest(raw_manifest)
    archive = fetch(str(manifest["archive_url"]), MAX_ARCHIVE)
    if len(archive) != manifest["archive_size"]:
        raise UpdateError("Downloaded release size does not match the signed manifest")
    if hashlib.sha256(archive).hexdigest() != manifest["archive_sha256"]:
        raise UpdateError("Downloaded release SHA-256 does not match the signed manifest")

    STATE.mkdir(parents=True, exist_ok=True)
    RELEASES.mkdir(parents=True, exist_ok=True)
    staging_parent = STATE / "staging"
    staging_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="release-", dir=staging_parent) as temporary:
        staged = Path(temporary) / "release"
        staged.mkdir()
        safe_extract(archive, staged)
        validate_release(staged, version)
        final = RELEASES / version
        if final.exists():
            raise UpdateError(f"Release directory already exists: {final}")
        shutil.move(str(staged), str(final))
    old_version = current_version()
    switched = False
    try:
        atomic_activate(final)
        switched = True
        subprocess.run(["systemctl", "daemon-reload"], check=True, capture_output=True)
        service("restart")
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if healthy():
                return {"status": "installed", "installed_version": version,
                        "previous_version": old_version}
            time.sleep(2)
        raise UpdateError("New release failed its post-install health check")
    except Exception as exc:
        if switched and old_version:
            try:
                atomic_activate(RELEASES / old_version)
                subprocess.run(["systemctl", "daemon-reload"], check=True, capture_output=True)
                service("restart")
            except Exception as rollback_error:
                raise UpdateError(
                    f"Update failed ({exc}); automatic rollback also failed ({rollback_error})"
                ) from rollback_error
        raise UpdateError(f"Update failed and previous release was restored: {exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "install"))
    args = parser.parse_args()
    try:
        if args.command == "install" and os.geteuid() != 0:
            raise UpdateError("Installing updates requires root; use the petcam-update.service unit")
        config = read_config()
        result = check_update(config) if args.command == "check" else install_update(config)
        print(json.dumps(result, indent=2))
        return 0
    except UpdateError as exc:
        print(f"petcam-update: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
