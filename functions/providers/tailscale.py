#!/usr/bin/env python3
"""Publish discovered local services through same-port Tailscale HTTPS Serve."""
from __future__ import annotations

import argparse
import copy
import importlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from common import now_iso, parse_bool, read_json, routable_services, route_record, write_json, ROUTE_SCHEMA_VERSION


def command(args: list[str], *, timeout: int = 15) -> str:
    try:
        result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"{' '.join(args[:3])}: {exc}") from exc
    if result.returncode:
        raise ValueError(result.stderr.strip() or result.stdout.strip() or "command failed")
    return result.stdout


def json_object(contents: str) -> dict[str, Any]:
    payload = json.loads(contents)
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object")
    return payload


def read_live_serve() -> dict[str, Any]:
    payload = json_object(command(["tailscale", "serve", "status", "--json"]))
    for field in ("TCP", "Web", "AllowFunnel", "Foreground", "Services"):
        if field in payload and not isinstance(payload[field], dict):
            raise ValueError(f"invalid Serve {field} configuration")
    return payload


def node_ports(config: dict[str, Any]) -> set[str]:
    ports = set(config.get("TCP", {}))
    for field in ("Web", "AllowFunnel"):
        ports.update(authority.rsplit(":", 1)[-1] for authority in config.get(field, {}))
    for foreground in config.get("Foreground", {}).values():
        ports.update(node_ports(foreground))
    return ports


def without_ports(config: dict[str, Any], ports: set[str]) -> dict[str, Any]:
    """Remove selected node ports, including paths, Funnel and foreground config."""
    result = copy.deepcopy(config)
    for port in ports:
        result.get("TCP", {}).pop(port, None)
    for field in ("Web", "AllowFunnel"):
        for authority in list(result.get(field, {})):
            if authority.rsplit(":", 1)[-1] in ports:
                result[field].pop(authority)
    for session, foreground in list(result.get("Foreground", {}).items()):
        remaining = without_ports(foreground, ports)
        if node_ports(remaining) or remaining.get("Services"):
            result["Foreground"][session] = remaining
        else:
            result["Foreground"].pop(session)
    return result


def remove_node_ports(config: dict[str, Any], ports: set[str]) -> dict[str, Any]:
    updated = without_ports(config, ports)
    if updated != config:
        command(["tailscale", "debug", "localapi", "POST", "serve-config", json.dumps(updated)])
    live = read_live_serve()
    if node_ports(live) & ports:
        raise ValueError("requested Serve ports remain configured after removal")
    return live


def serve_target(port: int, scheme: str) -> str:
    protocol = "https+insecure" if scheme == "https" else "http"
    return f"{protocol}://127.0.0.1:{port}"


def https_route_matches(config: dict[str, Any], domain: str, port: int, target: str) -> bool:
    key = str(port)
    if config.get("TCP", {}).get(key) != {"HTTPS": True}:
        return False
    web = {authority: value for authority, value in config.get("Web", {}).items()
           if authority.rsplit(":", 1)[-1] == key}
    if web != {f"{domain}:{port}": {"Handlers": {"/": {"Proxy": target}}}}:
        return False
    if any(allowed and authority.rsplit(":", 1)[-1] == key
           for authority, allowed in config.get("AllowFunnel", {}).items()):
        return False
    return not any(key in node_ports(foreground)
                   for foreground in config.get("Foreground", {}).values())


def running_status() -> dict[str, Any]:
    """One startup attempt, never login/up/auth-key flows, then bounded rechecks."""
    if not shutil.which("tailscale"):
        raise ValueError("tailscale CLI is not available")

    def status() -> dict[str, Any]:
        try:
            return json_object(command(["tailscale", "status", "--json"], timeout=3))
        except ValueError:
            return {}

    info = status()
    if info.get("BackendState") == "Running":
        return info
    if info.get("BackendState") == "Stopped":
        start = ["tailscale", "debug", "localapi", "PATCH", "prefs",
                 json.dumps({"WantRunning": True, "WantRunningSet": True})]
    else:
        start = ([] if os.geteuid() == 0 else ["sudo", "-n"]) + ["systemctl", "start", "tailscaled"]
    startup_error = ""
    try:
        command(start, timeout=10)
    except ValueError as exc:
        startup_error = f"; startup attempt: {exc}"
    for attempt in range(4):
        if attempt:
            time.sleep(0.5)
        info = status()
        if info.get("BackendState") == "Running":
            return info
    state = info.get("BackendState") or "unavailable"
    raise ValueError(f"Tailscale is {state} after one startup attempt{startup_error}; "
                     "start/authenticate it manually before retrying")


def citadel_value(provider_dir: str, key: str, default: str = "") -> str:
    root = Path(provider_dir).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return str(importlib.import_module("python_header").get(key, default)).strip()


def validate_services(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = payload.get("http_services")
    if not isinstance(rows, list):
        raise ValueError("services payload must contain an http_services list")
    ports: set[str] = set()
    for service in rows:
        if not isinstance(service, dict):
            raise ValueError("discovered service must be an object")
        port = str(service.get("port"))
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            raise ValueError(f"invalid discovered port {port}")
        key = str(int(port))
        if key in ports:
            raise ValueError(f"duplicate discovered port {key}")
        ports.add(key)
        if service.get("scheme") not in ("http", "https"):
            raise ValueError("discovered service must use http or https")
    return {str(int(service["port"])): service for service in routable_services(payload)}


def publish(args, ext, services_payload, services, routes, errors, enabled, running, domain, persistent):
    for service in services_payload.get("http_services", []):
        if not isinstance(service.get("urls"), dict):
            service["urls"] = {}
        service["urls"].pop("tailscale", None)
    if os.path.isdir(args.cache_dir):
        for name in os.listdir(args.cache_dir):
            if name.endswith(".json"):
                path = os.path.join(args.cache_dir, name)
                cached = read_json(path, {})
                if isinstance(cached, dict):
                    cached.pop("tailscale_url", None)
                    write_json(path, cached)
    for key, route in routes.items():
        services[key]["urls"]["tailscale"] = route["url"]
        path = os.path.join(args.cache_dir, f"{key}.json")
        cached = read_json(path, {})
        cached = cached if isinstance(cached, dict) else {}
        cached["tailscale_url"] = route["url"]
        write_json(path, cached)
    write_json(args.services_file, services_payload)
    payload = {
        "provider_id": "tailscale", "label": str(ext.get("label") or "Tailscale"),
        "considered": enabled, "enabled": enabled, "available": bool(routes),
        "generated_at": now_iso(), "default_candidate": True, "running": running,
        "domain": domain, "persistent": persistent, "route_schema": ROUTE_SCHEMA_VERSION,
        "services": routes, "errors": errors,
    }
    write_json(args.routes_out, payload)
    write_json(args.tailscale_file, payload)


def main() -> int:
    parser = argparse.ArgumentParser()
    for name in ("provider-dir", "services-file", "cache-dir", "config-ini", "routes-out", "tailscale-file"):
        parser.add_argument(f"--{name}", required=True)
    args = parser.parse_args()
    try:
        provider_dir = Path(args.provider_dir).absolute()
        ext = json_object((provider_dir / "extension.json").read_text(encoding="utf-8"))
        enabled = provider_dir.parent.name == "enabled" and parse_bool(ext.get("enabled", True))
        raw_persistent = citadel_value(args.provider_dir, "CITADEL_PERSISTENT", "0").strip().lower() or "0"
        if raw_persistent not in ("0", "1", "false", "true", "no", "yes", "off", "on"):
            raise ValueError("CITADEL_PERSISTENT must be a boolean")
        persistent = parse_bool(raw_persistent)
        services_payload = json_object(Path(args.services_file).read_text(encoding="utf-8"))
        services = validate_services(services_payload)
    except (OSError, ValueError, TypeError) as exc:
        print(exc, file=sys.stderr)
        return 1

    errors: list[str] = []
    routes: dict[str, dict[str, Any]] = {}
    running, domain = False, None
    if enabled:
        try:
            info = running_status()
            running = True
            cert_domains = info.get("CertDomains") or []
            domain = (info.get("Self", {}).get("DNSName") or next(iter(cert_domains), "")).rstrip(".")
            if not domain or domain not in cert_domains:
                raise ValueError("Tailscale has no certificate domain; enable HTTPS certificates manually")
            if not persistent:
                command(["tailscale", "serve", "reset"])
            live = read_live_serve()
            if not persistent and any(live.get(field) for field in ("TCP", "Web", "AllowFunnel", "Foreground", "Services")):
                raise ValueError("Serve reset did not clear the configuration; rebuild aborted")
            for key, service in sorted(services.items(), key=lambda item: int(item[0])):
                port = int(key)
                target = serve_target(port, service["scheme"])
                try:
                    if not https_route_matches(live, domain, port, target):
                        if key in node_ports(live):
                            live = remove_node_ports(live, {key})
                        # Peer Serve is intercepted before the host kernel.
                        # On TUN nodes, its additional local listener can block
                        # a wildcard backend on restart; see deployment notes.
                        command(["tailscale", "serve", "--bg", "--yes", f"--https={port}", target])
                        live = read_live_serve()
                    if not https_route_matches(live, domain, port, target):
                        raise ValueError("HTTPS Serve configuration was not confirmed")
                    routes[key] = route_record("proxy", f"https://{domain}:{port}",
                                               target=target, owns_listener=True)
                except (ValueError, TypeError, AttributeError) as exc:
                    errors.append(f"port {port}: {exc}")
        except (ValueError, TypeError, AttributeError) as exc:
            errors.append(str(exc))
    publish(args, ext, services_payload, services, routes, errors, enabled, running, domain, persistent)
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
