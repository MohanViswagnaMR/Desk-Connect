"""Discovery helpers for the USB-C / Thunderbolt point-to-point link.

When two machines are joined by a Thunderbolt 3/4 or USB4 cable, the
``thunderbolt-net`` kernel module exposes a virtual Ethernet interface on each
side and NetworkManager assigns IPv4 *link-local* addresses (169.254.0.0/16).
USB "data-link" bridge cables behave the same way, presenting an
RNDIS/CDC-ECM interface.

In every one of those cases the cable simply looks like a private network
interface to user space, so Desk Connect runs ordinary TCP over it -- no Wi-Fi
or router involved.  This module enumerates the interfaces and flags the ones
that almost certainly belong to such a direct cable so the UI can pre-fill
sensible addresses.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import socket
from dataclasses import dataclass

# Interface name fragments that strongly suggest a direct USB-C/TB cable.
_CABLE_HINTS = ("thunderbolt", "tbt", "usb")


@dataclass
class LinkInterface:
    name: str
    address: str
    is_link_local: bool

    @property
    def looks_like_cable(self) -> bool:
        if self.is_link_local:
            return True
        lname = self.name.lower()
        return any(hint in lname for hint in _CABLE_HINTS)


# struct sockaddr_in / sockaddr for getifaddrs -----------------------------
class _SockaddrIn(ctypes.Structure):
    _fields_ = [
        ("sin_family", ctypes.c_ushort),
        ("sin_port", ctypes.c_ushort),
        ("sin_addr", ctypes.c_ubyte * 4),
        ("sin_zero", ctypes.c_ubyte * 8),
    ]


class _Ifaddrs(ctypes.Structure):
    pass


_Ifaddrs._fields_ = [
    ("ifa_next", ctypes.POINTER(_Ifaddrs)),
    ("ifa_name", ctypes.c_char_p),
    ("ifa_flags", ctypes.c_uint),
    ("ifa_addr", ctypes.POINTER(_SockaddrIn)),
    ("ifa_netmask", ctypes.POINTER(_SockaddrIn)),
    ("ifa_broadaddr", ctypes.POINTER(_SockaddrIn)),
    ("ifa_data", ctypes.c_void_p),
]


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)


def list_interfaces() -> list[LinkInterface]:
    """Return all up IPv4 interfaces (loopback excluded)."""
    libc = _libc()
    libc.getifaddrs.restype = ctypes.c_int
    libc.getifaddrs.argtypes = [ctypes.POINTER(ctypes.POINTER(_Ifaddrs))]

    head = ctypes.POINTER(_Ifaddrs)()
    if libc.getifaddrs(ctypes.byref(head)) != 0:
        return []

    results: list[LinkInterface] = []
    try:
        node = head
        while node:
            ifa = node.contents
            addr = ifa.ifa_addr
            if addr and addr.contents.sin_family == socket.AF_INET:
                name = ifa.ifa_name.decode("utf-8", "replace")
                octets = bytes(addr.contents.sin_addr)
                ip = ".".join(str(b) for b in octets)
                if name != "lo" and not ip.startswith("127."):
                    results.append(
                        LinkInterface(
                            name=name,
                            address=ip,
                            is_link_local=ip.startswith("169.254."),
                        )
                    )
            node = ifa.ifa_next
    finally:
        libc.freeifaddrs(head)
    return results


def cable_interfaces() -> list[LinkInterface]:
    """Interfaces that look like a direct USB-C / Thunderbolt cable, best first."""
    ifaces = list_interfaces()
    ifaces.sort(key=lambda i: (not i.is_link_local, not i.looks_like_cable, i.name))
    return [i for i in ifaces if i.looks_like_cable] or ifaces


def best_cable_interface() -> LinkInterface | None:
    """The single most likely USB-C/Thunderbolt cable interface, if any."""
    for iface in cable_interfaces():
        if iface.looks_like_cable:
            return iface
    return None


def best_cable_address() -> str | None:
    iface = best_cable_interface()
    return iface.address if iface else None


def hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return "desk-connect"
