"""Entry point: ``python -m deskconnect`` and the installed launcher both land here.

A small headless CLI is provided too, so the engine can be exercised without a
display server (useful on the client machine over SSH, and for CI smoke tests).
"""

from __future__ import annotations

import argparse
import signal
import sys
import time

from .config import Settings


def _run_headless(args: argparse.Namespace) -> int:
    from .client import ClientEngine
    from .server import ServerEngine

    settings = Settings.load()
    if args.port:
        settings.listen_port = args.port

    def status(msg: str) -> None:
        print(f"[deskconnect] {msg}", flush=True)

    if args.role == "server":
        engine = ServerEngine(settings, status, lambda a: None, lambda a: None)
    else:
        if not args.peer:
            print("error: --peer ADDRESS is required for the client role", file=sys.stderr)
            return 2
        settings.peer_address = args.peer
        engine = ClientEngine(settings, status, lambda c: None)

    engine.start()
    stopping = {"v": False}

    def handle(*_):
        stopping["v"] = True

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)
    try:
        while not stopping["v"]:
            time.sleep(0.2)
    finally:
        engine.stop()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="deskconnect")
    parser.add_argument(
        "--headless", action="store_true",
        help="run the engine without the GTK interface",
    )
    parser.add_argument(
        "--autostart", action="store_true",
        help="launched at login; auto-connect when the cable is present",
    )
    parser.add_argument("--role", choices=["server", "client"], default="server")
    parser.add_argument("--peer", help="server address (client role)")
    parser.add_argument("--port", type=int, help="override the TCP port")
    args = parser.parse_args()

    if args.headless:
        return _run_headless(args)

    from .app import main as gui_main
    return gui_main(autostart_requested=args.autostart)


if __name__ == "__main__":
    sys.exit(main())
