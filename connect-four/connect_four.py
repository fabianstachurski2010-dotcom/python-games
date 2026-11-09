#!/usr/bin/env python3
"""Connect Four — local-network multiplayer for the same Wi-Fi.

    python3 connect_four.py                  # menu: host, join, or watch replays
    python3 connect_four.py --host           # create a room, then wait for a player
    python3 connect_four.py --host --ai hard # one seat against the AI instead
    python3 connect_four.py --join 192.168.1.23:51423
    python3 connect_four.py --watch          # watch the learned AI: AI vs AI, AI vs bot
    python3 connect_four.py --lang en        # English UI (settings persist)
    python3 connect_four.py --shots DIR      # render every screen offscreen (docs/QA)

Only dependency: pygame (``python3 -m pip install pygame``).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

import i18n
import net_client
import settings
import ui


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="connect-four",
        description=i18n.t("app.title") + " · " + i18n.t("app.subtitle"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--host", action="store_true", help="create a room and wait for a player")
    parser.add_argument("--join", metavar="HOST[:PORT]", help="join an existing room, e.g. 192.168.1.23:51423")
    parser.add_argument("--port", type=int, default=0, help="TCP port for --host (default: any free port)")
    parser.add_argument("--ai", choices=("easy", "normal", "hard", "learned"), help="when hosting, play the AI at this level")
    parser.add_argument("--lang", choices=("zh", "en"), help="UI language (default: saved setting, zh)")
    parser.add_argument("--name", help="your display name (default: saved setting or hostname)")
    parser.add_argument("--watch", action="store_true", help="open the Watch-AI screen (AI vs AI / AI vs bot)")
    parser.add_argument("--shots", metavar="DIR", help="render screens to PNG files and exit")
    parser.add_argument("--selftest", action="store_true", help="run the unit test suite and exit")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    return parser


def _split_address(value: str) -> tuple:
    """``192.168.1.23:51423`` -> ``("192.168.1.23", 51423)``; no port -> ``None``."""
    text = value.strip()
    if ":" in text:
        host, _, port_text = text.rpartition(":")
        if port_text.isdigit():
            return host or "127.0.0.1", int(port_text)
    return text or "127.0.0.1", None


def _find_room(host: str) -> int:
    """Look the TCP port up on the LAN when the user only typed an IP."""
    try:
        for room in net_client.discover_rooms(1.6):
            if host in (room.get("ip"), room.get("host")):
                return int(room.get("port") or 0)
    except Exception:  # pragma: no cover - discovery is best effort
        pass
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.version:
        print(f"{i18n.t('app.title')} / Connect Four {ui.VERSION}")
        return 0

    if args.name:
        settings.save(name=args.name.strip()[:16])
    if args.lang:
        settings.save(lang=args.lang)

    if args.selftest:
        import subprocess

        root = Path(__file__).resolve().parent
        cmd = [
            sys.executable,
            "-W",
            "ignore::ResourceWarning",
            "-m",
            "unittest",
            "discover",
            "-s",
            str(root / "tests"),
            "-t",
            str(root),
            "-v",
        ]
        return subprocess.call(cmd)

    if args.shots:
        out = Path(args.shots).expanduser()
        written = ui.run_shots(out, args.lang or i18n.get_lang())
        for path in written:
            print(path)
        return 0

    app = ui.App(lang=args.lang)
    try:
        if args.join:
            host, port = _split_address(args.join)
            if port is None:
                port = _find_room(host)
            app.goto("join", host=host, port=port or 0)
        elif args.watch:
            app.goto("watch")
        elif args.host:
            app.goto("game", mode="host", ai_level=args.ai, port=args.port or 0)
        elif args.port:
            app.goto("game", mode="host", ai_level=args.ai, port=args.port)
        app.run()
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
