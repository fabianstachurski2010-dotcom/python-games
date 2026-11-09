#!/usr/bin/env python3
"""The LAN protocol: authoritative server, clients, chat, rematch, discovery."""

from __future__ import annotations

import json
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import warnings

warnings.simplefilter("ignore", ResourceWarning)

import engine  # noqa: E402
import net_client  # noqa: E402
import net_server  # noqa: E402


class Remote:
    """A test client that keeps the newest sync and the rest of the inbox."""

    def __init__(self, port: int, name: str, role: str = "player") -> None:
        self.net = net_client.NetClient("127.0.0.1", port, name, role)
        self.net.start()
        self.inbox: list[dict] = []
        self.sync: dict | None = None
        self.welcome: dict | None = None

    def pump(self) -> None:
        for msg in self.net.poll():
            kind = msg.get("t")
            if kind == "sync":
                self.sync = msg
            elif kind == "welcome":
                self.welcome = msg
            else:
                self.inbox.append(msg)

    def wait(self, kind: str, timeout: float = 4.0, pred=None) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.pump()
            for index, msg in enumerate(self.inbox):
                if msg.get("t") == kind and (pred is None or pred(msg)):
                    return self.inbox.pop(index)
            time.sleep(0.01)
        raise AssertionError(f"timed out waiting for {kind!r} (inbox={self.inbox})")

    def wait_sync(self, pred=None, timeout: float = 4.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.pump()
            if self.sync and (pred is None or pred(self.sync)):
                return self.sync
            time.sleep(0.01)
        raise AssertionError(f"timed out waiting for a sync (last={self.sync})")

    def wait_seat(self, timeout: float = 4.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.pump()
            if self.welcome:
                return self.welcome
            time.sleep(0.01)
        raise AssertionError("no welcome message")

    def send(self, **payload) -> None:
        self.net.send(payload)

    def close(self) -> None:
        self.net.close()


class ServerTestCase(unittest.TestCase):
    discovery = False

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.server = net_server.GameServer(
            tcp_port=0,
            discovery=self.discovery,
            records_dir=self.tmp.name,
            host_name="Host",
            log=lambda *a, **k: None,
        )
        self.code, self.port = self.server.start()
        self.remotes: list[Remote] = []

    def tearDown(self) -> None:
        for remote in self.remotes:
            remote.close()
        self.server.stop()
        self.tmp.cleanup()

    def remote(self, name: str, role: str = "player") -> Remote:
        remote = Remote(self.port, name, role)
        self.remotes.append(remote)
        return remote

    def seat_two(self):
        red = self.remote("Kyle")
        self.assertEqual(red.wait_seat()["seat"], engine.RED)
        yellow = self.remote("Amy")
        self.assertEqual(yellow.wait_seat()["seat"], engine.YELLOW)
        red.wait_sync(lambda s: s["phase"] == "playing")
        yellow.wait_sync(lambda s: s["phase"] == "playing")
        return red, yellow

    def move(self, client: Remote, col: int) -> dict:
        before = int((client.sync or {}).get("moves") or 0)
        client.send(t="move", col=col)
        return client.wait_sync(lambda s: int(s["moves"]) > before)

    def play(self, red: "Remote", yellow: "Remote", cols) -> dict:
        """Play a fixed column list, waiting for every seat to catch up."""
        seats = {engine.RED: red, engine.YELLOW: yellow}
        players = [red, yellow]
        for index, col in enumerate(cols):
            seat = engine.RED if index % 2 == 0 else engine.YELLOW
            seats[seat].send(t="move", col=col)
            expected = index + 1
            for client in players:
                client.wait_sync(lambda s, n=expected: int(s["moves"]) == n)
        return red.sync


class LobbyTests(ServerTestCase):
    def test_welcome_and_seating(self):
        self.assertTrue(self.code)
        red = self.remote("Kyle")
        welcome = red.wait_seat()
        self.assertEqual(welcome["seat"], engine.RED)
        self.assertEqual(welcome["role"], "player")
        self.assertEqual(welcome["room"], self.code)

        yellow = self.remote("Amy")
        self.assertEqual(yellow.wait_seat()["seat"], engine.YELLOW)

        guest = self.remote("Watcher", role="spec")
        spec = guest.wait_seat()
        self.assertEqual(spec["role"], "spec")
        self.assertEqual(spec["seat"], 0)

    def test_third_player_is_downgraded_to_spectator(self):
        self.seat_two()
        third = self.remote("Late")
        welcome = third.wait_seat()
        self.assertIn(welcome["role"], ("spec", "player"))
        if welcome["role"] == "player":  # a seat opened up? shouldn't happen here
            self.fail("third player should not get a seat")

    def test_sync_contains_the_public_state(self):
        red = self.remote("Kyle")
        snap = red.wait_sync()
        for key in ("board", "moves", "turn", "phase", "seats", "scores", "chat", "legal", "you"):
            self.assertIn(key, snap)
        self.assertEqual(snap["phase"], "lobby")
        self.assertEqual(snap["turn"], engine.RED)
        self.assertEqual(len(snap["board"]), engine.ROWS)
        self.assertEqual(snap["you"]["seat"], engine.RED)


class PlayTests(ServerTestCase):
    def test_first_move_starts_the_board(self):
        red, yellow = self.seat_two()
        snap = self.move(red, 3)
        self.assertEqual(snap["moves"], 1)
        self.assertEqual(snap["last"]["seat"], engine.RED)
        self.assertEqual(snap["turn"], engine.YELLOW)
        self.assertEqual(snap["board"][engine.ROWS - 1][3], engine.RED)

    def test_wrong_turn_is_rejected(self):
        red, yellow = self.seat_two()
        yellow.send(t="move", col=3)
        error = yellow.wait("error")
        self.assertEqual(error["code"], "not_your_turn")

    def test_spectator_cannot_move(self):
        red, yellow = self.seat_two()
        watcher = self.remote("Watcher", role="spec")
        watcher.wait_seat()
        watcher.wait_sync()
        watcher.send(t="move", col=0)
        self.assertEqual(watcher.wait("error")["code"], "spectator")

    def test_illegal_column_is_rejected(self):
        red, _ = self.seat_two()
        red.send(t="move", col=99)
        self.assertIn(red.wait("error")["code"], ("bad_move", "bad_column"))

    def test_full_game_records_a_replay(self):
        red, yellow = self.seat_two()
        # RED wins vertically in column 3.
        self.play(red, yellow, [3, 0, 3, 1, 3, 2, 3])
        snap = red.wait_sync(lambda s: s["phase"] == "over")
        self.assertEqual(snap["winner"], engine.RED)
        self.assertEqual(len(snap["win_line"]), engine.WIN)
        self.assertEqual(snap["scores"], {"1": 1, "2": 0})
        self.assertTrue(snap["record_path"])

        files = list(Path(self.tmp.name).glob("*.json"))
        self.assertEqual(len(files), 1)
        record = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(record["game"], "connect4")
        self.assertEqual(record["winner"], engine.RED)
        self.assertEqual(record["columns"], [3, 0, 3, 1, 3, 2, 3])
        self.assertEqual(record["players"]["1"]["name"], "Kyle")
        self.assertEqual(record["move_count"], 7)
        self.assertEqual(len(engine.replay_frames(record["columns"])), 7)

    def test_chat_is_broadcast_with_seats(self):
        red, yellow = self.seat_two()
        watcher = self.remote("Watcher", role="spec")
        watcher.wait_seat()
        red.send(t="chat", text="good luck")
        snap = yellow.wait_sync(lambda s: any(m.get("text") == "good luck" for m in s["chat"]))
        messages = [m for m in snap["chat"] if m.get("text") == "good luck"]
        self.assertEqual(messages[0]["seat"], engine.RED)
        self.assertEqual(messages[0]["name"], "Kyle")
        spec_snap = watcher.wait_sync(lambda s: any(m.get("text") == "good luck" for m in s["chat"]))
        self.assertTrue(any(m.get("text") == "good luck" for m in spec_snap["chat"]))

    def test_rematch_needs_both_votes(self):
        red, yellow = self.seat_two()
        self.play(red, yellow, [3, 0, 3, 1, 3, 2, 3])
        red.wait_sync(lambda s: s["phase"] == "over")

        red.send(t="rematch")
        partial = yellow.wait_sync(lambda s: s["phase"] == "over" and s["rematch"])
        self.assertEqual(partial["phase"], "over")

        yellow.send(t="rematch")
        fresh = red.wait_sync(lambda s: s["phase"] == "playing" and s["moves"] == 0)
        self.assertEqual(fresh["scores"], {"1": 1, "2": 0})
        self.assertEqual(fresh["winner"], None)

    def test_leaving_ends_the_match(self):
        red, yellow = self.seat_two()
        self.play(red, yellow, [3])
        yellow.close()
        self.remotes.remove(yellow)
        lobby = red.wait_sync(lambda s: s["phase"] == "lobby")
        self.assertIsNone(lobby["seats"]["2"])
        self.assertIsNotNone(lobby["seats"]["1"])


class AiTests(ServerTestCase):
    def test_host_plays_the_ai_and_can_rematch(self):
        red = self.remote("Host")
        red.wait_seat()
        red.wait_sync(lambda s: s["phase"] == "lobby")
        red.send(t="ai", level="easy")
        snap = red.wait_sync(lambda s: s["phase"] == "playing")
        self.assertEqual(snap["ai"]["seat"], engine.YELLOW)
        self.assertTrue(snap["seats"]["2"]["ai"])
        self.assertFalse(snap["thinking"])

        guard = 0
        while snap["phase"] == "playing" and guard < 30:
            guard += 1
            before = int(snap["moves"])
            legal = snap["legal"]
            col = legal[0] if guard % 2 else legal[len(legal) // 2]
            red.send(t="move", col=col)
            snap = red.wait_sync(
                lambda s: s["phase"] == "over" or int(s["moves"]) >= before + 2,
                timeout=10.0,
            )
        self.assertEqual(snap["phase"], "over", "the AI game never finished")
        self.assertTrue(snap["record_path"])
        self.assertEqual(snap["seats"]["2"]["level"], "easy")

        red.send(t="rematch")
        fresh = red.wait_sync(lambda s: s["phase"] == "playing" and int(s["moves"]) == 0, timeout=6.0)
        self.assertTrue(fresh["seats"]["2"]["ai"])
        self.assertEqual(fresh["scores"]["1"] + fresh["scores"]["2"], 1)

    def test_only_the_red_seat_controls_the_ai(self):
        red, yellow = self.seat_two()
        yellow.send(t="ai", level="hard")
        self.assertEqual(yellow.wait("error")["code"], "ai_only_host")


class DiscoveryTests(ServerTestCase):
    discovery = True

    def test_udp_probe_gets_a_reply(self):
        if not self.server.discovery_ok:
            self.skipTest("UDP discovery unavailable in this environment")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(2.0)
        try:
            probe = json.dumps(net_client.DISCOVERY_PROBE).encode("utf-8") + b"\n"
            sock.sendto(probe, ("127.0.0.1", net_client.DISCOVERY_PORT))
            data, _ = sock.recvfrom(4096)
        finally:
            sock.close()
        reply = json.loads(data.decode("utf-8"))
        self.assertEqual(reply["t"], net_client.DISCOVERY_REPLY)
        self.assertEqual(reply["code"], self.code)
        self.assertEqual(reply["port"], self.port)

    def test_clients_can_discover_the_room(self):
        if not self.server.discovery_ok:
            self.skipTest("UDP discovery unavailable in this environment")
        rooms = net_client.discover_rooms(timeout=2.5)
        codes = {room["code"]: room for room in rooms}
        self.assertIn(self.code, codes)
        self.assertEqual(codes[self.code]["port"], self.port)
        self.assertEqual(codes[self.code]["phase"], "lobby")


if __name__ == "__main__":
    unittest.main(verbosity=2)
