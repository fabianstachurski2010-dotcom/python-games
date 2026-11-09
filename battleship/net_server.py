#!/usr/bin/env python3
"""Authoritative LAN server for Battleship — TCP JSON-lines + UDP discovery.

One host process runs a single :class:`GameServer`.  It owns the
:class:`engine.Game`, validates every client intent, and pushes a **full state
snapshot** (``sync``) to both players after every accepted change (SPEC §3.3).
Clients are thin: they only send intents and render the last snapshot.

Usage
    from net_server import GameServer

    with GameServer(discovery=True) as server:   # context manager starts/stops
        print(server.code, server.port, server.lan_ip)
        ...                                     # the UI runs its Tk loop here

    # or explicitly:
    server = GameServer(tcp_port=0, discovery=True)
    code, port = server.start()
    ...
    server.stop()

No Tkinter import, no third-party code.  Threads: one accept loop, one reader
per client, one UDP discovery responder.  The room state itself is guarded by a
single re-entrant lock.
"""

from __future__ import annotations

import json
import random
import socket
import threading
import time

import engine
from net_client import DISCOVERY_PORT, DISCOVERY_PROBE, local_ip

MAX_LINE = 64 * 1024
MAX_LOG = 300
MAX_CHAT = 100
MAX_NAME = 16
MAX_CHAT_TEXT = 200
JOIN_WAIT_SECONDS = 3.0

# Placement error codes that the wire protocol exposes verbatim; every other
# engine PlacementError collapses to "bad_placement" (SPEC §3.2).
_PLACEMENT_CODES = frozenset({"wrong_fleet", "overlap"})


def _now_ms() -> int:
    return int(time.time() * 1000)


# --------------------------------------------------------------------------- #
# Wire connection wrapper
# --------------------------------------------------------------------------- #

class _Conn:
    """One accepted TCP socket: newline-JSON sender plus its reader thread."""

    __slots__ = ("sock", "addr", "send_lock", "alive", "slot", "name", "thread", "_closed")

    def __init__(self, sock: socket.socket, addr: tuple) -> None:
        self.sock = sock
        self.addr = addr
        self.send_lock = threading.Lock()
        self.alive = True
        self.slot: int | None = None
        self.name = ""
        self.thread: threading.Thread | None = None
        self._closed = False

    def send(self, payload: dict) -> bool:
        """Send one JSON frame; returns False once the peer is gone."""
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
        with self.send_lock:
            if not self.alive:
                return False
            try:
                self.sock.sendall(data)
            except OSError:
                self.alive = False
                return False
        return True

    def close(self) -> None:
        """Close the socket (idempotent, safe from any thread)."""
        self.alive = False
        if self._closed:
            return
        self._closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class _Player:
    """A room slot.  ``conn is None`` once the player disconnected for good."""

    __slots__ = ("name", "conn", "connected")

    def __init__(self, name: str, conn: _Conn | None, connected: bool = True) -> None:
        self.name = name
        self.conn = conn
        self.connected = connected


# --------------------------------------------------------------------------- #
# GameServer
# --------------------------------------------------------------------------- #

class GameServer:
    """The authoritative room: two slots, one engine.Game, one room code."""

    def __init__(self, tcp_port: int = 0, discovery: bool = True, verbose: bool = False) -> None:
        self.code: str = f"{random.randint(1000, 9999)}"
        self.port: int = 0
        self.lan_ip: str = local_ip()
        self.running: bool = False
        self.discovery_active: bool = False

        self._tcp_port = int(tcp_port)
        self._discovery = bool(discovery)
        self._verbose = bool(verbose)

        self._lock = threading.RLock()
        self._conns: set[_Conn] = set()
        self._tcp: socket.socket | None = None
        self._discovery_sock: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._discovery_thread: threading.Thread | None = None
        self._client_threads: list[threading.Thread] = []

        # Room state (all guarded by self._lock).
        self._state = "lobby"
        self._slots: list[_Player | None] = [None, None]
        self._wins = [0, 0]
        self._rematch = [False, False]
        self._chat: list[dict] = []
        self._log: list[dict] = []
        self._game = engine.Game()
        self._game_log_len = 0
        self._match_seq = 0
        self._scored = False
        self._notice: str | None = None

    # -- lifecycle ---------------------------------------------------------- #
    def start(self) -> tuple[str, int]:
        """Bind the TCP socket (and UDP discovery) and start serving.

        Returns ``(code, port)``.  Raises ``OSError`` if the TCP bind fails;
        a failed UDP discovery bind only disables discovery.
        """
        with self._lock:
            if self.running:
                return self.code, self.port
            tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                tcp.bind(("0.0.0.0", self._tcp_port))
                tcp.listen(8)
                tcp.settimeout(0.5)
            except OSError:
                try:
                    tcp.close()
                except OSError:
                    pass
                raise
            self._tcp = tcp
            self.port = int(tcp.getsockname()[1])
            self.running = True
            self._accept_thread = threading.Thread(
                target=self._accept_loop, args=(tcp,), name="battleship-accept", daemon=True
            )
            self._accept_thread.start()
            if self._discovery:
                self._start_discovery()
        if self._verbose:
            print(f"[battleship] room {self.code} on 0.0.0.0:{self.port} (lan {self.lan_ip or '?'})")
        return self.code, self.port

    def stop(self) -> None:
        """Send ``bye``, close every socket and join all threads (max ~3 s)."""
        with self._lock:
            if not self.running and self._tcp is None and self._discovery_sock is None:
                return
            self.running = False
            tcp = self._tcp
            self._tcp = None
            discovery_sock = self._discovery_sock
            self._discovery_sock = None
            self.discovery_active = False

        if tcp is not None:
            try:
                tcp.close()
            except OSError:
                pass
        accept_thread = self._accept_thread
        self._accept_thread = None
        if accept_thread is not None and accept_thread is not threading.current_thread():
            accept_thread.join(0.8)

        with self._lock:
            conns = list(self._conns)
            self._conns.clear()
            client_threads = list(self._client_threads)
            self._client_threads.clear()
        for conn in conns:
            conn.send({"t": "bye", "reason": "host_left"})
        for conn in conns:
            conn.close()

        if discovery_sock is not None:
            try:
                discovery_sock.close()
            except OSError:
                pass
        discovery_thread = self._discovery_thread
        self._discovery_thread = None
        if discovery_thread is not None and discovery_thread is not threading.current_thread():
            discovery_thread.join(0.5)

        deadline = time.monotonic() + JOIN_WAIT_SECONDS
        for thread in client_threads:
            if thread is threading.current_thread():
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)
        if self._verbose:
            print(f"[battleship] room {self.code} stopped")

    def __enter__(self) -> "GameServer":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    def client_count(self) -> int:
        """Number of players currently connected to the room."""
        with self._lock:
            return sum(1 for player in self._slots if player is not None and player.connected)

    # -- TCP accept / reader ------------------------------------------------ #
    def _accept_loop(self, tcp: socket.socket) -> None:
        while self.running:
            try:
                client, addr = tcp.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self.running:
                try:
                    client.close()
                except OSError:
                    pass
                break
            try:
                client.settimeout(None)
                client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            conn = _Conn(client, addr)
            thread = threading.Thread(
                target=self._client_loop, args=(conn,),
                name=f"battleship-client-{addr[1]}", daemon=True,
            )
            conn.thread = thread
            with self._lock:
                self._conns.add(conn)
                self._client_threads.append(thread)
            thread.start()

    def _client_loop(self, conn: _Conn) -> None:
        buffer = b""
        try:
            while self.running and conn.alive:
                try:
                    chunk = conn.sock.recv(4096)
                except OSError:
                    break
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    if len(raw) > MAX_LINE:
                        if self._verbose:
                            print(f"[battleship] oversized frame from {conn.addr}")
                        return
                    if not self._process_line(conn, raw):
                        return
                if len(buffer) > MAX_LINE:
                    if self._verbose:
                        print(f"[battleship] oversized frame from {conn.addr}")
                    return
        finally:
            with self._lock:
                self._conns.discard(conn)
            self._on_conn_closed(conn)
            conn.close()

    def _process_line(self, conn: _Conn, raw: bytes) -> bool:
        """Handle one frame; ``False`` means the reader should stop."""
        if not raw.strip():
            return True
        try:
            msg = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._error(conn, "bad_message", "frame is not valid UTF-8 JSON")
            return True
        if not isinstance(msg, dict):
            self._error(conn, "bad_message", "frame must be a JSON object")
            return True
        with self._lock:
            if conn.slot is None:
                return self._handshake(conn, msg)
            return self._dispatch(conn, msg)

    def _handshake(self, conn: _Conn, msg: dict) -> bool:
        if msg.get("t") != "hello":
            self._error(conn, "bad_message", "the first message must be hello")
            return False
        return self._handle_hello(conn, msg)

    # -- message handlers --------------------------------------------------- #
    def _handle_hello(self, conn: _Conn, msg: dict) -> bool:
        name = msg.get("name", "")
        if not isinstance(name, str):
            self._error(conn, "bad_message", "name must be a string")
            return False
        name = name.strip()
        if not name:
            self._error(conn, "bad_message", "name must not be empty")
            return False
        if len(name) > MAX_NAME:
            self._error(conn, "name_too_long", f"name must be at most {MAX_NAME} characters")
            return False
        if all(player is not None for player in self._slots):
            self._error(conn, "room_full", "the room already has two players")
            return False
        if self._state in ("placing", "battle"):
            self._error(conn, "already_started", "the game already started")
            return False

        slot = 0 if self._slots[0] is None else 1
        conn.slot = slot
        conn.name = name
        self._slots[slot] = _Player(name, conn, True)
        conn.send({"t": "welcome", "you": slot, "code": self.code, "name": name, "ver": 1})
        self._append_log({"ts": _now_ms(), "kind": "join", "name": name})
        notice = None
        if self._slots[0] is not None and self._slots[1] is not None:
            self._begin_placing(keep_scores=False)
            notice = "new_opponent"
        self._broadcast(notice)
        return True

    def _dispatch(self, conn: _Conn, msg: dict) -> bool:
        kind = msg.get("t")
        if kind == "place":
            self._handle_place(conn, msg)
        elif kind == "fire":
            self._handle_fire(conn, msg)
        elif kind == "chat":
            self._handle_chat(conn, msg)
        elif kind == "rematch":
            self._handle_rematch(conn)
        elif kind == "lobby":
            self._handle_lobby(conn)
        elif kind == "ping":
            conn.send({"t": "pong"})
        elif kind == "leave":
            self._on_conn_closed(conn)
            return False
        elif kind == "hello":
            self._error(conn, "already_started", "this connection has already joined")
        else:
            self._error(conn, "bad_message", f"unknown message type {kind!r}")
        return True

    def _handle_place(self, conn: _Conn, msg: dict) -> None:
        if self._state != "placing":
            self._error(conn, "bad_phase", "placement is closed")
            return
        ships = msg.get("ships")
        if not isinstance(ships, list):
            self._error(conn, "bad_placement", "ships must be a list")
            return
        try:
            self._game.place_fleet(conn.slot, ships)
        except engine.BattleshipError as exc:
            code = exc.code if exc.code in _PLACEMENT_CODES else "bad_placement"
            self._error(conn, code, exc.message)
            return
        except (TypeError, ValueError, IndexError) as exc:
            self._error(conn, "bad_placement", str(exc))
            return
        if self._game.both_placed():
            self._game.start_battle(self._game.starter)
            self._drain_game_log()
            self._state = "battle"
        self._broadcast()

    def _handle_fire(self, conn: _Conn, msg: dict) -> None:
        if self._state != "battle":
            self._error(conn, "bad_phase", "you can only fire during battle")
            return
        r = msg.get("r")
        c = msg.get("c")
        if type(r) is not int or type(c) is not int:
            self._error(conn, "bad_message", "r and c must be integers")
            return
        try:
            self._game.fire(conn.slot, r, c)
        except engine.BattleshipError as exc:
            self._error(conn, exc.code, exc.message)
            return
        self._drain_game_log()
        if self._game.phase == "over":
            self._state = "over"
            self._score_winner()
        self._broadcast()

    def _handle_chat(self, conn: _Conn, msg: dict) -> None:
        text = msg.get("text")
        if not isinstance(text, str):
            self._error(conn, "bad_message", "chat text must be a string")
            return
        text = text.strip()[:MAX_CHAT_TEXT]
        if not text:
            return
        self._chat.append({"ts": _now_ms(), "from": conn.name, "text": text})
        if len(self._chat) > MAX_CHAT:
            del self._chat[:-MAX_CHAT]
        self._broadcast()

    def _handle_rematch(self, conn: _Conn) -> None:
        if self._state != "over":
            self._error(conn, "bad_phase", "a rematch is only possible after a game")
            return
        other = 1 - conn.slot
        opponent = self._slots[other]
        if opponent is None or not opponent.connected:
            self._error(conn, "bad_phase", "there is no opponent to play a rematch with")
            return
        self._rematch[conn.slot] = True
        if all(self._rematch):
            self._begin_placing(keep_scores=True)
            self._append_log({"ts": _now_ms(), "kind": "rematch"})
            self._broadcast("rematch")
        else:
            self._broadcast()

    def _handle_lobby(self, conn: _Conn) -> None:
        if self._state != "over":
            self._error(conn, "bad_phase", "lobby is only available after a game")
            return
        other = 1 - conn.slot
        opponent = self._slots[other]
        if opponent is not None and opponent.connected:
            self._error(conn, "bad_phase", "the opponent is still connected")
            return
        self._slots[other] = None
        self._reset_to_lobby()
        self._broadcast()

    # -- room transitions --------------------------------------------------- #
    def _begin_placing(self, keep_scores: bool) -> None:
        self._state = "placing"
        self._game = engine.Game()
        self._game_log_len = 0
        self._game.starter = self._match_seq % 2
        self._match_seq += 1
        self._rematch = [False, False]
        self._scored = False
        if not keep_scores:
            self._wins = [0, 0]

    def _reset_to_lobby(self) -> None:
        self._state = "lobby"
        self._game = engine.Game()
        self._game_log_len = 0
        self._game.starter = self._match_seq % 2
        self._rematch = [False, False]
        self._scored = False
        self._wins = [0, 0]

    def _score_winner(self) -> None:
        if self._scored:
            return
        if self._game.winner in (0, 1):
            self._wins[self._game.winner] += 1
            self._scored = True

    def _on_conn_closed(self, conn: _Conn) -> None:
        """Handle disconnect/leave for one connection (idempotent)."""
        with self._lock:
            slot = conn.slot
            if slot is None:
                return
            conn.slot = None
            player = self._slots[slot]
            if player is None or player.conn is not conn:
                return
            name = player.name
            self._append_log({"ts": _now_ms(), "kind": "leave", "name": name})
            notice = None
            if self._state == "lobby":
                self._slots[slot] = None
                notice = "opponent_left"
            elif self._state == "placing":
                self._slots[slot] = None
                self._reset_to_lobby()
                notice = "opponent_left"
            elif self._state == "battle":
                player.conn = None
                player.connected = False
                self._game.forfeit(slot)
                self._drain_game_log()
                self._state = "over"
                self._score_winner()
                notice = "opponent_left"
            elif self._state == "over":
                self._slots[slot] = None
                if self._slots[0] is None and self._slots[1] is None:
                    self._reset_to_lobby()
            self._broadcast(notice)
            if self._verbose:
                print(f"[battleship] disconnected {name!r} (slot {slot})")

    # -- snapshots ---------------------------------------------------------- #
    def _snapshot(self, slot: int, notice: str | None) -> dict:
        view = self._game.view_for(slot)
        players = []
        for index, player in enumerate(self._slots):
            if player is None:
                players.append(None)
            else:
                players.append(
                    {
                        "name": player.name,
                        "ready": self._game.boards[index].placed,
                        "connected": player.connected,
                    }
                )
        hits = [len(self._game.boards[1 - index].hit_cells()) for index in (0, 1)]
        turn = self._game.turn if self._game.turn is not None else self._game.starter
        return {
            "t": "sync",
            "state": self._state,
            "you": slot,
            "code": self.code,
            "players": players,
            "wins": list(self._wins),
            "starter": self._game.starter,
            "turn": turn,
            "your_board": view["your_board"],
            "enemy": view["enemy"],
            "stats": {"shots": list(self._game.shots_fired), "hits": hits},
            "log": list(self._log[-MAX_LOG:]),
            "chat": list(self._chat[-MAX_CHAT:]),
            "winner": self._game.winner,
            "forfeit": self._game.forfeited,
            "rematch": list(self._rematch),
            "notice": notice,
        }

    def _broadcast(self, notice: str | None = None) -> None:
        """Push a fresh snapshot to both players (caller holds the room lock)."""
        self._notice = notice
        sends: list[tuple[_Conn, dict]] = []
        for slot, player in enumerate(self._slots):
            if player is not None and player.conn is not None and player.connected:
                sends.append((player.conn, self._snapshot(slot, notice)))
        self._notice = None
        for conn, payload in sends:
            conn.send(payload)

    def _drain_game_log(self) -> None:
        entries = self._game.log[self._game_log_len:]
        self._game_log_len = len(self._game.log)
        for entry in entries:
            self._append_log(entry)

    def _append_log(self, entry: dict) -> None:
        self._log.append(entry)
        if len(self._log) > MAX_LOG:
            del self._log[:-MAX_LOG]

    def _error(self, conn: _Conn, code: str, message: str = "") -> None:
        conn.send({"t": "error", "code": code, "message": message or code})

    # -- UDP discovery ------------------------------------------------------ #
    def _start_discovery(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", DISCOVERY_PORT))
            sock.settimeout(0.5)
        except OSError as exc:
            self.discovery_active = False
            if self._verbose:
                print(f"[battleship] discovery disabled ({exc})")
            return
        self._discovery_sock = sock
        self.discovery_active = True
        self._discovery_thread = threading.Thread(
            target=self._discovery_loop, args=(sock,), name="battleship-discovery", daemon=True
        )
        self._discovery_thread.start()

    def _discovery_loop(self, sock: socket.socket) -> None:
        while self.running and self._discovery_sock is sock:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if not isinstance(msg, dict) or msg.get("t") != "battleship.discover":
                continue
            with self._lock:
                names = [
                    player.name
                    for player in self._slots
                    if player is not None and player.connected
                ]
                payload = {
                    "t": "battleship.room",
                    "code": self.code,
                    "port": self.port,
                    "host": self.lan_ip,
                    "names": names,
                    "state": self._state,
                    "ver": 1,
                }
            try:
                sock.sendto(json.dumps(payload, ensure_ascii=False).encode("utf-8"), addr)
            except OSError:
                break


if __name__ == "__main__":  # pragma: no cover - manual smoke helper
    import sys

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    server = GameServer(tcp_port=port, discovery=True, verbose=True)
    code, tcp_port = server.start()
    print(f"room {code} listening on {server.lan_ip or '0.0.0.0'}:{tcp_port} — Ctrl-C to stop")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
