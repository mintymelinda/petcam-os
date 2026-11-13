#!/usr/bin/env python3
"""Verify, stage, activate, and roll back signed PetCam application releases."""

from __future__ import annotations

import argparse
import grp
import hashlib
import json
import os
import platform
import re
import ssl
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
MAX_RECENT_RELEASES = 30
MAX_VALID_RELEASES = 10
MAX_ARCHIVE = 300 * 1024 * 1024
MAX_EXPANDED_ARCHIVE = 512 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10000
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
    "lib/home_assistant.py",
    "lib/timelapse.py",
    "lib/motion_detection.py",
    "systemd/petcam-motion.service",
    "systemd/petcam-home-assistant.service",
    "systemd/petcam-timelapse.service",
    "etc/petcam/home-assistant.conf",
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



def download_archive(url: str, destination: Path, expected_size: int, expected_digest: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise UpdateError("Update downloads must use HTTPS")
    request = urllib.request.Request(url, headers={"User-Agent": "PetCam-Updater/1"})
    digest = hashlib.sha256()
    received = 0
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if urllib.parse.urlparse(response.geturl()).scheme != "https":
                raise UpdateError("Update server redirected to a non-HTTPS URL")
            length = response.headers.get("Content-Length")
            if length and int(length) != expected_size:
                raise UpdateError("Release Content-Length does not match the signed manifest")
            with destination.open("wb") as output:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > expected_size or received > MAX_ARCHIVE:
                        raise UpdateError("Release archive exceeds its signed size limit")
                    digest.update(chunk)
                    output.write(chunk)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        destination.unlink(missing_ok=True)
        if isinstance(exc, UpdateError):
            raise
        raise UpdateError(f"Unable to download release archive: {exc}") from exc
    if received != expected_size:
        destination.unlink(missing_ok=True)
        raise UpdateError("Downloaded release size does not match the signed manifest")
    if digest.hexdigest() != expected_digest:
        destination.unlink(missing_ok=True)
        raise UpdateError("Downloaded release SHA-256 does not match the signed manifest")


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
    if manifest["architecture"] != "armhf":
        raise UpdateError("Release does not target the PetCam armhf image")
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


def release_manifest_candidates(manifest_url: str, key_path: Path) -> list[dict[str, object]]:
    """Find independently verifiable manifests in recent published GitHub releases."""
    parsed = urllib.parse.urlparse(manifest_url)
    match = re.fullmatch(
        r"/([^/]+)/([^/]+)/releases/latest/download/([^/]+)",
        parsed.path,
    )
    if parsed.netloc.lower() != "github.com" or not match:
        # Retain compatibility with explicitly configured signed manifest URLs.
        raw = fetch(manifest_url, MAX_MANIFEST)
        signature = fetch(manifest_url + ".sig", 4096)
        verify_signature(raw, signature, Path(key_path))
        manifest = parse_manifest(raw)
        return [{"manifest": manifest, "manifest_url": manifest_url, "tag_name": ""}]

    owner, repository, manifest_name = match.groups()
    api_url = (
        f"https://api.github.com/repos/{owner}/{repository}/releases"
        f"?per_page={MAX_RECENT_RELEASES}"
    )
    try:
        releases = json.loads(fetch(api_url, 2 * 1024 * 1024))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("GitHub Releases API returned invalid JSON") from exc
    if not isinstance(releases, list):
        raise UpdateError("GitHub Releases API returned an invalid response")

    valid: list[dict[str, object]] = []
    for release in releases:
        if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
            continue
        assets = release.get("assets")
        if not isinstance(assets, list):
            continue
        asset_urls = {
            asset.get("name"): asset.get("browser_download_url")
            for asset in assets if isinstance(asset, dict)
        }
        candidate_url = asset_urls.get(manifest_name)
        signature_url = asset_urls.get(manifest_name + ".sig")
        if not isinstance(candidate_url, str) or not isinstance(signature_url, str):
            continue
        try:
            raw = fetch(candidate_url, MAX_MANIFEST)
            signature = fetch(signature_url, 4096)
            verify_signature(raw, signature, Path(key_path))
            manifest = parse_manifest(raw)
            version = str(manifest["version"])
            if release.get("tag_name") not in (None, "", "v" + version, version):
                raise UpdateError("Release tag does not match its signed manifest version")
            valid.append({
                "manifest": manifest,
                "manifest_url": candidate_url,
                "tag_name": str(release.get("tag_name", "")),
                "published_at": str(release.get("published_at", "")),
            })
        except (UpdateError, ValueError, TypeError):
            # A broken or unsigned release must not prevent checking older releases.
            continue

    valid.sort(key=lambda item: version_tuple(str(item["manifest"]["version"])), reverse=True)
    return valid[:MAX_VALID_RELEASES]


def check_update(config: dict[str, str]) -> dict[str, object]:
    machine = platform.machine().lower()
    if machine not in ("armv6l", "armv7l", "armhf"):
        raise UpdateError(f"Updater requires a 32-bit ARM PetCam image (machine={machine})")
    key_path = Path(config.get("PUBLIC_KEY", str(PUBLIC_KEY)))
    manifest_url = config["MANIFEST_URL"]
    candidates = release_manifest_candidates(manifest_url, key_path)
    if not candidates:
        raise UpdateError("No valid signed PetCam releases were found in recent GitHub releases")

    installed = current_version()
    releases = []
    for candidate in candidates:
        manifest = candidate["manifest"]
        version = str(manifest["version"])
        releases.append({
            "version": version,
            "update_available": installed is None or version_tuple(version) > version_tuple(installed),
            "release_notes": str(manifest.get("release_notes", "")),
            "archive_size": manifest["archive_size"],
            "published_at": candidate.get("published_at", ""),
            "tag_name": candidate.get("tag_name", ""),
            "manifest_url": candidate["manifest_url"],
        })
    newest = releases[0]
    return {
        "installed_version": installed,
        "available_version": newest["version"],
        "update_available": bool(newest["update_available"]),
        "release_notes": newest["release_notes"],
        "archive_size": newest["archive_size"],
        "releases": releases,
    }


def safe_extract(archive: bytes | Path, destination: Path) -> None:
    temporary_archive = None
    if isinstance(archive, bytes):
        temporary_archive = tempfile.NamedTemporaryFile(prefix="petcam-release-", suffix=".tar.gz")
        temporary_archive.write(archive)
        temporary_archive.flush()
        archive_path = temporary_archive.name
    else:
        archive_path = str(archive)
    try:
        with tarfile.open(archive_path, "r:gz") as bundle:
            members = bundle.getmembers()
            if not members:
                raise UpdateError("Release archive is empty")
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise UpdateError("Release archive contains too many entries")
            expanded_size = sum(member.size for member in members if member.isfile())
            if expanded_size > MAX_EXPANDED_ARCHIVE:
                raise UpdateError("Expanded release archive exceeds the size limit")
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
        raise UpdateError(f"Unable to extract release archive: {exc}") from exc
    finally:
        if temporary_archive is not None:
            temporary_archive.close()


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


def install_runtime_integration_files(release: Path, etc_root: Path = Path("/etc")) -> None:
    """Install signed optional integration assets without overwriting user settings."""
    service_source = release / "systemd/petcam-home-assistant.service"
    config_source = release / "etc/petcam/home-assistant.conf"
    service_destination = etc_root / "systemd/system/petcam-home-assistant.service"
    config_destination = etc_root / "petcam/home-assistant.conf"
    timelapse_source = release / "systemd/petcam-timelapse.service"
    timelapse_destination = etc_root / "systemd/system/petcam-timelapse.service"
    motion_source = release / "systemd/petcam-motion.service"
    motion_destination = etc_root / "systemd/system/petcam-motion.service"
    service_destination.parent.mkdir(parents=True, exist_ok=True)
    config_destination.parent.mkdir(parents=True, exist_ok=True)
    timelapse_destination.parent.mkdir(parents=True, exist_ok=True)
    motion_destination.parent.mkdir(parents=True, exist_ok=True)

    service_temporary = service_destination.with_name(service_destination.name + ".new")
    shutil.copyfile(service_source, service_temporary)
    os.chmod(service_temporary, 0o644)
    os.replace(service_temporary, service_destination)
    timelapse_temporary = timelapse_destination.with_name(timelapse_destination.name + ".new")
    shutil.copyfile(timelapse_source, timelapse_temporary)
    os.chmod(timelapse_temporary, 0o644)
    os.replace(timelapse_temporary, timelapse_destination)
    motion_temporary = motion_destination.with_name(motion_destination.name + ".new")
    shutil.copyfile(motion_source, motion_temporary)
    os.chmod(motion_temporary, 0o644)
    os.replace(motion_temporary, motion_destination)
    config_destination_is_new = not config_destination.exists()
    if config_destination_is_new:
        config_temporary = config_destination.with_name(config_destination.name + ".new")
        shutil.copyfile(config_source, config_temporary)
        os.chmod(config_temporary, 0o640)
        os.replace(config_temporary, config_destination)
    elif config_destination.is_symlink() or not config_destination.is_file():
        raise UpdateError("Refusing to use a non-regular Home Assistant configuration file.")
    os.chmod(config_destination, 0o640)
    try:
        group_id = grp.getgrnam("petcam").gr_gid
        if os.geteuid() == 0:
            os.chown(service_destination, 0, 0)
            os.chown(timelapse_destination, 0, 0)
            os.chown(motion_destination, 0, 0)
            os.chown(config_destination, 0, group_id)
    except KeyError as error:
        raise UpdateError("The PetCam system group is missing; cannot secure Home Assistant configuration.") from error



def repair_config_directory_permissions(directory: Path = Path("/etc/petcam")) -> None:
    """Restore the writable, group-restricted config directory used by the web manager."""
    try:
        if directory.is_symlink():
            raise UpdateError(f"Refusing to repair a symlinked PetCam configuration directory: {directory}")
        directory.mkdir(parents=True, exist_ok=True)
        group_id = grp.getgrnam("petcam").gr_gid
        os.chown(directory, 0, group_id)
        os.chmod(directory, 0o1770)
    except UpdateError:
        raise
    except (OSError, KeyError) as error:
        raise UpdateError(
            f"Unable to secure PetCam configuration directory {directory}: {error}"
        ) from error


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


def restart_services() -> str:
    web = subprocess.run(
        ["systemctl", "restart", "petcam-web.service"],
        capture_output=True, text=True, check=False,
    )
    if web.returncode:
        raise UpdateError(f"Unable to restart PetCam web service: {web.stderr.strip()}")
    stream = subprocess.run(
        ["systemctl", "restart", "petcam-stream.service"],
        capture_output=True, text=True, check=False,
    )
    if stream.returncode:
        return "Camera stream did not start; check camera connection and petcam-stream.service logs"
    warnings = []
    integration_active = subprocess.run(
        ["systemctl", "is-active", "--quiet", "petcam-home-assistant.service"],
        check=False,
    )
    if integration_active.returncode == 0:
        integration = subprocess.run(
            ["systemctl", "restart", "petcam-home-assistant.service"],
            capture_output=True, text=True, check=False,
        )
        if integration.returncode:
            warnings.append("Home Assistant MQTT publisher did not restart; check petcam-home-assistant.service logs")
    timelapse_active = subprocess.run(
        ["systemctl", "is-active", "--quiet", "petcam-timelapse.service"],
        check=False,
    )
    if timelapse_active.returncode == 0:
        timelapse = subprocess.run(
            ["systemctl", "restart", "petcam-timelapse.service"],
            capture_output=True, text=True, check=False,
        )
        if timelapse.returncode:
            warnings.append("Timelapse worker did not restart; check petcam-timelapse.service logs")
    motion_active = subprocess.run(
        ["systemctl", "is-active", "--quiet", "petcam-motion.service"],
        check=False,
    )
    if motion_active.returncode == 0:
        motion = subprocess.run(
            ["systemctl", "restart", "petcam-motion.service"],
            capture_output=True, text=True, check=False,
        )
        if motion.returncode:
            warnings.append("Motion worker did not restart; check petcam-motion.service logs")
    return "; ".join(warnings)


def healthy() -> bool:
    if subprocess.run(
        ["systemctl", "is-active", "--quiet", "petcam-web.service"], check=False
    ).returncode != 0:
        return False
    try:
        with urllib.request.urlopen("https://127.0.0.1:8443/", context=ssl._create_unverified_context(), timeout=3) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError):
        return False


def remove_inactive_release(release: Path) -> bool:
    """Remove a failed release directory unless it is still the active release."""
    try:
        active = CURRENT.resolve(strict=True)
    except FileNotFoundError:
        active = None
    if active == release.resolve():
        return False
    shutil.rmtree(release)
    return True


def install_update(config: dict[str, str]) -> dict[str, object]:
    status = check_update(config)
    version = str(status["available_version"])
    if not status["update_available"]:
        return {"status": "up-to-date", **status}

    selected = status["releases"][0]
    manifest_url = str(selected["manifest_url"])
    raw_manifest = fetch(manifest_url, MAX_MANIFEST)
    signature = fetch(manifest_url + ".sig", 4096)
    verify_signature(raw_manifest, signature, Path(config.get("PUBLIC_KEY", str(PUBLIC_KEY))))
    manifest = parse_manifest(raw_manifest)
    STATE.mkdir(parents=True, exist_ok=True)
    RELEASES.mkdir(parents=True, exist_ok=True)
    staging_parent = STATE / "staging"
    staging_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="release-", dir=staging_parent) as temporary:
        archive_path = Path(temporary) / "release.tar.gz"
        download_archive(
            str(manifest["archive_url"]), archive_path,
            int(manifest["archive_size"]), str(manifest["archive_sha256"]),
        )
        staged = Path(temporary) / "release"
        staged.mkdir()
        safe_extract(archive_path, staged)
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
        repair_config_directory_permissions()
        install_runtime_integration_files(final)
        subprocess.run(["systemctl", "daemon-reload"], check=True, capture_output=True)
        subprocess.run(["systemctl", "enable", "--now", "petcam-timelapse.service"], check=True, capture_output=True)
        subprocess.run(["systemctl", "enable", "--now", "petcam-motion.service"], check=True, capture_output=True)
        stream_warning = restart_services()
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if healthy():
                return {"status": "installed", "installed_version": version,
                        "previous_version": old_version, "warning": stream_warning}
            time.sleep(2)
        raise UpdateError("New release failed its post-install health check")
    except Exception as exc:
        if switched and old_version:
            try:
                atomic_activate(RELEASES / old_version)
                subprocess.run(["systemctl", "daemon-reload"], check=True, capture_output=True)
                restart_services()
            except Exception as rollback_error:
                raise UpdateError(
                    f"Update failed ({exc}); automatic rollback also failed ({rollback_error})"
                ) from rollback_error
        try:
            if final.exists():
                remove_inactive_release(final)
        except OSError as cleanup_error:
            raise UpdateError(
                f"Update failed and previous release was restored: {exc}; "
                f"unable to remove inactive failed release {final}: {cleanup_error}"
            ) from cleanup_error
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
