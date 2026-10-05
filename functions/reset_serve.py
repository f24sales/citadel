"""Explicit Serve reset; never change Tailscale SSH, start services or scan."""
import fcntl
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent / "providers"))
from tailscale import command, read_live_serve


def reset() -> dict:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("TMPDIR") or "/tmp"
    path = Path(os.environ.get("CITADEL_SCAN_LOCK_FILE") or f"{runtime}/citadel-scan-{os.getuid()}.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Scan/Add is running; Serve reset aborted") from exc
        command(["tailscale", "serve", "reset"])
        live = read_live_serve()
        if any(live.get(key) for key in ("TCP", "Web", "AllowFunnel", "Foreground", "Services")):
            raise ValueError("Tailscale Serve reset was not confirmed")
    return {"ok": True, "action": "reset-serve"}
