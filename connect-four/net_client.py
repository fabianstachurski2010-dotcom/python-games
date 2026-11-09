#!/usr/bin/env python3
"""Client half of the LAN protocol: TCP JSON-lines + UDP room discovery.

The server is authoritative; this module only moves messages.  The pygame UI
polls :meth:`NetClient.poll` once per frame and renders the newest ``sync``.
"""

from __future__ import annotations

import json
import queue
import socket
import threading
import time
from typing import List, Optional

PROTOCOL_VERSION = 1

#: UDP port used only for "who is hosting on this LAN?" broadcasts.  54321 is
#: taken by the battleship game in this workspace, so connect-four uses 54322.
DISCOVERY_PORT = 54322
DISCOVERY_PROBE = {"t": "connect4.discover", "ver": PROTOCOL_VERSION}
DISCOVERY_REPLY = "connect4.room"

MAX_LINE = 64 * 1024
CONNECT_TIMEOUT = 4.0


def local_ip() -> str:
    """Best-effort LAN IPv4 address of this machine (no packets are sent)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        sock.close()


def discover_rooms(timeout: float = 1.2) -> List[dict]:
    """Broadcast a probe on the LAN and collect host replies.

    Returns a list of ``{"code", "port", "host", "ip", "players", "phase"}``
    dicts, one per responding host.  Never raises: discovery is optional and the
    manual IP field always works.
    """
    rooms: dict = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(0.15)
    probe = json.dumps(DISCOVERY_PROBE).encode("utf-8") + b"\n"
    targets = [("255.255.255.255", DISCOVERY_PORT), ("127.0.0.1", DISCOVERY_PORT)]
    deadline = time.monotonic() + timeout
    try:
        for target in targets:
            try:
                sock.sendto(probe, target)
            except OSError:
                pass
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = json.loads(data.decode("utf-8", "replace").strip())
            except ValueError:
                continue
            if not isinstance(msg, dict) or msg.get("t") != DISCOVERY_REPLY:
                continue
            key = (msg.get("code"), addr[0], msg.get("port"))
            if key in rooms:
                continue
            rooms[key] = {
                "code": str(msg.get("code", "????")),
                "port": int(msg.get("port", 0)),
                "host": str(msg.get("host", "?")),
                "ip": addr[0],
                "players": int(msg.get("players", 0)),
                "phase": str(msg.get("phase", "lobby")),
            }
    finally:
        sock.close()
    return sorted(rooms.values(), key=lambda r: (r["host"], r["ip"]))


def _encode(msg: dict) -> bytes:
    return json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


class NetClient:
    """Thread-safe TCP client: one reader thread, a queue for the UI."""

    def __init__(self, host: str, port: int, name: str, role: str = "player", timeout: float = CONNECT_TIMEOUT) -> None:
        self.host = host
        self.port = int(port)
        self.name = name
        self.role = role
        self._queue: "queue.Queue[dict]" = queue.Queue()
        self._send_lock = threading.Lock()
        self._closed = threading.Event()
        self._reader: Optional[threading.Thread] = None
        self.error: Optional[str] = None
        self._sock = socket.create_connection((host, self.port), timeout=timeout)
        self._sock.settimeout(None)
        self._file = self._sock.makefile("rb")

    # -- lifecycle --------------------------------------------------------- #

    def start(self) -> None:
        self._reader = threading.Thread(target=self._read_loop, daemon=True, name="c4-net")
        self._reader.start()
        self.send({"t": "hello", "name": self.name, "role": self.role, "ver": PROTOCOL_VERSION})

    def _read_loop(self) -> None:
        try:
            while not self._closed.is_set():
                line = self._file.readline()
                if not line:
                    break
                if len(line) > MAX_LINE:
                    break
                try:
                    msg = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    self._queue.put(msg)
        except OSError as exc:
            self.error = str(exc)
        finally:
            self._closed.set()
            self._queue.put({"t": "_disconnected"})

    # -- io ---------------------------------------------------------------- #

    @property
    def alive(self) -> bool:
        return not self._closed.is_set()

    def send(self, msg: dict) -> bool:
        if self._closed.is_set():
            return False
        data = _encode(msg)
        try:
            with self._send_lock:
                self._sock.sendall(data)
            return True
        except OSError:
            self._closed.set()
            self._queue.put({"t": "_disconnected"})
            return False

    def poll(self) -> List[dict]:
        """Drain every message received since the last call."""
        out: List[dict] = []
        while True:
            try:
                out.append(self._queue.get_nowait())
            except queue.Empty:
                return out

    def wait_for(self, kind: str, timeout: float = 5.0) -> Optional[dict]:
        """Blocking helper for CLI flows and tests: first message of ``kind``."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for msg in self.poll():
                if msg.get("t") == kind:
                    return msg
            time.sleep(0.01)
        return None

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        for closer in (self._file, self._sock):
            try:
                closer.close()
            except OSError:
                pass
        self._queue.put({"t": "_disconnected"})
