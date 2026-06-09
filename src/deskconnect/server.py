"""Server engine: capture local keyboard/mouse and forward to the peer.

The server runs on the computer whose physical keyboard and mouse you want to
share.  It listens on the cable's TCP port for the client to connect, then:

* watches the selected input devices for the *switch hotkey* (default
  Ctrl+Alt+S);
* while in REMOTE state it ``EVIOCGRAB``s the devices so the local session no
  longer reacts to them, and streams every event to the client;
* pressing the hotkey again releases the grab and returns control locally.

Everything runs on a single background thread driven by ``select`` so we never
block the GTK main loop.
"""

from __future__ import annotations

import os
import select
import socket
import threading
from typing import Callable, Optional

from . import protocol
from .config import Settings
from .linux_input import (
    EV_KEY,
    EV_SYN,
    EvdevDevice,
    InputEvent,
    list_devices,
)

StatusCb = Callable[[str], None]
StateCb = Callable[[bool], None]


class HotkeyMatcher:
    """Edge-detects a chord of EV_KEY codes regardless of press order."""

    def __init__(self, combo: list[int]):
        self.combo = set(combo)
        self._down: set[int] = set()
        self._fired = False

    def feed(self, event: InputEvent) -> bool:
        if event.type != EV_KEY or not self.combo:
            return False
        if event.value == 1:  # key down
            self._down.add(event.code)
        elif event.value == 0:  # key up
            self._down.discard(event.code)
            self._fired = False
            return False
        if self.combo.issubset(self._down) and not self._fired:
            self._fired = True
            return True
        return False

    def is_combo_key(self, code: int) -> bool:
        return code in self.combo


class ServerEngine:
    def __init__(
        self,
        settings: Settings,
        on_status: StatusCb,
        on_active: StateCb,
        on_client: Callable[[str], None],
    ):
        self.settings = settings
        self._on_status = on_status
        self._on_active = on_active
        self._on_client = on_client

        self._thread: Optional[threading.Thread] = None
        self._wake_r, self._wake_w = os.pipe()
        self._stop = False

        self._devices: list[EvdevDevice] = []
        self._conn: Optional[socket.socket] = None
        self._remote_active = False
        self._hotkey = HotkeyMatcher(settings.switch_hotkey)

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="dc-server", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        try:
            os.write(self._wake_w, b"x")
        except OSError:
            pass
        if self._thread:
            self._thread.join(timeout=2.0)

    # -- internals ---------------------------------------------------------
    def _open_devices(self) -> None:
        infos = [d for d in list_devices() if d.kind in ("keyboard", "mouse", "pointer")]
        for info in infos:
            try:
                self._devices.append(EvdevDevice(info.path))
            except OSError as exc:
                self._on_status(f"Could not open {info.path}: {exc}")
        if not self._devices:
            self._on_status(
                "No input devices could be opened. Check that the udev rule is "
                "installed and you are in the 'input' group."
            )

    def _close_devices(self) -> None:
        for dev in self._devices:
            dev.close()
        self._devices = []

    def _set_remote(self, active: bool) -> None:
        if active == self._remote_active:
            return
        self._remote_active = active
        for dev in self._devices:
            if active:
                try:
                    dev.grab()
                except OSError as exc:
                    self._on_status(f"Grab failed on {dev.path}: {exc}")
            else:
                dev.ungrab()
        self._on_active(active)
        self._on_status(
            "Controlling REMOTE computer — press the switch hotkey to return"
            if active
            else "Control is LOCAL — press the switch hotkey to drive the remote"
        )

    def _accept(self, listener: socket.socket) -> None:
        conn, addr = listener.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if self._conn is not None:
            conn.close()
            return
        self._conn = conn
        self._on_client(addr[0])
        self._on_status(f"Client connected from {addr[0]}")
        try:
            from .link import hostname
            conn.sendall(protocol.encode_hello("server", hostname()))
        except OSError:
            self._drop_client()

    def _drop_client(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None
        self._set_remote(False)
        self._on_client("")
        self._on_status("Waiting for a client to connect…")

    def _forward(self, event: InputEvent) -> None:
        if self._conn is None:
            return
        try:
            self._conn.sendall(protocol.encode_event(event))
        except OSError:
            self._drop_client()

    def _handle_events(self, dev: EvdevDevice) -> None:
        for event in dev.read():
            # The hotkey is evaluated on every event so it works in both states.
            if self._hotkey.feed(event):
                self._set_remote(not self._remote_active)
                continue
            if not self._remote_active:
                continue
            # Don't leak the modifier keys of the switch chord to the remote.
            if event.type == EV_KEY and self._hotkey.is_combo_key(event.code):
                continue
            self._forward(event)

    def _run(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((self.settings.bind_address, self.settings.listen_port))
            listener.listen(1)
        except OSError as exc:
            self._on_status(f"Could not listen on port {self.settings.listen_port}: {exc}")
            listener.close()
            return

        self._open_devices()
        self._on_status("Waiting for a client to connect…")

        try:
            while not self._stop:
                listen_fd = listener.fileno()
                conn_fd = self._conn.fileno() if self._conn is not None else -1
                dev_by_fd = {d.fileno(): d for d in self._devices}

                rlist = [self._wake_r, listen_fd] + list(dev_by_fd)
                if conn_fd >= 0:
                    rlist.append(conn_fd)
                ready, _, _ = select.select(rlist, [], [], 1.0)
                ready_set = set(ready)

                if self._wake_r in ready_set:
                    os.read(self._wake_r, 64)
                if listen_fd in ready_set:
                    self._accept(listener)
                if conn_fd >= 0 and conn_fd in ready_set:
                    self._drain_client()
                for fd, dev in dev_by_fd.items():
                    if fd in ready_set:
                        try:
                            self._handle_events(dev)
                        except OSError:
                            self._on_status(f"Lost device {dev.path}")
                            dev.close()
                            if dev in self._devices:
                                self._devices.remove(dev)
        finally:
            self._set_remote(False)
            self._drop_client()
            self._close_devices()
            listener.close()
            self._on_status("Stopped")

    def _drain_client(self) -> None:
        assert self._conn is not None
        try:
            data = self._conn.recv(4096)
        except OSError:
            data = b""
        if not data:
            self._drop_client()
        # The client never needs to send us input; any traffic is keepalive.
