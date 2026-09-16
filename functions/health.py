"""Read-only health view of the existing scan output. Never runs providers."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def extensions_arg(value: str) -> list[str]:
    names = list(dict.fromkeys(v.strip() for v in value.split(",") if v.strip()))
    if any(not ID.fullmatch(name) for name in names):
        raise ValueError("Invalid extension name")
    return names


def read_object(path: Path) -> dict:
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"Invalid object: {path.name}")
    return data


def public_url(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Missing route URL")
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Invalid route URL")
    # Health checks never transport embedded credentials or query tokens.
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def snapshot(base: Path, selected: list[str] | None = None) -> dict:
    enabled = base / "extensions" / "enabled"
    names = selected if selected is not None else sorted(p.name for p in enabled.iterdir() if p.is_dir())
    if any(not ID.fullmatch(name) for name in names):
        raise ValueError("Invalid extension name")
    result = {"schema_version": 1, "scope": "index", "status": "NOT_TESTED",
              "generated_at": datetime.now(timezone.utc).isoformat(), "last_index_at": None,
              "errors": [], "extensions": []}
    try:
        result["last_index_at"] = (base / "last_scan.txt").read_text().strip()
        if not result["last_index_at"]:
            raise ValueError("Missing index timestamp")
        services = read_object(base / "services.json")["http_services"]
        if not isinstance(services, list):
            raise ValueError("Invalid services list")
        indexed = {str(int(s["port"])): s for s in services}
        state = read_object(base / "extensions" / "providers_state.json")
    except (OSError, ValueError, KeyError, TypeError):
        result["errors"].append("Index data missing or invalid")
        indexed, state = {}, {}

    for name in dict.fromkeys(names):
        entry = {"id": name, "status": "SKIP", "detail": "Extension not enabled", "services": []}
        result["extensions"].append(entry)
        directory = enabled / name
        if not directory.is_dir():
            continue
        try:
            manifest = read_object(directory / "extension.json")
            if manifest.get("enabled") is False:
                continue
            if result["errors"]:
                entry.update(status="NOT_TESTED", detail="Index data unavailable")
                continue
            routes = read_object(directory / "routes.json")
            provider = state.get("providers", {}).get(name, {})
            if provider.get("status") != "ok" or routes.get("errors"):
                raise ValueError("Provider failed in the index")
            if not routes.get("considered"):
                entry["detail"] = "Extension not configured in the index"
                continue
            if not routes.get("available"):
                raise ValueError("Configured extension unavailable")
            mappings = [(name, routes.get("services", {}))]
            for variant, payload in routes.get("variants", {}).items():
                if payload.get("considered"):
                    mappings.append((name + "-" + variant, payload.get("services", {})))
            seen = set()
            for variant, mapping in mappings:
                for port, route in mapping.items():
                    url = public_url(route.get("url"))
                    if (port, url) in seen:
                        continue
                    seen.add((port, url))
                    service = {"port": int(port), "variant": variant, "url": url, "status": "PASS", "detail": "Indexed route"}
                    entry["services"].append(service)
                    try:
                        if port not in indexed:
                            raise ValueError("Route missing from service index")
                        cache = read_object(base / "cache" / f"{int(port)}.json")
                        if cache.get("kind") not in ("html", "openai-v1", "http-service"):
                            raise ValueError("Unsupported or missing cache kind")
                        service.update(name=str(indexed[port].get("name") or f"Port {port}"), kind=cache["kind"])
                    except (OSError, ValueError, TypeError):
                        service.update(status="FAIL", detail="Route has no valid indexed service/cache")
            # Detect lost routes for services that the last scan assigned to this provider.
            routed = {str(s["port"]) for s in entry["services"]}
            for port, svc in indexed.items():
                if any(k == name or k.startswith(name + "-") for k in (svc.get("urls") or {})) and port not in routed:
                    entry["services"].append({"port": int(port), "status": "FAIL", "detail": "Indexed provider URL has no route"})
            if not entry["services"]:
                raise ValueError("Configured extension has no indexed routes")
            entry.update(status="PASS" if all(s["status"] == "PASS" for s in entry["services"]) else "FAIL",
                         detail="Index state; live reachability checked by external CLI")
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            entry.update(status="FAIL", detail="Provider state or routes missing, invalid or unavailable")
    statuses = [e["status"] for e in result["extensions"]]
    result["status"] = ("FAIL" if result["errors"] or "FAIL" in statuses else
                        "PASS" if "PASS" in statuses else "NOT_TESTED")
    return result
