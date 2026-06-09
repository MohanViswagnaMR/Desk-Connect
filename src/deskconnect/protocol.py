"""Desk Connect wire protocol.

The link between the two machines is an ordinary TCP stream (carried over the
USB-C / Thunderbolt network interface).  Messages are length-prefixed so we
never depend on packet boundaries:

    +--------+----------------------+
    | u32 be | payload (len bytes)  |
    +--------+----------------------+

The first payload byte is the message kind.  Input events are deliberately
tiny -- a kind byte plus ``type/code/value`` -- so the round-trip stays well
under a millisecond on a direct cable.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass

from .linux_input import InputEvent

PROTOCOL_VERSION = 1
DEFAULT_PORT = 24850  # near Barrier's 24800, but distinct so they can coexist

# Message kinds
KIND_HELLO = 0x01  # JSON handshake
KIND_EVENT = 0x02  # type(u16) code(u16) value(i32)
KIND_FLUSH = 0x03  # SYN_REPORT boundary marker (no body)
KIND_PING = 0x04
KIND_PONG = 0x05
KIND_BYE = 0x06

_LEN = struct.Struct(">I")
_EVENT_BODY = struct.Struct(">HHi")


def encode(kind: int, body: bytes = b"") -> bytes:
    payload = bytes([kind]) + body
    return _LEN.pack(len(payload)) + payload


def encode_hello(role: str, hostname: str) -> bytes:
    body = json.dumps(
        {"version": PROTOCOL_VERSION, "role": role, "hostname": hostname}
    ).encode("utf-8")
    return encode(KIND_HELLO, body)


def encode_event(event: InputEvent) -> bytes:
    return encode(KIND_EVENT, _EVENT_BODY.pack(event.type, event.code, event.value))


def decode_event(body: bytes) -> InputEvent:
    etype, code, value = _EVENT_BODY.unpack(body)
    return InputEvent(etype, code, value)


@dataclass
class Message:
    kind: int
    body: bytes


class Decoder:
    """Incremental, allocation-light framing decoder for a TCP stream."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[Message]:
        self._buf.extend(data)
        messages: list[Message] = []
        while True:
            if len(self._buf) < _LEN.size:
                break
            (length,) = _LEN.unpack_from(self._buf, 0)
            if len(self._buf) < _LEN.size + length:
                break
            start = _LEN.size
            payload = self._buf[start : start + length]
            del self._buf[: start + length]
            if not payload:
                continue
            messages.append(Message(kind=payload[0], body=bytes(payload[1:])))
        return messages
