#!/usr/bin/env python3
"""Low-overhead Wi-Fi and network-interface telemetry for PetCam."""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_CACHE: dict[str, Any] | None = None
_CACHE_AT = 0.0
_PREVIOUS: dict[str, Any] = {}
_INTERFACE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,32}$")


def _command(args: list[str], timeout: float = 2.0) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        return result.stdout if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _number(pattern: str, value: str, cast=float):
    match = re.search(pattern, value, re.MULTILINE | re.IGNORECASE)
    if not match:
        return None
    try:
        return cast(match.group(1))
    except (TypeError, ValueError):
        return None


def _default_interface() -> str | None:
    raw = _command(["/usr/sbin/ip", "-j", "route", "show", "default"])
    try:
        routes = json.loads(raw)
    except (TypeError, ValueError):
        routes = []
    for route in routes if isinstance(routes, list) else []:
        name = route.get("dev") if isinstance(route, dict) else None
        if isinstance(name, str) and _INTERFACE_RE.fullmatch(name) and name != "lo":
            return name
    try:
        names = sorted(path.name for path in Path("/sys/class/net").glob("*") if path.name != "lo")
    except OSError:
        names = []
    return names[0] if names else None


def _read_counter(interface: str, name: str) -> int | None:
    try:
        return int((Path("/sys/class/net") / interface / "statistics" / name).read_text().strip())
    except (OSError, ValueError):
        return None


def collect_network_performance() -> dict[str, Any]:
    """Return cached wireless link data and measured interface throughput."""
    global _CACHE, _CACHE_AT
    now = time.monotonic()
    with _LOCK:
        if _CACHE is not None and now - _CACHE_AT < 4.0:
            return dict(_CACHE)
        interface = _default_interface()
        if not interface:
            result = {"available": False, "type": "unknown", "interface_name": None}
            _CACHE, _CACHE_AT = result, now
            return dict(result)

        root = Path("/sys/class/net") / interface
        wireless = (root / "wireless").exists()
        rx_bytes = _read_counter(interface, "rx_bytes")
        tx_bytes = _read_counter(interface, "tx_bytes")
        rx_dropped = _read_counter(interface, "rx_dropped")
        tx_dropped = _read_counter(interface, "tx_dropped")
        rx_errors = _read_counter(interface, "rx_errors")
        tx_errors = _read_counter(interface, "tx_errors")
        previous = _PREVIOUS.get(interface)
        elapsed = max(0.001, now - previous["at"]) if previous else None
        rx_mbps = tx_mbps = None
        if previous and elapsed:
            if rx_bytes is not None and previous["rx_bytes"] is not None:
                rx_mbps = round(max(0, rx_bytes - previous["rx_bytes"]) * 8 / elapsed / 1_000_000, 3)
            if tx_bytes is not None and previous["tx_bytes"] is not None:
                tx_mbps = round(max(0, tx_bytes - previous["tx_bytes"]) * 8 / elapsed / 1_000_000, 3)
        _PREVIOUS[interface] = {"at": now, "rx_bytes": rx_bytes, "tx_bytes": tx_bytes}

        wifi: dict[str, Any] = {"connected": None}
        if wireless:
            link = _command(["/usr/sbin/iw", "dev", interface, "link"])
            station = _command(["/usr/sbin/iw", "dev", interface, "station", "dump"])
            info = _command(["/usr/sbin/iw", "dev", interface, "info"])
            wifi = {
                "connected": "Connected to " in link,
                "signal_dbm": _number(r"signal:\s*(-?\d+)\s*dBm", link, int),
                "signal_avg_dbm": _number(r"signal avg:\s*(-?\d+)\s*dBm", station, int),
                "frequency_mhz": _number(r"freq:\s*(\d+)", link, int) or _number(r"channel\s+\d+\s+\((\d+)\s+MHz\)", info, int),
                "rx_bitrate_mbps": _number(r"rx bitrate:\s*([\d.]+)", link),
                "tx_bitrate_mbps": _number(r"tx bitrate:\s*([\d.]+)", link),
                "tx_retries": _number(r"tx retries:\s*(\d+)", station, int),
                "tx_failed": _number(r"tx failed:\s*(\d+)", station, int),
            }
            frequency = wifi["frequency_mhz"]
            wifi["channel"] = (
                int((frequency - 2407) / 5) if frequency and 2412 <= frequency <= 2472
                else (14 if frequency == 2484 else (int((frequency - 5000) / 5) if frequency and frequency >= 5000 else None))
            )
        result = {
            "available": True,
            "type": "wireless" if wireless else "wired_or_other",
            "interface_name": interface,
            "rx_mbps": rx_mbps,
            "tx_mbps": tx_mbps,
            "rx_dropped": rx_dropped,
            "tx_dropped": tx_dropped,
            "rx_errors": rx_errors,
            "tx_errors": tx_errors,
            "wifi": wifi,
            "sample_interval_seconds": round(elapsed, 2) if elapsed else None,
        }
        _CACHE, _CACHE_AT = result, now
        return dict(result)
