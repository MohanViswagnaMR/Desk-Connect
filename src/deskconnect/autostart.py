"""Register Desk Connect to start at login via the XDG Background portal.

Inside the Flatpak sandbox we cannot write to ``~/.config/autostart`` directly,
so we ask ``org.freedesktop.portal.Background.RequestBackground`` to create the
autostart entry for us. Outside the sandbox we fall back to writing the
autostart desktop file ourselves.
"""

from __future__ import annotations

import os

from gi.repository import Gio, GLib

_APP_ID = "io.github.mohanviswagnamr.DeskConnect"


def _in_flatpak() -> bool:
    return os.path.exists("/.flatpak-info")


def _autostart_file() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "autostart", f"{_APP_ID}.desktop")


def set_autostart(enabled: bool) -> bool:
    """Enable or disable launch-at-login. Returns True on success."""
    if _in_flatpak():
        return _portal_request(enabled)
    return _write_desktop_file(enabled)


def _portal_request(enabled: bool) -> bool:
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        proxy = Gio.DBusProxy.new_sync(
            bus,
            Gio.DBusProxyFlags.NONE,
            None,
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.Background",
            None,
        )
        options = {
            "reason": GLib.Variant("s", "Start Desk Connect when you log in"),
            "autostart": GLib.Variant("b", enabled),
            "background": GLib.Variant("b", True),
            "commandline": GLib.Variant("as", ["deskconnect", "--autostart"]),
        }
        proxy.call_sync(
            "RequestBackground",
            GLib.Variant("(sa{sv})", ("", options)),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )
        return True
    except GLib.Error:
        return False


def _write_desktop_file(enabled: bool) -> bool:
    path = _autostart_file()
    try:
        if not enabled:
            if os.path.exists(path):
                os.remove(path)
            return True
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Name=Desk Connect\n"
                "Exec=deskconnect --autostart\n"
                "X-GNOME-Autostart-enabled=true\n"
            )
        return True
    except OSError:
        return False
