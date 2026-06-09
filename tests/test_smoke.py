#!/usr/bin/env python3
"""Smoke + integration tests for Desk Connect.

These stub the kernel device layer (no /dev/input or /dev/uinput required), so
they run on any Linux box and in CI. Run directly:  python3 tests/test_smoke.py
"""

import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import deskconnect.client as client  # noqa: E402
import deskconnect.protocol as proto  # noqa: E402
import deskconnect.server as server  # noqa: E402
from deskconnect.config import DEFAULT_HOTKEY, Settings  # noqa: E402
from deskconnect.linux_input import (  # noqa: E402
    EV_KEY,
    EV_REL,
    REL_X,
    DeviceInfo,
    InputEvent,
)
import deskconnect.linux_input as li  # noqa: E402


def test_ioctl_numbers():
    expected = {
        "UI_DEV_CREATE": 0x5501,
        "UI_DEV_DESTROY": 0x5502,
        "UI_SET_EVBIT": 0x40045564,
        "UI_SET_KEYBIT": 0x40045565,
        "UI_SET_RELBIT": 0x40045566,
        "EVIOCGRAB": 0x40044590,
    }
    for name, val in expected.items():
        assert getattr(li, name) == val, name
    assert li.EVENT_SIZE == 24
    assert li._UINPUT_USER_DEV.size == 1116


def test_protocol_framing():
    ev = InputEvent(EV_KEY, 31, 1)
    wire = proto.encode_event(ev)
    dec = proto.Decoder()
    msgs = dec.feed(wire[:3]) + dec.feed(wire[3:])  # split mid-frame
    assert len(msgs) == 1
    assert proto.decode_event(msgs[0].body) == ev

    buf = proto.encode_hello("server", "h") + wire + proto.encode(proto.KIND_FLUSH)
    kinds = [m.kind for m in proto.Decoder().feed(buf)]
    assert kinds == [proto.KIND_HELLO, proto.KIND_EVENT, proto.KIND_FLUSH]


def test_client_inject_path():
    recorded = []

    class FakeUInput:
        def __init__(self, *a, **k): pass
        def write_event(self, ev): recorded.append(ev)
        def syn(self): pass
        def close(self): pass

    client.UInput = FakeUInput
    from deskconnect import link as _link
    _link.best_cable_address = lambda: "127.0.0.1"  # pretend loopback is the cable

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    events = [InputEvent(EV_KEY, 31, 1), InputEvent(EV_KEY, 31, 0),
              InputEvent(EV_REL, REL_X, 7)]

    def serve():
        conn, _ = srv.accept()
        conn.recv(1024)
        for e in events:
            conn.sendall(proto.encode_event(e))
        time.sleep(0.3)
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    eng = client.ClientEngine(
        Settings(role="client", peer_address="127.0.0.1", listen_port=port),
        lambda m: None, lambda c: None,
    )
    eng.start()
    time.sleep(1.0)
    eng.stop()
    srv.close()
    assert recorded == events, recorded


def test_server_capture_grab_forward():
    class FakeDev:
        def __init__(self, path):
            self.path = path
            self._r, self._w = os.pipe()
            self.grabbed = False
            self._queue = []

        def fileno(self): return self._r
        def grab(self): self.grabbed = True
        def ungrab(self): self.grabbed = False

        def feed(self, events):
            self._queue.extend(events)
            os.write(self._w, b"x")

        def read(self):
            os.read(self._r, 64)
            q, self._queue = self._queue, []
            return iter(q)

        def close(self):
            os.close(self._r)
            os.close(self._w)

    fake = FakeDev("/dev/input/eventX")
    server.list_devices = lambda: [
        DeviceInfo("/dev/input/eventX", "Fake", has_keys=True, has_rel=True)
    ]
    server.EvdevDevice = lambda path: fake
    from deskconnect import link as _link
    _link.best_cable_address = lambda: "127.0.0.1"

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    active = []
    eng = server.ServerEngine(
        Settings(role="server", bind_address="127.0.0.1", listen_port=port),
        lambda m: None, lambda a: active.append(a), lambda c: None,
    )
    eng.start()
    time.sleep(0.3)
    cli = socket.create_connection(("127.0.0.1", port), timeout=2)
    cli.recv(1024)
    time.sleep(0.2)

    # LOCAL state: nothing forwarded.
    fake.feed([InputEvent(EV_REL, REL_X, 3)])
    time.sleep(0.2)
    cli.setblocking(False)
    try:
        early = cli.recv(1024)
    except BlockingIOError:
        early = b""
    assert early == b"", early

    # Hotkey -> REMOTE + grab.
    fake.feed([InputEvent(EV_KEY, c, 1) for c in DEFAULT_HOTKEY])
    time.sleep(0.3)
    assert fake.grabbed
    assert active[-1] is True

    cli.setblocking(True)
    cli.settimeout(2)
    fake.feed([InputEvent(EV_REL, REL_X, 9), InputEvent(EV_KEY, 30, 1)])
    time.sleep(0.2)
    evs = [proto.decode_event(m.body)
           for m in proto.Decoder().feed(cli.recv(4096))
           if m.kind == proto.KIND_EVENT]
    assert InputEvent(EV_REL, REL_X, 9) in evs
    assert InputEvent(EV_KEY, 30, 1) in evs

    eng.stop()
    cli.close()
    assert not fake.grabbed


def test_discovery_cable_only():
    """Beacon carrying a cable IP is accepted; a Wi-Fi-only beacon is rejected."""
    import struct as _struct

    from deskconnect import discovery

    # A beacon advertising a link-local cable address is honoured...
    cable_pkt = discovery.MAGIC + _struct.pack(">H", 24850) + b"169.254.7.7"
    assert discovery._parse(cable_pkt, "192.168.1.50") == ("169.254.7.7", 24850)

    # ...but a beacon with no/!cable advertised IP arriving from a Wi-Fi source
    # is refused, so we never route input over Wi-Fi.
    wifi_pkt = discovery.MAGIC + _struct.pack(">H", 24850)
    assert discovery._parse(wifi_pkt, "192.168.1.50") is None

    # End-to-end over loopback with a proper cable advertisement.
    result = {}

    def listen():
        result["found"] = discovery.discover_server(
            timeout=3.0, should_stop=lambda: False
        )

    t = threading.Thread(target=listen, daemon=True)
    t.start()
    time.sleep(0.3)
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for _ in range(5):
        sender.sendto(cable_pkt, ("127.0.0.1", discovery.DISCOVERY_PORT))
        time.sleep(0.1)
    t.join(timeout=4)
    sender.close()
    assert result.get("found") == ("169.254.7.7", 24850), result.get("found")


def test_pointer_touchpad():
    """Touchpad motion, taps and two-finger scroll are synthesised; mice pass through."""
    from deskconnect.linux_input import (
        ABS_X,
        ABS_Y,
        BTN_LEFT,
        BTN_RIGHT,
        BTN_TOOL_DOUBLETAP,
        BTN_TOOL_FINGER,
        BTN_TOUCH,
        EV_ABS,
        EV_KEY,
        EV_REL,
        REL_WHEEL,
        REL_X,
    )
    from deskconnect.pointer import make_pointer

    # Relative mouse: passes straight through.
    m = make_pointer(False)
    assert m.feed(InputEvent(EV_REL, REL_X, 4)) == [InputEvent(EV_REL, REL_X, 4)]

    # Single-finger motion -> relative deltas (scale 1.0 for a 0..1920 range).
    tp = make_pointer(True, (0, 1920), (0, 1080))
    tp.feed(InputEvent(EV_KEY, BTN_TOUCH, 1))
    tp.feed(InputEvent(EV_KEY, BTN_TOOL_FINGER, 1))
    assert tp.feed(InputEvent(EV_ABS, ABS_X, 100)) == []           # baseline
    assert tp.feed(InputEvent(EV_ABS, ABS_X, 110)) == [InputEvent(EV_REL, REL_X, 10)]

    # Quick light single-finger contact -> left click.
    tp = make_pointer(True, (0, 1920), (0, 1080))
    tp.feed(InputEvent(EV_KEY, BTN_TOUCH, 1))
    tp.feed(InputEvent(EV_KEY, BTN_TOOL_FINGER, 1))
    out = tp.feed(InputEvent(EV_KEY, BTN_TOUCH, 0))
    assert InputEvent(EV_KEY, BTN_LEFT, 1) in out
    assert InputEvent(EV_KEY, BTN_LEFT, 0) in out

    # Two-finger tap -> right click.
    tp = make_pointer(True, (0, 1920), (0, 1080))
    tp.feed(InputEvent(EV_KEY, BTN_TOUCH, 1))
    tp.feed(InputEvent(EV_KEY, BTN_TOOL_DOUBLETAP, 1))
    out = tp.feed(InputEvent(EV_KEY, BTN_TOUCH, 0))
    assert InputEvent(EV_KEY, BTN_RIGHT, 1) in out

    # Two-finger drag -> scroll wheel (no cursor motion).
    tp = make_pointer(True, (0, 1080), (0, 1080))  # y scale = 1.0
    tp.feed(InputEvent(EV_KEY, BTN_TOUCH, 1))
    tp.feed(InputEvent(EV_KEY, BTN_TOOL_DOUBLETAP, 1))
    tp.feed(InputEvent(EV_ABS, ABS_Y, 100))            # baseline
    out = tp.feed(InputEvent(EV_ABS, ABS_Y, 200))      # 100px -> several notches
    assert out and all(e.code == REL_WHEEL for e in out), out
    assert not any(e.code == REL_X for e in out)


def test_server_onscreen_toggle():
    """request_toggle() must switch control without the hotkey."""
    class FakeDev:
        def __init__(self, path):
            self.path = path
            self._r, self._w = os.pipe()
            self.grabbed = False
            self._queue = []

        def fileno(self): return self._r
        def grab(self): self.grabbed = True
        def ungrab(self): self.grabbed = False

        def feed(self, events):
            self._queue.extend(events)
            os.write(self._w, b"x")

        def read(self):
            os.read(self._r, 64)
            q, self._queue = self._queue, []
            return iter(q)

        def close(self):
            os.close(self._r)
            os.close(self._w)

    fake = FakeDev("/dev/input/eventX")
    server.list_devices = lambda: [
        DeviceInfo("/dev/input/eventX", "Fake", has_keys=True, has_rel=True)
    ]
    server.EvdevDevice = lambda path: fake
    from deskconnect import link as _link
    _link.best_cable_address = lambda: "127.0.0.1"

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    eng = server.ServerEngine(
        Settings(role="server", bind_address="127.0.0.1", listen_port=port),
        lambda m: None, lambda a: None, lambda c: None,
    )
    eng.start()
    time.sleep(0.3)
    cli = socket.create_connection(("127.0.0.1", port), timeout=2)
    cli.recv(1024)
    time.sleep(0.2)

    eng.request_toggle()  # on-screen button equivalent
    time.sleep(0.3)
    assert fake.grabbed, "toggle did not grab"

    cli.settimeout(2)
    fake.feed([InputEvent(EV_REL, REL_X, 5)])
    time.sleep(0.2)
    evs = [proto.decode_event(m.body)
           for m in proto.Decoder().feed(cli.recv(4096))
           if m.kind == proto.KIND_EVENT]
    assert InputEvent(EV_REL, REL_X, 5) in evs
    eng.stop()
    cli.close()


def main():
    tests = [
        test_ioctl_numbers,
        test_protocol_framing,
        test_client_inject_path,
        test_server_capture_grab_forward,
        test_discovery_cable_only,
        test_pointer_touchpad,
        test_server_onscreen_toggle,
    ]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} tests passed")


if __name__ == "__main__":
    main()
