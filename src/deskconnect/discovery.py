"""Zero-config peer discovery over the USB-C / Thunderbolt link.

The server broadcasts a tiny UDP beacon on the cable's network. The client
listens for it and learns the server's address and TCP port automatically, so
the user never has to read or type a ``169.254.x.x`` address. Because the cable
is a private point-to-point link, the broadcast only reaches the other end.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from typing import Callable, Optional

DISCOVERY_PORT = 24851
MAGIC = b"DESKCONNECT1"


def _broadcast_addresses() -> list[str]:
    """Broadcast targets: the global broadcast plus each cable's /16 broadcast."""
    from .link import cable_interfaces

    addrs = ["255.255.255.255"]
    for iface in cable_interfaces():
        if iface.is_link_local:
            addrs.append("169.254.255.255")
        else:
            parts = iface.address.split(".")
            if len(parts) == 4:
                addrs.append(f"{parts[0]}.{parts[1]}.255.255")
    # de-duplicate, preserve order
    seen: set[str] = set()
    return [a for a in addrs if not (a in seen or seen.add(a))]


class Beacon:
    """Periodically announces ``MAGIC + tcp_port`` so clients can find us."""

    def __init__(self, tcp_port: int, interval: float = 1.0):
        self.tcp_port = tcp_port
        self.interval = interval
        self._stop = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="dc-beacon", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        if self._thread:
            self._thread.join(timeout=1.5)

    def _run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        payload = MAGIC + struct.pack(">H", self.tcp_port)
        try:
            while not self._stop:
                for addr in _broadcast_addresses():
                    try:
                        sock.sendto(payload, (addr, DISCOVERY_PORT))
                    except OSError:
                        pass
                time.sleep(self.interval)
        finally:
            sock.close()


def discover_server(
    timeout: float = 10.0,
    should_stop: Callable[[], bool] = lambda: False,
) -> Optional[tuple[str, int]]:
    """Listen for a server beacon. Returns ``(ip, tcp_port)`` or ``None``."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", DISCOVERY_PORT))
    except OSError:
        sock.close()
        return None
    sock.settimeout(0.5)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline and not should_stop():
            try:
                data, sender = sock.recvfrom(64)
            except socket.timeout:
                continue
            except OSError:
                break
            if data.startswith(MAGIC) and len(data) >= len(MAGIC) + 2:
                (port,) = struct.unpack(">H", data[len(MAGIC):len(MAGIC) + 2])
                return sender[0], port
    finally:
        sock.close()
    return None
