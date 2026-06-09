"""Persisted settings (stored under the XDG config dir, sandbox-friendly)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field

from .protocol import DEFAULT_PORT

# EV_KEY codes for the default switch hotkey: Ctrl + Alt + S.
KEY_LEFTCTRL = 29
KEY_LEFTALT = 56
KEY_S = 31
DEFAULT_HOTKEY = [KEY_LEFTCTRL, KEY_LEFTALT, KEY_S]


def _config_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "deskconnect", "settings.json")


@dataclass
class Settings:
    role: str = "server"            # "server" or "client"
    listen_port: int = DEFAULT_PORT
    peer_address: str = ""          # client uses this to reach the server
    bind_address: str = "0.0.0.0"   # server listen address
    switch_hotkey: list[int] = field(default_factory=lambda: list(DEFAULT_HOTKEY))
    auto_grab_all: bool = True      # grab every keyboard + pointer when active
    auto_connect: bool = True       # auto-start when the USB-C cable appears
    start_at_login: bool = False    # register an autostart entry via the portal

    @classmethod
    def load(cls) -> "Settings":
        try:
            with open(_config_path(), "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return cls()
        known = {f for f in cls().__dict__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        path = _config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, indent=2)
        os.replace(tmp, path)
