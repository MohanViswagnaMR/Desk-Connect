"""Client engine: connect to the server and inject received events.

The client runs on the computer you want to *control*.  It dials the server
across the cable, then replays every input event it receives into a virtual
keyboard+pointer created with ``/dev/uinput``.  Because uinput injects below
the display server, this works identically on X11 and Wayland (GNOME/KDE on
Fedora and Nobara).
"""

from __future__ import annotations

import os
import select
import socket
import threading
import time
from typing import Callable, Optional

from . import protocol
from .config import Settings
from .linux_input import EV_SYN, SYN_REPORT, InputEvent, UInput, UInputError

StatusCb = Callable[[str], None]
StateCb = Callable[[bool], None]


class ClientEngine:
    def __init__(self, settings: Settings, on_status: StatusCb, on_connected: StateCb):
        self.settings = settings
        self._on_status = on_status
        self._on_connected = on_connected

        self._thread: Optional[threading.Thread] = None
        self._wake_r, self._wake_w = os.pipe()
        self._stop = False
        self._uinput: Optional[UInput] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="dc-client", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        try:
            os.write(self._wake_w, b"x")
        except OSError:
            pass
        if self._thread:
            self._thread.join(timeout=2.0)

    # ----------------------------------------------------------------------
    def _run(self) -> None:
        try:
            self._uinput = UInput()
        except UInputError as exc:
            self._on_status(str(exc))
            return

        try:
            while not self._stop:
                connected = self._connect_and_serve()
                if self._stop:
                    break
                # Reconnect quickly after a dropped session, back off after a
                # failed dial so we don't spin on an unreachable server.
                self._on_status("Disconnected — retrying…")
                self._interruptible_sleep(2.0 if connected else 3.0)
        finally:
            if self._uinput is not None:
                self._uinput.close()
            self._on_status("Stopped")

    def _connect_and_serve(self) -> bool:
        addr = self.settings.peer_address.strip()
        if not addr:
            self._on_status("Set the server address first.")
            self._interruptible_sleep(2.0)
            return False

        self._on_status(f"Connecting to {addr}:{self.settings.listen_port}…")
        try:
            sock = socket.create_connection(
                (addr, self.settings.listen_port), timeout=5.0
            )
        except OSError as exc:
            self._on_status(f"Connection failed: {exc}")
            return False

        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(None)
        from .link import hostname
        try:
            sock.sendall(protocol.encode_hello("client", hostname()))
        except OSError:
            sock.close()
            return False

        self._on_connected(True)
        self._on_status(f"Connected to {addr} — ready to receive input")
        decoder = protocol.Decoder()
        try:
            while not self._stop:
                ready, _, _ = select.select([sock.fileno(), self._wake_r], [], [], 1.0)
                if self._wake_r in ready:
                    os.read(self._wake_r, 64)
                    break
                if sock.fileno() not in ready:
                    continue
                data = sock.recv(8192)
                if not data:
                    break
                for msg in decoder.feed(data):
                    self._dispatch(msg)
        except OSError as exc:
            self._on_status(f"Link error: {exc}")
        finally:
            sock.close()
            self._on_connected(False)
        return True  # a session was established; caller decides on reconnect

    def _dispatch(self, msg: protocol.Message) -> None:
        if msg.kind == protocol.KIND_EVENT:
            event = protocol.decode_event(msg.body)
            self._inject(event)
        elif msg.kind == protocol.KIND_PING:
            pass  # keepalive only

    def _inject(self, event: InputEvent) -> None:
        if self._uinput is None:
            return
        self._uinput.write_event(event)
        # The server forwards real SYN_REPORTs, but emit one defensively so a
        # dropped boundary never stalls the virtual device.
        if event.type == EV_SYN and event.code == SYN_REPORT:
            return

    def _interruptible_sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self._stop:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            ready, _, _ = select.select([self._wake_r], [], [], remaining)
            if self._wake_r in ready:
                os.read(self._wake_r, 64)
                return
