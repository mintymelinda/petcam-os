#!/usr/bin/env python3
"""Collect local network, listener, and PetCam endpoint diagnostics safely."""

from __future__ import annotations

import io
import json
import os
import re
import socket
import ssl
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import parse_config

CONFIG_FILE = Path("/etc/petcam/petcam.conf")
HEALTH_FILE = Path("/var/lib/petcam/health.json")
MANAGER_PORT = 8443
OBS_PORT = 8080
DEFAULT_BACKEND_PORT = 8081
SECRET_LINE = re.compile(
    r"(?i)(\b[\w.-]*(?:password|passwd|token|secret|authorization|cookie|credential|"
    r"private[_ -]?key|psk|ssid|api[_ -]?key)[\w.-]*\s*[:=]\s*).*$",
    re.MULTILINE,
)
BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
QUERY_SECRET = re.compile(
    r"(?i)([?&](?:token|key|secret|password|access_token|refresh_token)=)[^&\s]+"
)


def command_output(command: list[str], timeout: int = 5) -> str:
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
        return result.stdout if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def redact_text(value: str) -> str:
    value = SECRET_LINE.sub(r"\1[REDACTED]", value)
    value = BEARER.sub("Bearer [REDACTED]", value)
    return QUERY_SECRET.sub(r"\1[REDACTED]", value)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def collect_interfaces() -> list[dict[str, Any]]:
    interfaces = []
    try:
        names = [name for _, name in socket.if_nameindex()]
    except OSError:
        names = []
    for name in names:
        root = Path("/sys/class/net") / name
        state = _read_text(root / "operstate") or "unknown"
        carrier_value = _read_text(root / "carrier")
        carrier = carrier_value == "1" if carrier_value in {"0", "1"} else None
        kind = "wireless" if (root / "wireless").exists() else "wired_or_other"
        interfaces.append({
            "name": name,
            "type": kind,
            "state": state,
            "carrier": carrier,
        })
    return interfaces


def _parse_default_routes(raw: str) -> list[dict[str, str]]:
    try:
        routes = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(routes, list):
        return []
    return [
        {
            key: str(route[key])
            for key in ("dev", "gateway", "protocol", "metric")
            if key in route
        }
        for route in routes
        if isinstance(route, dict)
    ]


def _listener_report(raw: str, ports: tuple[int, ...] | None = None) -> dict[str, list[dict[str, str]]]:
    wanted = {str(port) for port in (ports or (MANAGER_PORT, OBS_PORT, DEFAULT_BACKEND_PORT))}
    result = {port: [] for port in sorted(wanted)}
    lines = raw.splitlines()
    for line in lines[1:]:
        fields = line.split()
        if len(fields) < 5:
            continue
        local = fields[3]
        port = local.rsplit(":", 1)[-1]
        if port in wanted:
            result[port].append({"state": fields[1], "local_address": local})
    return result


def probe_http(url: str, timeout: float = 3, stream: bool = False, tls: bool = False) -> dict[str, Any]:
    context = ssl._create_unverified_context() if tls else None
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=context) as response:
            if stream:
                ready = response.status == 200 and bool(response.read(1))
                return {"reachable": ready, "status": response.status, "frame_byte_received": ready}
            return {"reachable": 200 <= response.status < 500, "status": response.status}
    except (OSError, urllib.error.URLError, TimeoutError, ValueError):
        return {"reachable": False, "status": None}


def _addresses_from_ip(raw: str) -> list[dict[str, str]]:
    try:
        entries = json.loads(raw)
    except (TypeError, ValueError):
        return []
    addresses = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        for address in entry.get("addr_info", []):
            if not isinstance(address, dict) or not address.get("local"):
                continue
            addresses.append({
                "interface": str(entry.get("ifname", "unknown")),
                "family": str(address.get("family", "unknown")),
                "address": str(address["local"]),
                "scope": str(address.get("scope", "unknown")),
            })
    return addresses


def _mdns_resolution(name: str) -> list[str]:
    raw = command_output(["getent", "ahostsv4", name], timeout=3)
    addresses = []
    for line in raw.splitlines():
        fields = line.split()
        if fields and fields[0] not in addresses:
            addresses.append(fields[0])
    return addresses


def _read_health() -> dict[str, Any]:
    try:
        value = json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def collect_diagnostics() -> dict[str, Any]:
    try:
        config = parse_config(CONFIG_FILE)
        backend_port = int(config["PORT"])
        config_valid = True
        config_error = None
    except Exception as error:
        backend_port = DEFAULT_BACKEND_PORT
        config_valid = False
        config_error = str(error)

    hostname = socket.gethostname()
    mdns_name = hostname if hostname.endswith(".local") else f"{hostname}.local"
    mdns_addresses = _mdns_resolution(mdns_name)
    interfaces = collect_interfaces()
    addresses = _addresses_from_ip(command_output(["ip", "-j", "address", "show"]))
    routes = _parse_default_routes(command_output(["ip", "-j", "route", "show", "default"]))
    raw_listeners = _listener_report(
        command_output(["ss", "-ltn"]), (MANAGER_PORT, OBS_PORT, backend_port)
    )
    listeners = {
        "healthy": all(bool(entries) for entries in raw_listeners.values()),
        "ports": {
            port: {"listening": bool(entries), "bindings": entries}
            for port, entries in raw_listeners.items()
        },
    }

    backend_url = f"http://127.0.0.1:{backend_port}"
    backend_http = probe_http(backend_url + "/", timeout=3)
    backend_stream = probe_http(backend_url + "/stream", timeout=5, stream=True)
    manager_url = f"https://127.0.0.1:{MANAGER_PORT}/"
    manager = probe_http(manager_url, timeout=3, tls=True)
    obs_url = f"http://127.0.0.1:{OBS_PORT}/stream"
    obs_stream = probe_http(obs_url, timeout=5, stream=True)

    obs_urls = [f"http://{mdns_name}:{OBS_PORT}/stream"]
    for item in addresses:
        if item["family"] == "inet" and item["scope"] != "host":
            candidate = f"http://{item['address']}:{OBS_PORT}/stream"
            if candidate not in obs_urls:
                obs_urls.append(candidate)

    health = _read_health()
    layers = {
        "configuration": {"healthy": config_valid, "detail": "Valid" if config_valid else config_error},
        "camera": {
            "healthy": health.get("camera_connected") is not False,
            "detail": health.get("state", "not_checked"),
        },
        "capture_backend": {
            "healthy": backend_http["reachable"] and backend_stream["reachable"],
            "http": backend_http,
            "mjpeg": backend_stream,
        },
        "manager_https": manager,
        "obs_stream_proxy": obs_stream,
        "hostname_resolution": {
            "name": mdns_name,
            "resolved": bool(mdns_addresses),
            "addresses": mdns_addresses,
        },
        "network_link": {
            "healthy": any(item["state"] == "up" and item["name"] != "lo" for item in interfaces),
            "interfaces": interfaces,
        },
        "default_route": {"available": bool(routes), "routes": routes},
        "listeners": listeners,
    }
    problems = []
    if not config_valid:
        problems.append("Camera configuration is invalid.")
    if health.get("camera_connected") is False:
        problems.append("No supported camera is currently detected.")
    if not backend_http["reachable"] or not backend_stream["reachable"]:
        problems.append("The loopback capture backend is unavailable or is not producing MJPEG frames.")
    if not manager["reachable"]:
        problems.append("The local HTTPS management listener is unavailable.")
    if not obs_stream["reachable"]:
        problems.append("The local OBS stream proxy is unavailable or is not producing frames.")
    if not mdns_addresses:
        problems.append(f"mDNS name {mdns_name} did not resolve on the Pi.")
    if not layers["network_link"]["healthy"]:
        problems.append("No non-loopback network interface reports an up link.")
    if not routes:
        problems.append("No default network route was found.")
    for port, label in (
        (str(OBS_PORT), "OBS stream"),
        (str(backend_port), "capture backend"),
        (str(MANAGER_PORT), "HTTPS manager"),
    ):
        if not listeners["ports"][port]["listening"]:
            problems.append(f"No listening socket was found for the {label} port {port}.")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "hostname": hostname,
        "backend_port": backend_port,
        "obs_browser_source_urls": obs_urls,
        "layers": layers,
        "problems": problems,
        "remote_client_tested": False,
        "remote_client_note": (
            "All endpoint and name-resolution checks run on the Pi itself. "
            "They cannot prove that another LAN client can reach the Pi; "
            "client isolation, remote firewall rules, Wi-Fi segmentation, and "
            "the remote client's DNS/mDNS support must be checked from that client."
        ),
    }


def _redacted_config() -> str:
    try:
        lines = CONFIG_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return "Configuration file unavailable.\n"
    return redact_text("\n".join(lines) + "\n")


def _journal_excerpt() -> str:
    raw = command_output([
        "journalctl", "-u", "petcam-stream.service", "-u", "petcam-web.service",
        "-n", "120", "--no-pager", "-o", "short-iso",
    ], timeout=8)
    if not raw:
        return "Journal entries unavailable to the current user.\n"
    return redact_text(raw)


def create_support_bundle(performance: dict[str, Any] | None = None) -> bytes:
    diagnostics = collect_diagnostics()
    status = command_output(["/usr/local/bin/petcam-status"], timeout=15)
    files = {
        "diagnostics.json": json.dumps(diagnostics, indent=2) + "\n",
        "performance.json": json.dumps(performance or {}, indent=2) + "\n",
        "petcam-status.json": redact_text(status) if status else "petcam-status unavailable.\n",
        "petcam.conf.redacted": _redacted_config(),
        "journal.txt": _journal_excerpt(),
    }
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, value in files.items():
            data = value.encode("utf-8", errors="replace")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o600
            info.mtime = int(time.time())
            archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", action="store_true", help="write a redacted support archive")
    parser.add_argument("--output", type=Path, help="support archive path (requires --bundle)")
    args = parser.parse_args()
    if args.output and not args.bundle:
        parser.error("--output requires --bundle")
    if args.bundle:
        destination = args.output or Path.cwd() / (
            "petcam-support-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".tar.gz"
        )
        destination.write_bytes(create_support_bundle())
        os.chmod(destination, 0o600)
        if os.geteuid() == 0 and os.environ.get("SUDO_UID"):
            try:
                os.chown(destination, int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"]))
            except (KeyError, ValueError, OSError):
                pass
        print(destination)
        return 0
    print(json.dumps(collect_diagnostics(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
