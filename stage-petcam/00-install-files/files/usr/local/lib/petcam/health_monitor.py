#!/usr/bin/env python3
"""Detect stalled PetCam streams and recover only the affected service."""

from __future__ import annotations

import http.client
import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_FILE = Path("/var/lib/petcam/health.json")
BACKEND_STREAM_URL = "http://127.0.0.1:8081/stream"
PUBLIC_STREAM_URL = "http://127.0.0.1:8080/stream"
STARTUP_GRACE_SECONDS = 30
BACKEND_FRAME_TIMEOUT_SECONDS = 15
PUBLIC_FRAME_TIMEOUT_SECONDS = 5
BACKOFF_BASE_SECONDS = 30
BACKOFF_MAX_SECONDS = 300


def timestamp(now: float | None = None) -> str:
    return datetime.fromtimestamp(now if now is not None else time.time(), timezone.utc).isoformat()


def load_state() -> dict[str, Any]:
    try:
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o644)
    temporary.replace(STATE_FILE)


def service_snapshot(service: str) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [
                "/usr/bin/systemctl", "show", service,
                "--property=ActiveState", "--property=ActiveEnterTimestampMonotonic", "--value",
            ],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if result.returncode:
            return {"state": "unknown", "active_age": None}
        values = result.stdout.splitlines()
        active_state = values[0].strip() if values else "unknown"
        entered = int(values[1].strip()) if len(values) > 1 and values[1].strip().isdigit() else 0
        age = max(0.0, time.monotonic() - entered / 1_000_000) if entered else None
        return {"state": active_state, "active_age": age}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {"state": "unknown", "active_age": None}


def probe_stream(url: str, timeout: int) -> bool:
    """Require an actual first MJPEG body byte, not merely an open TCP socket."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if response.status != 200:
                return False
            return bool(response.read(1))
    except (OSError, urllib.error.URLError, TimeoutError, ValueError, http.client.HTTPException):
        return False


def camera_present() -> bool | None:
    """Return None when discovery failed; do not mistake a failed probe for no camera."""
    try:
        result = subprocess.run(
            ["/usr/local/bin/petcam-camera", "discover"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode:
            return None
        inventory = json.loads(result.stdout)
        return bool(inventory.get("usb_devices") or inventory.get("csi_cameras"))
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        return None


def restart_service(service: str) -> tuple[bool, str]:
    try:
        if service == "petcam-web.service":
            # Exit so the existing Restart=always policy replaces the web manager.
            os.kill(os.getpid(), signal.SIGTERM)
            return True, "Manager process exited for systemd recovery."
        result = subprocess.run(
            ["sudo", "-n", "/usr/bin/systemctl", "restart", service],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode:
            return False, result.stderr.strip() or f"systemctl restart {service} failed"
        return True, ""
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error)


def _persist(report: dict[str, Any]) -> dict[str, Any]:
    save_state(report)
    return report


def _starting(snapshot: dict[str, Any]) -> bool:
    return snapshot["state"] in ("activating", "reloading", "deactivating") or (
        snapshot["state"] == "active"
        and snapshot["active_age"] is not None
        and snapshot["active_age"] < STARTUP_GRACE_SECONDS
    )


def _recover(report: dict[str, Any], previous: dict[str, Any], service: str,
             reason: str, now: float) -> dict[str, Any]:
    failures = int(previous.get("consecutive_failures", 0)) + 1
    delay = min(BACKOFF_BASE_SECONDS * (2 ** min(failures - 1, 4)), BACKOFF_MAX_SECONDS)
    next_allowed = float(previous.get("next_recovery_at", 0))
    report["consecutive_failures"] = failures
    report["recovery_target"] = service
    report["reason"] = reason
    if now < next_allowed:
        report["state"] = "recovery_backoff"
        report["next_recovery_at"] = next_allowed
        return _persist(report)

    report["recovery_count"] = int(previous.get("recovery_count", 0)) + 1
    report["last_recovery_at"] = timestamp(now)
    report["next_recovery_at"] = now + delay
    report["last_recovery_result"] = "requested"
    report["state"] = "recovering"
    _persist(report)
    succeeded, error = restart_service(service)
    report["last_recovery_result"] = "requested" if succeeded else error
    report["state"] = "recovering" if succeeded else "recovery_failed"
    return _persist(report)


def check_health(now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    previous = load_state()
    report: dict[str, Any] = {
        "state": "starting",
        "reason": "",
        "last_checked_at": timestamp(now),
        "last_healthy_at": previous.get("last_healthy_at"),
        "last_recovery_at": previous.get("last_recovery_at"),
        "next_recovery_at": previous.get("next_recovery_at", 0),
        "recovery_count": int(previous.get("recovery_count", 0)),
        "consecutive_failures": int(previous.get("consecutive_failures", 0)),
        "recovery_target": previous.get("recovery_target"),
        "last_recovery_result": previous.get("last_recovery_result"),
    }

    backend = service_snapshot("petcam-stream.service")
    web = service_snapshot("petcam-web.service")
    report["backend_service_state"] = backend["state"]
    report["web_service_state"] = web["state"]
    report["backend_stream_ready"] = False
    report["public_stream_ready"] = False
    report["camera_connected"] = None

    if backend["state"] == "unknown" or web["state"] == "unknown":
        report["state"] = "service_state_unknown"
        report["reason"] = "systemd service state could not be read; no restart was attempted."
        return _persist(report)
    if _starting(backend) or _starting(web):
        report["state"] = "starting"
        report["reason"] = "A PetCam service is within its startup grace period."
        return _persist(report)

    backend_ready = backend["state"] == "active" and probe_stream(
        BACKEND_STREAM_URL, BACKEND_FRAME_TIMEOUT_SECONDS
    )
    report["backend_stream_ready"] = backend_ready
    if not backend_ready:
        present = camera_present()
        report["camera_connected"] = present
        if present is False:
            report["state"] = "camera_disconnected"
            report["reason"] = "No supported USB or CSI camera is currently detected; waiting for hardware."
            return _persist(report)
        if present is None:
            report["state"] = "camera_detection_failed"
            report["reason"] = "Camera discovery failed; no service restart was attempted."
            return _persist(report)
        reason = (
            "Capture service is not active."
            if backend["state"] != "active"
            else "Capture service is active but the local MJPEG stream produced no frame."
        )
        return _recover(report, previous, "petcam-stream.service", reason, now)

    if _starting(web):
        report["state"] = "starting"
        report["reason"] = "The web proxy is within its startup grace period."
        return _persist(report)
    if web["state"] != "active":
        return _recover(
            report, previous, "petcam-web.service",
            "The web manager process is not active while the local capture stream is healthy.", now,
        )

    public_ready = probe_stream(PUBLIC_STREAM_URL, PUBLIC_FRAME_TIMEOUT_SECONDS)
    report["public_stream_ready"] = public_ready
    if not public_ready:
        return _recover(
            report, previous, "petcam-web.service",
            "The local capture stream is healthy but the public OBS stream proxy is not producing frames.", now,
        )

    report["state"] = "healthy"
    report["reason"] = ""
    report["last_healthy_at"] = timestamp(now)
    report["consecutive_failures"] = 0
    report["next_recovery_at"] = 0
    report["recovery_target"] = None
    report["last_recovery_result"] = None
    return _persist(report)


def main() -> int:
    report = check_health()
    print(json.dumps(report, indent=2))
    return 0 if report["state"] in ("healthy", "starting", "camera_disconnected") else 1


if __name__ == "__main__":
    raise SystemExit(main())
