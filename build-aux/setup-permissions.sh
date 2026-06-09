#!/usr/bin/env bash
# Grant the current user access to the input subsystem so Desk Connect can run
# without root. Safe to re-run. Works on Fedora, Nobara, and other distros.
set -euo pipefail

RULE_SRC="$(dirname "$0")/../data/60-deskconnect-uinput.rules"
RULE_DST="/etc/udev/rules.d/60-deskconnect-uinput.rules"

if [ "$(id -u)" -ne 0 ]; then
    echo "This script needs root to install the udev rule. Re-running with sudo…"
    exec sudo -E bash "$0" "${SUDO_USER:-$USER}"
fi

TARGET_USER="${1:-${SUDO_USER:-root}}"

echo "==> Loading the uinput kernel module"
modprobe uinput || true
echo "uinput" >/etc/modules-load.d/deskconnect-uinput.conf

echo "==> Installing udev rule -> $RULE_DST"
install -Dm644 "$RULE_SRC" "$RULE_DST"
udevadm control --reload-rules
udevadm trigger

echo "==> Adding '$TARGET_USER' to the 'input' group"
gpasswd -a "$TARGET_USER" input

cat <<EOF

Done. Log out and back in (or reboot) so the new group membership takes effect.
Verify with:   id -nG | tr ' ' '\n' | grep -x input
EOF
