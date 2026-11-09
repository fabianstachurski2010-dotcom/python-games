#!/usr/bin/env python3
"""Authoritative LAN server for Connect Four -- TCP JSON-lines + UDP discovery.

One host process runs a single :class:`GameServer`.  It owns the rules engine,
validates every intent, and pushes a **full state snapshot** (``sync``) to every
connected client after each accepted change.  Clients (players *and*
spectators) are thin: they send intents and render the newest snapshot.

Features
--------
* two human seats (red moves first), any number of spectators
* second seat may be filled by the AI (easy / normal / hard)
* chat, scoreboard that survives rematches, rematch voting
* every finished game is written to ``records/`` and shipped to clients so each
  machine keeps its own copy for the replay screen

Usage
-----
    with GameServer(discovery=True) as server:
        print(server.code, server.port, server.lan_ip)
        server.wait()            # or run a pygame UI in this thread

    server = GameServer(tcp_port=0, discovery=False)
    code, port = server.start()
    ...
    server.stop()
"""

from __future__ import annotations

import json
import os
import random
import re
import socket
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import engine
from net_client import (
    DISCOVERY_PORT,
    DISCOVERY_PROBE,
    DISCOVERY_REPLY,
    PROTOCOL_VERSION,
    local_ip,
)

MAX_LINE = 64 * 1024
MAX_NAME = 16
MAX_CHAT = 200
CHAT_HISTORY = 80

PHASE_LOBBY = "lobby"
PHASE_PLAYING = "playing"
PHASE_OVER = "over"

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clean_text(raw: object, limit: int) -> str:
    text = "".join(ch for ch in str(raw or "") if ch >= " " or ch == "\t")
    return " ".join(text.split())[:limit]


def _clean_name(raw: object, fallback: str) -> str:
    return _clean_text(raw, MAX_NAME) or fallback


# --------------------------------------------------------------------------- #
# Connected client
# --------------------------------------------------------------------------- #

class _Client:
    __slots__ = ("id", "sock", "addr", "file", "name", "role", "seat", "closed", "_send_lock")

    def __init__(self, cid: int, sock: socket.socket, addr) -> None:
        self.id = cid
        self.sock = sock
        self.addr = addr
        self.file = sock.makefile("rb")
        self.name = f"Player {cid}"
        self.role = "guest"  # guest -> player | spec
        self.seat = 0        # 0 = spectator
        self.closed = False
        self._send_lock = threading.Lock()

    def send(self, msg: dict) -> bool:
        if self.closed:
            return False
        data = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            with self._send_lock:
                self.sock.sendall(data)
            return True
        except OSError:
            self.closed = True
            return False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        for closer in (self.file, self.sock):
            try:
                closer.close()
            except OSError:
                pass


class _AI:
    """Seat holder for a computer opponent."""
    __slots__ = ("level",)

    def __init__(self, level: str) -> None:
        self.level = level

    @property
    def name(self) -> str:
        return f"AI ({self.level})"


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #

class GameServer:
    def __init__(
        self,
        tcp_port: int = 0,
        discovery: bool = True,
        room_code: Optional[str] = None,
        records_dir: Optional[os.PathLike | str] = None,
        host_name: str = "Host",
        log=print,
    ) -> None:
        self.tcp_port = int(tcp_port)
        self.discovery = bool(discovery)
        self.room_code = room_code or f"{random.randint(0, 9999):04d}"
        self.records_dir = Path(records_dir) if records_dir else Path(__file__).resolve().parent / "records"
        self.host_name = host_name
        self.lan_ip = local_ip()
        self._log = log

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._clients: Dict[int, _Client] = {}
        self._next_id = 1
        self._accept_thread: Optional[threading.Thread] = None
        self._discovery_thread: Optional[threading.Thread] = None
        self._listen: Optional[socket.socket] = None
        self._udp: Optional[socket.socket] = None
        self._code_socket = None

        # game state
        self.game = engine.Game()
        self.phase = PHASE_LOBBY
        self.seats: Dict[int, object] = {engine.RED: None, engine.YELLOW: None}
        self.scores = {engine.RED: 0, engine.YELLOW: 0}
        self.rematch: set = set()
        self.chat: deque = deque(maxlen=CHAT_HISTORY)
        self.record: Optional[dict] = None
        self.record_path: Optional[str] = None
        self.started_iso: Optional[str] = None
        self.seq = 0
        self._gen = 0          # bumped whenever a game starts/ends
        self._ai_thinking = False
        self.discovery_port = DISCOVERY_PORT
        self.discovery_ok = False

    # -- lifecycle --------------------------------------------------------- #

    def __enter__(self) -> "GameServer":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    @property
    def port(self) -> int:
        return self._listen.getsockname()[1] if self._listen else 0

    @property
    def host_ip(self) -> str:
        return self.lan_ip

    def start(self) -> tuple:
        listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listen.bind(("", self.tcp_port))
        listen.listen(16)
        listen.settimeout(0.25)
        self._listen = listen

        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True, name="c4-accept")
        self._accept_thread.start()

        if self.discovery:
            self._start_discovery()

        self._log(f"[server] room {self.room_code} listening on {self.lan_ip}:{self.port}")
        return self.room_code, self.port

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            for client in list(self._clients.values()):
                client.close()
            self._clients.clear()
        for sock in (self._listen, self._udp):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        self._listen = None
        self._udp = None
        self._log("[server] stopped")

    def wait(self) -> None:
        """Block forever (Ctrl+C friendly) — useful when running headless."""
        try:
            while not self._stop.is_set():
                time.sleep(0.25)
        except KeyboardInterrupt:
            self.stop()

    # -- discovery --------------------------------------------------------- #

    def _start_discovery(self) -> None:
        try:
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            udp.bind(("", self.discovery_port))
            udp.settimeout(0.3)
            self._udp = udp
        except OSError as exc:
            self.discovery_ok = False
            self._log(f"[server] LAN discovery unavailable ({exc}); use the IP manually")
            return
        self.discovery_ok = True
        self._discovery_thread = threading.Thread(target=self._discovery_loop, daemon=True, name="c4-udp")
        self._discovery_thread.start()

    def _discovery_loop(self) -> None:
        assert self._udp is not None
        while not self._stop.is_set():
            try:
                data, addr = self._udp.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                msg = json.loads(data.decode("utf-8", "replace").strip())
            except ValueError:
                continue
            if not isinstance(msg, dict) or msg.get("t") != DISCOVERY_PROBE.get("t"):
                continue
            if int(msg.get("ver") or 0) != PROTOCOL_VERSION:
                continue
            with self._lock:
                players = sum(1 for s in self.seats.values() if s is not None and not isinstance(s, _AI))
                reply = {
                    "t": DISCOVERY_REPLY,
                    "ver": PROTOCOL_VERSION,
                    "game": "connect4",
                    "code": self.room_code,
                    "port": self.port,
                    "host": self.host_name,
                    "players": players,
                    "phase": self.phase,
                }
            try:
                self._udp.sendto(
                    json.dumps(reply, ensure_ascii=False).encode("utf-8"), addr
                )
            except OSError:
                pass

    # -- accept / read loops ----------------------------------------------- #

    def _accept_loop(self) -> None:
        assert self._listen is not None
        while not self._stop.is_set():
            try:
                sock, addr = self._listen.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._lock:
                client = _Client(self._next_id, sock, addr)
                self._next_id += 1
                self._clients[client.id] = client
            threading.Thread(
                target=self._serve, args=(client,), daemon=True, name=f"c4-client-{client.id}"
            ).start()

    def _serve(self, client: _Client) -> None:
        self._log(f"[server] {client.addr[0]} connected (#{client.id})")
        try:
            while not self._stop.is_set() and not client.closed:
                line = client.file.readline()
                if not line:
                    break
                if len(line) > MAX_LINE:
                    break
                try:
                    msg = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    self._error(client, "bad_message", "malformed JSON line")
                    continue
                if isinstance(msg, dict):
                    self._handle(client, msg)
        except OSError:
            pass
        finally:
            self._disconnect(client)

    # -- intent dispatch --------------------------------------------------- #

    def _handle(self, client: _Client, msg: dict) -> None:
        kind = str(msg.get("t", ""))
        try:
            if kind == "hello":
                self._on_hello(client, msg)
            elif kind == "ping":
                client.send({"t": "pong", "ts": msg.get("ts", _now_ms())})
            elif kind == "move":
                self._on_move(client, msg)
            elif kind == "chat":
                self._on_chat(client, msg)
            elif kind == "rematch":
                self._on_rematch(client)
            elif kind == "ai":
                self._on_ai(client, msg)
            elif kind == "leave":
                client.close()
            else:
                self._error(client, "bad_message", f"unknown message type {kind!r}")
        except (KeyError, TypeError, ValueError) as exc:
            self._error(client, "bad_message", str(exc))

    def _error(self, client: _Client, code: str, message: str = "") -> None:
        client.send({"t": "error", "code": code, "msg": message or code})

    # -- hello / seats ----------------------------------------------------- #

    def _on_hello(self, client: _Client, msg: dict) -> None:
        role = "spec" if str(msg.get("role", "player")) == "spec" else "player"
        name = _clean_name(msg.get("name"), f"Player {client.id}")
        with self._lock:
            if client.role != "guest":  # already seated: just rename
                client.name = name
                self._broadcast_sync()
                return
            client.name = name
            seat = 0
            if role == "player":
                seat = self._take_seat(client)
            client.role = "player" if seat else "spec"
            client.seat = seat
            client.send(
                {
                    "t": "welcome",
                    "id": client.id,
                    "name": name,
                    "room": self.room_code,
                    "role": client.role,
                    "seat": seat,
                    "ver": PROTOCOL_VERSION,
                    "why": "" if seat or role == "spec" else "room_full",
                }
            )
            self._maybe_start()
            self._broadcast_sync()
        self._log(f"[server] #{client.id} {name} joined as {client.role} seat={client.seat}")

    def _take_seat(self, client: _Client) -> int:
        """Seat 1 (red) first, then seat 2 (yellow).  Evicts an idle AI."""
        for seat in (engine.RED, engine.YELLOW):
            who = self.seats[seat]
            if who is None:
                self.seats[seat] = client
                return seat
            if isinstance(who, _AI) and self.phase != PHASE_PLAYING:
                self.seats[seat] = client
                self.scores = {engine.RED: 0, engine.YELLOW: 0}
                self._chat_system(
                    f"{client.name} replaces the AI. New match, scores reset.",
                    "ai_replaced",
                    name=client.name,
                )
                return seat
        return 0

    def _seat_info(self, seat: int) -> Optional[dict]:
        who = self.seats.get(seat)
        if who is None:
            return None
        if isinstance(who, _AI):
            return {"name": who.name, "ai": True, "level": who.level, "connected": True}
        return {
            "name": who.name,
            "ai": False,
            "level": None,
            "connected": not who.closed,
        }

    # -- game control ------------------------------------------------------ #

    def _maybe_start(self) -> None:
        # Only a full lobby starts a game: a spectator joining after the final
        # disc must never quietly discard the finished result.
        if self.phase != PHASE_LOBBY:
            return
        if all(self.seats[s] is not None for s in (engine.RED, engine.YELLOW)):
            self._start_game()

    def _start_game(self) -> None:
        self.game = engine.Game()
        self.phase = PHASE_PLAYING
        self.rematch.clear()
        self.record = None
        self.record_path = None
        self.started_iso = _iso()
        self.seq += 1
        self._gen += 1
        self._chat_system("New game — red starts.", "new_game")
        self._log("[server] game started")

    def _finish_game(self) -> None:
        self.phase = PHASE_OVER
        self.seq += 1
        self._gen += 1
        names = {}
        for seat in (engine.RED, engine.YELLOW):
            info = self._seat_info(seat)
            names[str(seat)] = info["name"] if info else ("Red" if seat == engine.RED else "Yellow")
        winner = self.game.winner
        if winner:
            self.scores[winner] += 1
        ai_info = None
        for seat in (engine.RED, engine.YELLOW):
            if isinstance(self.seats.get(seat), _AI):
                ai_info = {"seat": seat, "level": self.seats[seat].level}
        record = engine.make_record(
            names=names,
            moves=self.game.moves,
            winner=winner,
            draw=self.game.draw,
            started=self.started_iso or _iso(),
            ended=_iso(),
            ai=ai_info,
        )
        self.record = record
        self.record_path = self._save_record(record)
        if self.game.draw:
            self._chat_system("Draw.", "draw")
        else:
            self._chat_system(f"{names[str(winner)]} wins!", "result", name=names[str(winner)])
        self._log(
            f"[server] game over: winner={winner} draw={self.game.draw} record={self.record_path}"
        )

    def _save_record(self, record: dict) -> Optional[str]:
        try:
            self.records_dir.mkdir(parents=True, exist_ok=True)
            red = _SAFE.sub("_", record["players"]["1"]["name"]).strip("_")[:20] or "red"
            yellow = _SAFE.sub("_", record["players"]["2"]["name"]).strip("_")[:20] or "yellow"
            path = self.records_dir / f"c4_{_stamp()}_{red}_vs_{yellow}.json"
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(path)
            return str(path)
        except OSError as exc:
            self._log(f"[server] could not save record: {exc}")
            return None

    # -- moves / chat / rematch / ai -------------------------------------- #

    def _on_move(self, client: _Client, msg: dict) -> None:
        with self._lock:
            if client.seat not in (engine.RED, engine.YELLOW):
                self._error(client, "spectator", "spectators cannot move")
                return
            if self.phase != PHASE_PLAYING:
                self._error(client, "not_playing", "no game in progress")
                return
            if isinstance(self.seats.get(client.seat), _AI):
                self._error(client, "spectator", "that seat is the AI")
                return
            if client.seat != self.game.turn:
                self._error(client, "not_your_turn", "it is not your turn")
                return
            try:
                col = int(msg.get("col"))
            except (TypeError, ValueError):
                self._error(client, "bad_move", "col must be an integer")
                return
            try:
                self.game.drop(col)
            except engine.MoveError as exc:
                self._error(client, exc.code, str(exc))
                return
            if self.game.over:
                self._finish_game()
            if self.phase == PHASE_PLAYING:
                self._schedule_ai()
            self._broadcast_sync()

    def _on_chat(self, client: _Client, msg: dict) -> None:
        text = _clean_text(msg.get("text"), MAX_CHAT)
        if not text:
            return
        with self._lock:
            self.chat.append(
                {
                    "seat": client.seat,
                    "name": client.name,
                    "text": text,
                    "ts": _now_ms(),
                    "system": False,
                }
            )
            self._broadcast_sync()

    def _on_rematch(self, client: _Client) -> None:
        with self._lock:
            if client.seat not in (engine.RED, engine.YELLOW):
                self._error(client, "spectator", "spectators cannot vote")
                return
            if self.phase != PHASE_OVER:
                self._error(client, "not_playing", "the game is still running")
                return
            self.rematch.add(client.seat)
            # The AI seat votes automatically, so the human's vote is enough.
            for seat in (engine.RED, engine.YELLOW):
                if isinstance(self.seats.get(seat), _AI):
                    self.rematch.add(seat)
            needed = {s for s in (engine.RED, engine.YELLOW) if self.seats.get(s) is not None}
            if self.rematch >= needed:
                self._start_game()
                self._schedule_ai()
            self._broadcast_sync()

    def _on_ai(self, client: _Client, msg: dict) -> None:
        level = str(msg.get("level", "normal")).lower()
        if level not in ("easy", "normal", "hard", "learned"):
            level = "normal"
        with self._lock:
            if client.seat != engine.RED:
                self._error(client, "ai_only_host", "only the first player can start an AI game")
                return
            if self.phase == PHASE_PLAYING:
                self._error(client, "not_playing", "a game is already running")
                return
            if self.seats.get(engine.YELLOW) is not None and not isinstance(
                self.seats.get(engine.YELLOW), _AI
            ):
                self._error(client, "seat_taken", "another player is already in the second seat")
                return
            self.seats[engine.YELLOW] = _AI(level)
            self.scores = {engine.RED: 0, engine.YELLOW: 0}
            self._start_game()
            self._schedule_ai()
            self._broadcast_sync()

    def _schedule_ai(self) -> None:
        ai = self.seats.get(self.game.turn)
        if not isinstance(ai, _AI) or self.phase != PHASE_PLAYING:
            return
        if self._ai_thinking:
            return
        self._ai_thinking = True
        gen = self._gen
        cells = [row[:] for row in self.game.cells]
        seat = self.game.turn
        level = ai.level
        threading.Thread(
            target=self._ai_think, args=(gen, cells, seat, level), daemon=True, name="c4-ai"
        ).start()

    def _ai_think(self, gen: int, cells, seat: int, level: str) -> None:
        import ai as ai_module

        started = time.monotonic()
        col = None
        try:
            col = ai_module.Connect4AI(level).choose_move(cells, seat)
        except Exception as exc:  # pragma: no cover - defensive
            self._log(f"[server] AI error: {exc}")
        # A short pause makes the computer feel deliberate instead of instant.
        wait = 0.45 - (time.monotonic() - started)
        if wait > 0:
            time.sleep(wait)
        with self._lock:
            self._ai_thinking = False
            if gen != self._gen or self.phase != PHASE_PLAYING or self.game.turn != seat:
                return
            if col is None or not self.game.can_play(col):
                legal = self.game.legal_moves()
                if not legal:
                    return
                col = random.choice(legal)
            self.game.drop(col)
            if self.game.over:
                self._finish_game()
            if self.phase == PHASE_PLAYING:
                self._schedule_ai()
            self._broadcast_sync()

    # -- sync -------------------------------------------------------------- #

    def _you(self, client: _Client) -> dict:
        return {
            "id": client.id,
            "name": client.name,
            "role": client.role,
            "seat": client.seat if client.role == "player" else 0,
        }

    def _sync_base(self) -> dict:
        spectators = sum(1 for c in self._clients.values() if c.role == "spec" and not c.closed)
        seats = {str(s): self._seat_info(s) for s in (engine.RED, engine.YELLOW)}
        ai_info = None
        for seat in (engine.RED, engine.YELLOW):
            info = seats[str(seat)]
            if info and info["ai"]:
                ai_info = {"seat": seat, "level": info["level"], "name": info["name"]}
        return {
            "t": "sync",
            "room": self.room_code,
            "seq": self.seq,
            "proto": PROTOCOL_VERSION,
            "phase": self.phase,
            "board": self.game.board(),
            "moves": len(self.game.moves),
            "turn": self.game.turn,
            "winner": self.game.winner,
            "win_line": self.game.win_line,
            "draw": self.game.draw,
            "status": self.game.status,
            "legal": self.game.legal_moves(),
            "last": self.game.last_move(),
            "seats": seats,
            "ai": ai_info,
            "scores": {"1": self.scores[engine.RED], "2": self.scores[engine.YELLOW]},
            "specs": spectators,
            "chat": list(self.chat),
            "rematch": sorted(self.rematch),
            "record": self.record,
            "record_path": self.record_path,
            "thinking": self._ai_thinking and self.phase == PHASE_PLAYING,
        }

    def _broadcast_sync(self) -> None:
        with self._lock:
            base = self._sync_base()
            payloads = []
            for client in self._clients.values():
                if client.closed or client.role == "guest":
                    continue
                payload = dict(base)
                payload["you"] = self._you(client)
                payloads.append((client, payload))
        for client, payload in payloads:
            client.send(payload)

    def _chat_system(self, text: str, code: str = "", **args) -> None:
        """Append a system line.  ``code`` + ``args`` let the UI localise it."""
        self.chat.append(
            {
                "seat": 0,
                "name": "",
                "text": text,
                "ts": _now_ms(),
                "system": True,
                "code": code,
                "args": args,
            }
        )

    # -- disconnect -------------------------------------------------------- #

    def _disconnect(self, client: _Client) -> None:
        with self._lock:
            if client.closed:
                client.close()
            was_seated = client.seat in (engine.RED, engine.YELLOW) and self.seats.get(client.seat) is client
            self._clients.pop(client.id, None)
            if not was_seated:
                self._broadcast_sync()
                return
            self.seats[client.seat] = None
            self._chat_system(f"{client.name} left the table.", "left", name=client.name)
            if self.phase in (PHASE_PLAYING, PHASE_OVER):
                # Abort the match: the remaining player waits for a new opponent.
                self.game = engine.Game()
                self.phase = PHASE_LOBBY
                self.rematch.clear()
                self.record = None
                self.record_path = None
                self.scores = {engine.RED: 0, engine.YELLOW: 0}
                self._gen += 1
                self.seq += 1
                self._chat_system("Match abandoned — waiting for players.", "abandoned")
            # If the human left an AI game, free the AI seat too.
            if any(isinstance(self.seats.get(s), _AI) for s in (engine.RED, engine.YELLOW)):
                if not any(
                    isinstance(self.seats.get(s), _Client) for s in (engine.RED, engine.YELLOW)
                ):
                    for seat in (engine.RED, engine.YELLOW):
                        if isinstance(self.seats.get(seat), _AI):
                            self.seats[seat] = None
            self.seq += 1
            self._broadcast_sync()
        self._log(f"[server] #{client.id} {client.name} disconnected")
