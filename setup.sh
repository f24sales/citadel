#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

usage() {
    cat <<'EOF'
Usage: ./setup.sh [--show]
       ./setup.sh --render-containerfile

Default: configure CITADEL and render its user service (existing CONTAINER
directory checkouts retain their container configuration workflow).

--render-containerfile copies Containerfile.example to Containerfile only.
It does not configure, build, push, or start anything. Run from a source
checkout; the future build context is this directory. A matching Containerfile
is retained; move a custom or outdated Containerfile aside before regenerating.
Compose/Quadlet generation remains the separate config.sh --render-container
workflow; CITADEL uses the image's python3 -s webui.py startup command.
EOF
}

for argument in "$@"; do
    case "$argument" in
        --help|-h) usage; exit 0 ;;
        --render-containerfile)
            if [ "$#" -ne 1 ]; then
                echo "Use --render-containerfile on its own." >&2
                exit 2
            fi
            template="$SCRIPT_DIR/Containerfile.example"
            target="$SCRIPT_DIR/Containerfile"
            if [ ! -f "$template" ]; then
                echo "Missing Containerfile.example." >&2
                exit 1
            fi
            if [ -L "$target" ] || [ -e "$target" ]; then
                if [ ! -L "$target" ] && [ -f "$target" ] && cmp -s "$template" "$target"; then
                    echo "  Unchanged: $target"
                    exit 0
                fi
                echo "Keeping existing Containerfile; move it aside before rendering." >&2
                exit 1
            fi
            # Refuse a concurrently created destination, including symlinks.
            (set -o noclobber; cat "$template" > "$target")
            echo "  Rendered: $target (no image build or service changes)"
            exit 0
            ;;
    esac
done

if [ "$(basename "$(cd "$SCRIPT_DIR/.." && pwd -P)")" = "CONTAINER" ]; then
    "$SCRIPT_DIR/config.sh" "$@"
    exit 0
fi

"$SCRIPT_DIR/config.sh" --no-container "$@"
"$SCRIPT_DIR/set_daemon.sh" --render-only

printf '\nLink the rendered systemd user service:\n'
printf '  ln -sfn %q %q\n' \
    "$SCRIPT_DIR/citadel.service" \
    "$USER_UNIT_DIR/citadel.service"
