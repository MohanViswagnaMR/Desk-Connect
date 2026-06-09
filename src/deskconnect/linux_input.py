"""Low-level Linux input plumbing implemented in pure Python.

This module deliberately avoids any compiled third-party dependency (such as
``python-evdev``) so that the Flatpak can be built against nothing but the
GNOME runtime's PyGObject.  Everything here talks to the kernel directly
through ``ioctl`` calls and the ``struct input_event`` ABI.

Two responsibilities live here:

* :class:`EvdevDevice` -- read events from ``/dev/input/event*`` and,
  optionally, *grab* the device with ``EVIOCGRAB`` so the local session no
  longer sees the events while we forward them to the peer.
* :class:`UInput` -- create a virtual keyboard + relative pointer through
  ``/dev/uinput`` and replay events that arrive from the peer.

Linux input event codes (``KEY_*``, ``REL_*``, ``BTN_*`` ...) are stable
kernel ABI and identical across machines, so events captured on one computer
can be replayed verbatim on the other with no translation table.
"""

from __future__ import annotations

import ctypes
import fcntl
import glob
import os
import struct
from dataclasses import dataclass, field
from typing import Iterator

# ---------------------------------------------------------------------------
# struct input_event
#
# On 64-bit Linux this is: struct timeval (two longs) + __u16 type +
# __u16 code + __s32 value.  Using native sizes/alignment gives the correct
# 24-byte layout.
# ---------------------------------------------------------------------------
_EVENT = struct.Struct("llHHi")
EVENT_SIZE = _EVENT.size


@dataclass(frozen=True)
class InputEvent:
    """A single kernel input event (timestamp dropped -- we re-stamp on replay)."""

    type: int
    code: int
    value: int


# ---------------------------------------------------------------------------
# Event type / code constants (subset we care about, from <linux/input-event-codes.h>)
# ---------------------------------------------------------------------------
EV_SYN = 0x00
EV_KEY = 0x01
EV_REL = 0x02
EV_ABS = 0x03
EV_MSC = 0x04

SYN_REPORT = 0x00

REL_X = 0x00
REL_Y = 0x01
REL_HWHEEL = 0x06
REL_WHEEL = 0x08
REL_WHEEL_HI_RES = 0x0B
REL_HWHEEL_HI_RES = 0x0C

MSC_SCAN = 0x04

BTN_LEFT = 0x110
BTN_RIGHT = 0x111
BTN_MIDDLE = 0x112

KEY_MAX = 0x2FF
REL_MAX = 0x0F


# ---------------------------------------------------------------------------
# ioctl request-number helpers (mirrors the kernel's <asm-generic/ioctl.h>)
# ---------------------------------------------------------------------------
_IOC_NONE = 0
_IOC_WRITE = 1
_IOC_READ = 2

_IOC_NRBITS = 8
_IOC_TYPEBITS = 8
_IOC_SIZEBITS = 14

_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS


def _ioc(direction: int, type_: str, nr: int, size: int) -> int:
    return (
        (direction << _IOC_DIRSHIFT)
        | (ord(type_) << _IOC_TYPESHIFT)
        | (nr << _IOC_NRSHIFT)
        | (size << _IOC_SIZESHIFT)
    )


def _io(type_: str, nr: int) -> int:
    return _ioc(_IOC_NONE, type_, nr, 0)


def _iow(type_: str, nr: int, size: int) -> int:
    return _ioc(_IOC_WRITE, type_, nr, size)


def _ior(type_: str, nr: int, size: int) -> int:
    return _ioc(_IOC_READ, type_, nr, size)


# evdev ioctls (type 'E')
EVIOCGRAB = _iow("E", 0x90, 4)


def _eviocgname(length: int) -> int:
    return _ior("E", 0x06, length)


def _eviocgbit(ev: int, length: int) -> int:
    return _ioc(_IOC_READ, "E", 0x20 + ev, length)


# uinput ioctls (type 'U')
UI_DEV_CREATE = _io("U", 1)
UI_DEV_DESTROY = _io("U", 2)
UI_SET_EVBIT = _iow("U", 100, 4)
UI_SET_KEYBIT = _iow("U", 101, 4)
UI_SET_RELBIT = _iow("U", 102, 4)
UI_SET_ABSBIT = _iow("U", 103, 4)
UI_SET_MSCBIT = _iow("U", 104, 4)

UINPUT_MAX_NAME_SIZE = 80
UINPUT_PATHS = ("/dev/uinput", "/dev/input/uinput")


# ---------------------------------------------------------------------------
# Reading + classifying real input devices
# ---------------------------------------------------------------------------
@dataclass
class DeviceInfo:
    path: str
    name: str
    has_keys: bool = False
    has_rel: bool = False
    has_abs: bool = False

    @property
    def is_keyboard(self) -> bool:
        # A device that reports a healthy span of keyboard keys.
        return self.has_keys and not self.has_rel

    @property
    def is_pointer(self) -> bool:
        return self.has_rel or self.has_abs

    @property
    def kind(self) -> str:
        if self.is_pointer and self.has_keys:
            return "mouse"
        if self.is_pointer:
            return "pointer"
        if self.has_keys:
            return "keyboard"
        return "other"


def _device_name(fd: int) -> str:
    buf = bytearray(256)
    try:
        fcntl.ioctl(fd, _eviocgname(len(buf)), buf)
    except OSError:
        return "unknown"
    return buf.split(b"\x00", 1)[0].decode("utf-8", "replace")


def _event_types(fd: int) -> set[int]:
    """Return the set of EV_* types the device supports."""
    nbytes = (max(EV_MSC, EV_ABS) // 8) + 1
    buf = bytearray(nbytes)
    try:
        fcntl.ioctl(fd, _eviocgbit(0, len(buf)), buf)
    except OSError:
        return set()
    types = set()
    for bit in (EV_KEY, EV_REL, EV_ABS, EV_MSC):
        if buf[bit // 8] & (1 << (bit % 8)):
            types.add(bit)
    return types


def _has_real_keys(fd: int) -> bool:
    """True if the device exposes ordinary keyboard keys (KEY_Q..KEY_P range)."""
    nbytes = (KEY_MAX // 8) + 1
    buf = bytearray(nbytes)
    try:
        fcntl.ioctl(fd, _eviocgbit(EV_KEY, len(buf)), buf)
    except OSError:
        return False
    # KEY_Q (16) .. KEY_M (50) covers the main typing block.  Plain mice only
    # expose BTN_* codes (>= 0x100), so this cleanly separates the two.
    for code in range(16, 51):
        if buf[code // 8] & (1 << (code % 8)):
            return True
    return False


def list_devices() -> list[DeviceInfo]:
    """Enumerate ``/dev/input/event*`` and classify each device."""
    devices: list[DeviceInfo] = []
    for path in sorted(glob.glob("/dev/input/event*"), key=_event_index):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            types = _event_types(fd)
            info = DeviceInfo(
                path=path,
                name=_device_name(fd),
                has_keys=_has_real_keys(fd),
                has_rel=EV_REL in types,
                has_abs=EV_ABS in types,
            )
        finally:
            os.close(fd)
        if info.kind != "other":
            devices.append(info)
    return devices


def _event_index(path: str) -> int:
    digits = "".join(ch for ch in os.path.basename(path) if ch.isdigit())
    return int(digits) if digits else 0


class EvdevDevice:
    """A real input device opened for reading (and optional exclusive grab)."""

    def __init__(self, path: str):
        self.path = path
        self.fd = os.open(path, os.O_RDONLY)
        self._grabbed = False

    def fileno(self) -> int:
        return self.fd

    def grab(self) -> None:
        """Take exclusive ownership so the local session stops seeing events."""
        if not self._grabbed:
            fcntl.ioctl(self.fd, EVIOCGRAB, 1)
            self._grabbed = True

    def ungrab(self) -> None:
        if self._grabbed:
            try:
                fcntl.ioctl(self.fd, EVIOCGRAB, 0)
            finally:
                self._grabbed = False

    @property
    def grabbed(self) -> bool:
        return self._grabbed

    def read(self) -> Iterator[InputEvent]:
        """Read all currently-available events (call when select() says ready)."""
        data = os.read(self.fd, EVENT_SIZE * 64)
        for offset in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
            _sec, _usec, etype, code, value = _EVENT.unpack_from(data, offset)
            yield InputEvent(etype, code, value)

    def close(self) -> None:
        self.ungrab()
        try:
            os.close(self.fd)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# struct uinput_user_dev  (legacy device-creation ABI, widely supported)
#
#   char            name[UINPUT_MAX_NAME_SIZE];   # 80
#   struct input_id id;                           # 4 * __u16  = 8
#   __u32           ff_effects_max;               # 4
#   __s32           absmax[ABS_CNT];              # 64 * 4
#   __s32           absmin[ABS_CNT];
#   __s32           absfuzz[ABS_CNT];
#   __s32           absflat[ABS_CNT];
# ---------------------------------------------------------------------------
_ABS_CNT = 64
_UINPUT_USER_DEV = struct.Struct(
    "{}s".format(UINPUT_MAX_NAME_SIZE) + "HHHH" + "I" + "{}i".format(_ABS_CNT * 4)
)


class UInputError(RuntimeError):
    pass


class UInput:
    """A synthetic keyboard + relative pointer created via ``/dev/uinput``."""

    def __init__(self, name: str = "Desk Connect Virtual Input"):
        self.name = name
        self.fd = self._open_uinput()
        try:
            self._configure()
        except OSError as exc:  # pragma: no cover - hardware dependent
            os.close(self.fd)
            raise UInputError(f"failed to configure uinput device: {exc}") from exc

    @staticmethod
    def _open_uinput() -> int:
        last: OSError | None = None
        for path in UINPUT_PATHS:
            try:
                return os.open(path, os.O_WRONLY | os.O_NONBLOCK)
            except OSError as exc:
                last = exc
        raise UInputError(
            "cannot open /dev/uinput ({}). Install the udev rule shipped with "
            "Desk Connect and make sure your user is allowed to write to it.".format(
                last
            )
        )

    def _configure(self) -> None:
        fd = self.fd

        # Enable the event types we will replay.
        for ev in (EV_KEY, EV_REL, EV_MSC, EV_SYN):
            fcntl.ioctl(fd, UI_SET_EVBIT, ev)

        # Keyboard keys, plus mouse/extra buttons in the BTN range.
        for code in range(0, 256):
            fcntl.ioctl(fd, UI_SET_KEYBIT, code)
        for code in range(0x100, 0x150):  # BTN_* (mouse, side, extra, task ...)
            fcntl.ioctl(fd, UI_SET_KEYBIT, code)

        for code in (REL_X, REL_Y, REL_WHEEL, REL_HWHEEL, REL_WHEEL_HI_RES,
                     REL_HWHEEL_HI_RES):
            fcntl.ioctl(fd, UI_SET_RELBIT, code)

        fcntl.ioctl(fd, UI_SET_MSCBIT, MSC_SCAN)

        # Describe the device and create it.
        name = self.name.encode("utf-8")[: UINPUT_MAX_NAME_SIZE - 1]
        payload = _UINPUT_USER_DEV.pack(
            name,
            0x03,    # bustype = BUS_USB
            0x1209,  # vendor  (pid.codes "open source" range)
            0xDC01,  # product
            1,       # version
            0,       # ff_effects_max
            *([0] * (_ABS_CNT * 4)),  # abs{max,min,fuzz,flat}
        )
        os.write(fd, payload)
        fcntl.ioctl(fd, UI_DEV_CREATE)

    def write_event(self, event: InputEvent) -> None:
        packed = _EVENT.pack(0, 0, event.type, event.code, event.value)
        os.write(self.fd, packed)

    def syn(self) -> None:
        self.write_event(InputEvent(EV_SYN, SYN_REPORT, 0))

    def close(self) -> None:
        try:
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
