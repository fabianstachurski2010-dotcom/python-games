#!/usr/bin/env python3
"""LAN client for Battleship — thin TCP JSON-lines client plus UDP room discovery.

The server is authoritative: this module only sends *intents* (``place``,
``fire``, ``chat``, ``rematch``, ``lobby``) and hands every server message to
the UI through :meth:`NetClient.poll`.  No Tkinter import lives here.

Usage
    from net_client import NetClient, NetError, discover_rooms

    rooms = discover_rooms()                    # [{"code","host","port",...}]
    client = NetClient()
    client.connect("192.168.1.20", 50007, "Kyle")
    ...
    for msg in client.poll():
        if msg["t"] == "sync":
            render(msg)
    client.leave()

Wire format: one UTF-8 JSON object per line, newline terminated, at most
64 KiB per line (SPEC §3.1).
"""

from __future__ import annotations

import json
import queue
import socket
import threading
import time

# Shared discovery constants (net_server imports these; keeps a single source).
DISCOVERY_PORT = 54321
DISCOVERY_PROBE = b'{"t":"battleship.discover","ver":1}'

MAX_LINE = 64 * 1024
_LINE_LIMIT = MAX_LINE


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

class NetError(Exception):
    """Network/protocol failure.

    ``code`` is a stable machine-readable string the UI can localise, e.g.
    ``connect_failed``, ``connect_timeout``, ``room_full``, ``name_too_long``,
    ``not_connected`` or ``connection_lost``.
    """

    def __init__(self, code: str = "error", message: str = ""):
        self.code = code
        self.message = message or code
        super().__init__(self.message)


class _ReaderDone(Exception):
    """Internal: the reader thread has a reason to stop reading."""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def local_ip() -> str:
    """Best-effort local LAN IPv4 address; ``""`` when it cannot be found.

    Uses a UDP ``connect`` (no packet is sent) so it never blocks on DNS.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    finally:
        try:
            sock.close()
        except OSError:
            pass
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return ""


# --------------------------------------------------------------------------- #
# NetClient
# --------------------------------------------------------------------------- #

class NetClient:
    """One player's connection to a :class:`net_server.GameServer`.

    All public ``send_*`` helpers are safe to call from the Tk thread; the
    reader thread only ever appends to the internal queue consumed by
    :meth:`poll`.
    """

    def __init__(self) -> None:
        self.code: str | None = None
        self.you: int | None = None
        self._sock: socket.socket | None = None
        self._reader: threading.Thread | None = None
        self._inbox: queue.Queue[dict] = queue.Queue()
        self._send_lock = threading.Lock()
        self._welcome = threading.Event()
        self._welcome_msg: dict | None = None
        self._closed = threading.Event()
        self._stopping = False
        self._connected = False
        self._buf = b""

    # -- connection lifecycle ---------------------------------------------- #
    @property
    def connected(self) -> bool:
        """True from the moment ``welcome`` arrives until the socket drops."""
        return self._connected

    def connect(self, host: str, port: int, name: str, timeout: float = 5.0) -> None:
        """Open the TCP connection, send ``hello`` and wait for ``welcome``.

        Raises :class:`NetError` with code ``connect_failed``,
        ``connect_timeout``, ``room_full``, ``name_too_long``,
        ``already_started`` or ``connection_lost``.
        """
        if self._sock is not None or self._reader is not None:
            raise NetError("already_connected", "client is already connected")
        self._welcome.clear()
        self._welcome_msg = None
        deadline = time.monotonic() + max(0.05, timeout)
        try:
            sock = socket.create_connection((host, int(port)), timeout=max(0.05, timeout))
        except socket.timeout as exc:
            raise NetError("connect_timeout", f"connecting to {host}:{port} timed out") from exc
        except OSError as exc:
            raise NetError("connect_failed", f"could not connect to {host}:{port}: {exc}") from exc

        self._sock = sock
        try:
            sock.settimeout(None)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

        self._reader = threading.Thread(
            target=self._reader_loop, name="battleship-client-reader", daemon=True
        )
        self._reader.start()

        try:
            self._write({"t": "hello", "name": name, "ver": 1})
        except NetError:
            self.close()
            raise

        remaining = max(0.0, deadline - time.monotonic())
        if not self._welcome.wait(remaining):
            self.close()
            raise NetError("connect_timeout", "the server did not answer the hello")

        # ``welcome`` is consumed by the reader (kept out of poll()) and stashed
        # in ``_welcome_msg``; anything else stays queued for poll().
        pending: list[dict] = []
        error_code = ""
        while True:
            try:
                msg = self._inbox.get_nowait()
            except queue.Empty:
                break
            if msg.get("t") == "error" and not error_code:
                error_code = str(msg.get("code") or "connect_failed")
            else:
                pending.append(msg)
        for msg in pending:
            self._inbox.put(msg)

        if self._welcome_msg is not None and not self._closed.is_set():
            self._connected = True
            return
        self.close()
        if error_code:
            raise NetError(error_code, f"the server refused the connection ({error_code})")
        if self._welcome_msg is not None:
            raise NetError("connection_lost", "the connection closed during the handshake")
        raise NetError("connect_failed", "the server closed the connection during the handshake")

    def leave(self) -> None:
        """Tell the server we are leaving, then close (safe to call twice)."""
        if self._connected and not self._stopping:
            try:
                self._write({"t": "leave"})
            except NetError:
                pass
        self.close()

    def close(self) -> None:
        """Idempotent: shut the socket down and join the reader thread."""
        self._stopping = True
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(2.0)
        self._connected = False

    # -- intents ------------------------------------------------------------ #
    def send_place(self, ships: list[dict]) -> None:
        """Submit the complete five-ship fleet (engine format)."""
        self._write({"t": "place", "ships": ships})

    def send_fire(self, r: int, c: int) -> None:
        """Fire at row *r*, column *c* (0..9)."""
        self._write({"t": "fire", "r": int(r), "c": int(c)})

    def send_chat(self, text: str) -> None:
        """Send a chat line (the server trims it and caps it at 200 chars)."""
        self._write({"t": "chat", "text": str(text)})

    def send_rematch(self) -> None:
        """Vote for a rematch (only meaningful while the game is over)."""
        self._write({"t": "rematch"})

    def send_lobby(self) -> None:
        """Clear a departed opponent's slot and return to the waiting lobby."""
        self._write({"t": "lobby"})

    def send_ping(self) -> None:
        """Keepalive; the server answers with ``{"t":"pong"}``."""
        self._write({"t": "ping"})

    # -- inbound ------------------------------------------------------------ #
    def poll(self) -> list[dict]:
        """Non-blocking drain of every server message received so far.

        Thread-safe.  Once the reader thread stops, the list ends with
        ``{"t": "closed", "reason": ...}`` exactly once.
        """
        messages: list[dict] = []
        while True:
            try:
                messages.append(self._inbox.get_nowait())
            except queue.Empty:
                return messages

    # -- internals ---------------------------------------------------------- #
    def _write(self, payload: dict) -> None:
        with self._send_lock:
            if self._stopping or self._sock is None:
                raise NetError("not_connected", "the client is not connected")
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
            try:
                self._sock.sendall(data)
            except OSError as exc:
                self._connected = False
                raise NetError("connection_lost", str(exc)) from exc

    def _reader_loop(self) -> None:
        sock = self._sock
        reason = "connection_lost"
        if sock is None:
            self._finish_reader("not_connected")
            return
        try:
            while not self._stopping:
                try:
                    chunk = sock.recv(4096)
                except OSError:
                    reason = "client_closed" if self._stopping else "connection_lost"
                    break
                if not chunk:
                    reason = "connection_closed"
                    break
                self._buf += chunk
                while b"\n" in self._buf:
                    raw, self._buf = self._buf.split(b"\n", 1)
                    if len(raw) > _LINE_LIMIT:
                        raise _ReaderDone("protocol_error")
                    self._handle_line(raw)
                if len(self._buf) > _LINE_LIMIT:
                    raise _ReaderDone("protocol_error")
        except _ReaderDone as done:
            reason = str(done) or "protocol_error"
        except OSError:
            reason = "connection_lost"
        finally:
            self._finish_reader(reason)

    def _finish_reader(self, reason: str) -> None:
        self._connected = False
        self._welcome.set()
        self._closed.set()
        self._inbox.put({"t": "closed", "reason": reason})

    def _handle_line(self, raw: bytes) -> None:
        if not raw.strip():
            return
        try:
            msg = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return
        if not isinstance(msg, dict):
            return
        kind = msg.get("t")
        if kind == "welcome":
            self.you = msg.get("you")
            self.code = msg.get("code")
            self._welcome_msg = msg
            self._welcome.set()
        elif kind == "bye":
            self._inbox.put(msg)
            raise _ReaderDone(msg.get("reason") or "host_left")
        else:
            self._inbox.put(msg)


# --------------------------------------------------------------------------- #
# UDP discovery
# --------------------------------------------------------------------------- #

def _broadcast_targets() -> list[str]:
    """Every address a local room responder may be reachable on."""
    targets = ["255.255.255.255", "127.0.0.1"]
    ip = local_ip()
    if ip:
        parts = ip.split(".")
        if len(parts) == 4:
            targets.append(".".join(parts[:3] + ["255"]))
    return targets


def discover_rooms(timeout: float = 1.2, udp_port: int = DISCOVERY_PORT) -> list[dict]:
    """Broadcast the discovery probe and collect room replies for *timeout* s.

    Returns a de-duplicated list of ``{"code","host","port","names","state"}``
    dicts (empty list when UDP is blocked or no room answers — never raises).
    """
    if timeout <= 0:
        return []
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return []
    rooms: dict[tuple[str, str], dict] = {}
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", 0))
        for target in _broadcast_targets():
            try:
                sock.sendto(DISCOVERY_PROBE, (target, int(udp_port)))
            except OSError:
                continue
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                break
            except OSError:
                break
            try:
                msg = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if not isinstance(msg, dict) or msg.get("t") != "battleship.room":
                continue
            code = msg.get("code")
            port = msg.get("port")
            if not isinstance(code, str) or type(port) is not int:
                continue
            host = msg.get("host") or addr[0]
            if not isinstance(host, str) or not host:
                host = addr[0]
            names = msg.get("names")
            if not isinstance(names, list):
                names = []
            state = msg.get("state")
            if not isinstance(state, str):
                state = "lobby"
            key = (host, code)
            rooms.setdefault(
                key,
                {"code": code, "host": host, "port": port, "names": names, "state": state},
            )
    except OSError:
        return []
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return list(rooms.values())


if __name__ == "__main__":  # pragma: no cover - manual smoke helper
    found = discover_rooms()
    print(f"{len(found)} room(s) found")
    for room in found:
        print(room)
