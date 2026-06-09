#!/usr/bin/env bash
# Run Desk Connect straight from the source tree (no Flatpak), for development.
# Requires PyGObject + GTK4 + libadwaita. On Fedora / Nobara:
#   sudo dnf install python3-gobject gtk4 libadwaita
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONPATH="$PWD/src:${PYTHONPATH:-}"
exec python3 -m deskconnect "$@"
