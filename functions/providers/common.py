from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from typing import Any

from atomic_io import atomic_write_json


ROUTE_SCHEMA_VERSION = 1


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def read_json(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path: str, payload: Any) -> None:
    atomic_write_json(path, payload)


def adding() -> bool:
    """Internal invocation mode, never a persistent configuration setting."""
    return os.environ.get("CITADEL_SCAN_ADD") == "1"


def write_routes(path: str, payload: dict) -> None:
    if adding():
        previous = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                previous = json.load(handle)
            if not isinstance(previous, dict) or not isinstance(previous.get("services"), dict):
                raise ValueError("Invalid existing routes; Add aborted")
        payload["services"] = {**payload.get("services", {}), **previous.get("services", {})}
        payload["available"] = bool(payload["services"])
    write_json(path, payload)


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def routable_services(payload: Any, key: str = "http_services") -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get(key, [])
    if not isinstance(rows, list):
        return []
    services = [row for row in rows if isinstance(row, dict)]
    if adding():
        ports = set(payload.get("added_ports", []))
        services = [row for row in services if row.get("port") in ports]
    if not parse_bool(payload.get("https_only")):
        return services
    return [
        row
        for row in services
        if str(row.get("scheme") or "").strip().lower() == "https"
    ]


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def route_record(
    mode: str,
    url: str,
    *,
    target: str | None = None,
    owns_listener: bool = False,
) -> dict[str, Any]:
    return {
        "mode": mode,
        "url": url,
        "target": target,
        "owns_listener": owns_listener,
    }
