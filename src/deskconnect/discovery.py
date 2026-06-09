"""Zero-config peer discovery over the USB-C / Thunderbolt link — cable only.

The whole point of Desk Connect is that traffic stays on the wire, never Wi-Fi.
So discovery is deliberately **cable-scoped**:

* The server broadcasts its beacon **out of the cable interface only** (bound to
  the cable's source address) and the beacon **carries the server's cable IP**.
* The client connects to the IP carried in the beacon — the ``169.254.x.x``
  cable address — so the TCP session is pinned to the wire even if the same
  broadcast happens to also arrive over Wi-Fi.

Beacon payload:  ``MAGIC (12) | tcp_port (u16 be) | cable_ip (ascii)``
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from typing import Callable, Optional

DISCOVERY_PORT = 24851
MAGIC = b"DESKCONNECT1"
_HEADER = struct.Struct(">H")


def _is_cable_ip(ip: str) -> bool:
    """True only if *ip* is reachable over a genuine cable, never Wi-Fi.

    Link-local (``169.254.x.x``) is the canonical Thunderbolt/USB4 case and is
    always a direct cable. A USB bridge cable may instead hand out a private
    subnet that is indistinguishable from Wi-Fi by address alone, so we accept
    such an address only when it sits on one of *this* machine's own cable
    interfaces (same subnet). That guarantees we never dial a Wi-Fi peer.
    """
    if ip.startswith("169.254."):
        return True
    return _on_local_cable_subnet(ip)


def _on_local_cable_subnet(ip: str) -> bool:
    from .link import cable_interfaces

    target = ip.split(".")
    if len(target) != 4:
        return False
    for iface in cable_interfaces():
        if not iface.looks_like_cable or iface.is_link_local:
            continue
        own = iface.address.split(".")
        if len(own) == 4 and own[:3] == target[:3]:  # same /24
            return True
    return False


def _cable_targets() -> list[tuple[str, str]]:
    """Return ``(source_ip, broadcast_ip)`` pairs for each cable interface."""
    from .link import cable_interfaces

    targets: list[tuple[str, str]] = []
    for iface in cable_interfaces():
        if not iface.looks_like_cable:
            continue
        parts = iface.address.split(".")
        if len(parts) != 4:
            continue
        if iface.is_link_local:
            bcast = "169.254.255.255"
        else:
            bcast = f"{parts[0]}.{parts[1]}.{parts[2]}.255"
        targets.append((iface.address, bcast))
    return targets


class Beacon:
    """Announces ``port + cable_ip`` on the cable network only."""

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
        while not self._stop:
            for source_ip, bcast in _cable_targets():
                self._send(source_ip, bcast)
            time.sleep(self.interval)

    def _send(self, source_ip: str, bcast: str) -> None:
        payload = MAGIC + _HEADER.pack(self.tcp_port) + source_ip.encode("ascii")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            # Bind to the cable's address so the datagram leaves that NIC and
            # advertises the cable IP — not the Wi-Fi one.
            sock.bind((source_ip, 0))
            sock.sendto(payload, (bcast, DISCOVERY_PORT))
        except OSError:
            pass
        finally:
            sock.close()


def discover_server(
    timeout: float = 10.0,
    should_stop: Callable[[], bool] = lambda: False,
) -> Optional[tuple[str, int]]:
    """Listen for a server beacon; return the advertised ``(cable_ip, port)``."""
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
                data, sender = sock.recvfrom(128)
            except socket.timeout:
                continue
            except OSError:
                break
            parsed = _parse(data, sender[0])
            if parsed is not None:
                return parsed
    finally:
        sock.close()
    return None


def _parse(data: bytes, sender_ip: str) -> Optional[tuple[str, int]]:
    head = len(MAGIC) + _HEADER.size
    if not data.startswith(MAGIC) or len(data) < head:
        return None
    (port,) = _HEADER.unpack(data[len(MAGIC):head])
    advertised = data[head:].decode("ascii", "ignore").strip()
    # Prefer the cable IP the server advertised; fall back to the sender if it
    # is itself a cable address. Never connect over a public/Wi-Fi address.
    if advertised and _is_cable_ip(advertised):
        return advertised, port
    if _is_cable_ip(sender_ip):
        return sender_ip, port
    return None
