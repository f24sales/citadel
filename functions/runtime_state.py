"""One state directory for the host service and the container bind mount."""

import argparse
import os
from pathlib import Path


def prepare_state(root: Path, state_name: str = "CITADEL") -> None:
    state = root / state_name
    files = {name: name for name in (
        "ports.filter.json", "services.json", "ss.json", "tailscale.json", "last_scan.txt",
    )}
    files["extensions/providers_state.json"] = "providers_state.json"
    files.update({"icons": "icons", "CADDY": "CADDY",
                  "extensions/enabled/caddy/config.json": "caddy-config.json"})
    for directory in (root / "extensions").glob("*/*"):
        if directory.name == "cloudflare":
            continue  # Disposable scan output only; remote state is queried afresh.
        if directory.is_dir() and (directory / "extension.json").is_file():
            for name in ("routes.json", "status.json"):
                files[str((directory / name).relative_to(root))] = f"{directory.name}-{name}"

    pairs = [(root / source, state / target) for source, target in files.items()]
    # Refuse conflicting copies before moving anything; never discard state.
    for source, target in pairs:
        if source.is_symlink():
            if source.resolve() != target.resolve():
                raise ValueError(f"Unexpected state link: {source}")
        elif source.exists() and target.exists():
            raise ValueError(f"Conflicting state: {source} and {target}")

    state.mkdir(exist_ok=True)
    for source, target in pairs:
        if source.is_symlink():
            continue
        source.parent.mkdir(parents=True, exist_ok=True)
        if source.exists():
            source.rename(target)
        source.symlink_to(os.path.relpath(target, source.parent))
    for directory in ("icons", "CADDY"):
        (state / directory).mkdir(exist_ok=True)


def prepare_image(root: Path) -> None:
    prepare_state(root, "CITADEL_DATA")
    (root / "CITADEL_TAILSCALE").mkdir(mode=0o700, exist_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", action="store_true")
    prepare = prepare_image if parser.parse_args().image else prepare_state
    prepare(Path(__file__).resolve().parents[1])
