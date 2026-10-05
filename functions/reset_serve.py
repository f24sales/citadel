"""Explicit Serve operations; never change SSH, Citadel data or start a scan."""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent / "providers"))
from tailscale import command, node_ports, read_live_serve, remove_node_ports


@contextmanager
def serve_lock():
    runtime = os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("TMPDIR") or "/tmp"
    path = Path(os.environ.get("CITADEL_SCAN_LOCK_FILE") or f"{runtime}/citadel-scan-{os.getuid()}.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Scan/Add is running; Serve operation aborted") from exc
        yield


def status() -> dict:
    return {"ports": sorted(int(port) for port in node_ports(read_live_serve()))}


def delete(port) -> dict:
    raw = str(port)
    if not raw.isascii() or not raw.isdigit() or not 1 <= int(raw) <= 65535:
        raise ValueError("Expected one port between 1 and 65535")
    port = int(raw)
    with serve_lock():
        remove_node_ports(read_live_serve(), {str(port)})
    return {"ok": True, "action": "delete-serve", "port": port}


def reset() -> dict:
    with serve_lock():
        command(["tailscale", "serve", "reset"])
        live = read_live_serve()
        if any(live.get(key) for key in ("TCP", "Web", "AllowFunnel", "Foreground", "Services")):
            raise ValueError("Tailscale Serve reset was not confirmed")
    return {"ok": True, "action": "reset-serve"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--del", dest="port", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(delete(args.port)))
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
