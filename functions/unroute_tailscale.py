#!/usr/bin/env python3
"""Remove requested node Serve ports; no ownership ledger is required."""

from __future__ import annotations

import argparse
import copy
import fcntl
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent / "providers"))
from providers.atomic_io import atomic_write_json
from providers.tailscale import (
    node_ports,
    read_live_serve,
    remove_node_ports,
    serve_management_enabled,
)

STATE_PATHS = ("tailscale.json", "extensions/enabled/tailscale/routes.json")


class UnrouteError(RuntimeError):
    pass


def read_json_object(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UnrouteError(f"cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise UnrouteError(f"expected a JSON object in {path}")
    return payload


def read_configured_port(project_dir: Path) -> int:
    path = project_dir / "config.conf"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == "CITADEL_WEBUI_PORT":
                port = int(value.split("#", 1)[0].strip().strip("\"'"))
                if 1 <= port <= 65535:
                    return port
    except (OSError, ValueError) as exc:
        raise UnrouteError(f"cannot read CITADEL_WEBUI_PORT: {exc}") from exc
    raise UnrouteError(f"valid CITADEL_WEBUI_PORT missing from {path}")


def _clear_metadata(project_dir: Path, released: set[str]) -> None:
    path = project_dir / "services.json"
    payload = read_json_object(path)
    if payload is not None:
        for service in payload.get("http_services", []):
            if not isinstance(service, dict) or str(service.get("port")) not in released:
                continue
            urls = service.get("urls")
            if isinstance(urls, dict):
                urls.pop("tailscale", None)
        atomic_write_json(path, payload)
    for port in released:
        path = project_dir / "cache" / f"{port}.json"
        cached = read_json_object(path)
        if cached is not None:
            cached.pop("tailscale_url", None)
            atomic_write_json(path, cached)


def _unroute_locked(project_dir: Path, requested_ports: list[int] | None) -> int:
    ports = requested_ports or [read_configured_port(project_dir)]
    if any(not 1 <= port <= 65535 for port in ports):
        raise UnrouteError("ports must be between 1 and 65535")
    keys = list(dict.fromkeys(str(port) for port in ports))
    states = {}
    for name in STATE_PATHS:
        path = project_dir / name
        payload = read_json_object(path)
        if payload is not None:
            states[path] = payload
    if not shutil.which("tailscale"):
        raise UnrouteError("tailscale CLI is unavailable")
    try:
        live = read_live_serve()
    except (ValueError, TypeError, AttributeError) as exc:
        raise UnrouteError(f"cannot read Serve status: {exc}") from exc

    released: set[str] = set()
    errors = []
    for key in keys:
        try:
            if key in node_ports(live):
                live = remove_node_ports(live, {key})
        except (ValueError, TypeError, AttributeError) as exc:
            errors.append(f"port {key}: {exc}")
            continue
        released.add(key)

    for path, payload in states.items():
        updated = copy.deepcopy(payload)
        entries = updated.get("services")
        if isinstance(entries, dict):
            for key in released:
                entries.pop(key, None)
        updated["available"] = bool(updated.get("services"))
        if updated != payload:
            atomic_write_json(path, updated)
    if released:
        _clear_metadata(project_dir, released)
    print("[unroute] released ports: " + (", ".join(sorted(released, key=int)) or "none"))
    print("[unroute] other ports, discovered services and logos were preserved")
    if errors:
        raise UnrouteError("; ".join(errors))
    return 0


def unroute(project_dir: Path, requested_ports: list[int] | None = None) -> int:
    try:
        enabled = serve_management_enabled(str(project_dir / "extensions/enabled/tailscale"))
    except ValueError as exc:
        raise UnrouteError(str(exc)) from exc
    if not enabled:
        print("[unroute] CITADEL_TAILSCALE_SERVE=0; Serve and metadata left unchanged")
        return 0
    runtime = os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("TMPDIR") or "/tmp"
    path = Path(os.environ.get("CITADEL_SCAN_LOCK_FILE") or f"{runtime}/citadel-scan-{os.getuid()}.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UnrouteError("CITADEL scan is running; retry after it finishes") from exc
        return _unroute_locked(project_dir.resolve(), requested_ports)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("ports", nargs="*", type=int, metavar="PORT")
    args = parser.parse_args()
    try:
        return unroute(args.root, args.ports)
    except (UnrouteError, OSError) as exc:
        print(f"[unroute] failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
