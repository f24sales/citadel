"""Transport selection and safe Unix socket ownership for the WebUI."""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import os
from pathlib import Path
import socket
import stat


def _absolute_path(value: str, setting: str) -> Path:
    path = Path(value)
    if "\0" in value or not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{setting} must be an absolute path without '..'.")
    return path


def unix_socket_path(environment: Mapping[str, str] | None = None) -> Path | None:
    """Return None for TCP, or the validated Unix path, without touching disk.

    Read the environment after python_header has loaded the project's config.
    Only Unix mode consumes socket settings; TCP keeps its existing defaults.
    """
    environment = os.environ if environment is None else environment
    transport = environment.get("CITADEL_WEBUI_TRANSPORT", "").strip() or "tcp"
    if transport == "tcp":
        return None
    if transport != "unix":
        raise ValueError("CITADEL_WEBUI_TRANSPORT must be 'tcp' or 'unix'.")

    value = environment.get("CITADEL_WEBUI_SOCKET", "").strip()
    if not value or value.startswith("%t/"):
        runtime = _absolute_path(
            environment.get("XDG_RUNTIME_DIR", "").strip() or "/run",
            "XDG_RUNTIME_DIR",
        )
        value = str(runtime) + "/" + (value[3:] if value else "citadel/citadel.sock")
    path = _absolute_path(value, "CITADEL_WEBUI_SOCKET")
    if not path.name or value.endswith("/") or value.endswith("/."):
        raise ValueError("CITADEL_WEBUI_SOCKET must name a socket, not a directory.")
    return path


@contextmanager
def bind_unix_socket(path: Path) -> Iterator[socket.socket]:
    """Bind without replacing any existing path, including stale/live sockets.

    Create only the immediate socket directory, respecting the caller's umask
    and existing permissions. Missing ancestors must be provisioned separately.
    Passing the bound descriptor to Uvicorn avoids its Unix-path replacement
    behavior. Clean shutdown removes only the socket created by this invocation;
    after an unclean exit, an operator must inspect/remove the stale socket.
    """
    path.parent.mkdir(mode=0o755, exist_ok=True)
    owned = None
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(str(path))
            owned = path.lstat()
            yield listener
        finally:
            listener.close()
            if owned is not None:
                try:
                    current = path.lstat()
                except FileNotFoundError:
                    pass
                else:
                    if stat.S_ISSOCK(current.st_mode) and (
                        current.st_dev, current.st_ino
                    ) == (owned.st_dev, owned.st_ino):
                        path.unlink()
