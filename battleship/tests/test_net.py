#!/usr/bin/env python3
"""End-to-end tests for the Battleship LAN stack (net_client + net_server).

Run from the project directory:

    python3 -m unittest tests.test_net -v

Every test starts a real :class:`net_server.GameServer` bound to an ephemeral
port on 127.0.0.1 and talks to it through real :class:`net_client.NetClient`
instances.  All waits have timeouts, so a protocol bug fails the test instead of
hanging the suite.  Discovery is exercised over the real UDP port and is
skipped when the sandbox blocks that socket.
"""

import json
import os
import socket
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(HERE)
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import engine
import net_server
from net_client import NetClient, NetError, discover_rooms

HOST = "127.0.0.1"
WAIT = 3.0

SYNC_KEYS = {
    "t",
    "state",
    "you",
    "code",
    "players",
    "wins",
    "starter",
    "turn",
    "your_board",
    "enemy",
    "stats",
    "log",
    "chat",
    "winner",
    "forfeit",
    "rematch",
    "notice",
}


# --------------------------------------------------------------------------- #
# Small polling helpers (timeout instead of blocking forever)
# --------------------------------------------------------------------------- #

def wait_for(client, predicate, timeout=WAIT, what="message"):
    """Drain *client* until ``predicate(msg)`` is true; fail loudly on timeout."""
    deadline = time.monotonic() + timeout
    while True:
        for msg in client.poll():
            if predicate(msg):
                return msg
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout:.1f}s waiting for {what}")
        time.sleep(0.005)


def wait_sync(client, predicate=None, timeout=WAIT):
    """Wait for the next ``sync`` snapshot (optionally matching *predicate*)."""
    return wait_for(
        client,
        lambda msg: msg.get("t") == "sync" and (predicate is None or predicate(msg)),
        timeout,
        "sync snapshot",
    )


def wait_error(client, timeout=WAIT):
    """Wait for the next server ``error`` (or report a premature close)."""
    msg = wait_for(
        client,
        lambda m: m.get("t") in ("error", "closed"),
        timeout,
        "error message",
    )
    if msg.get("t") != "error":
        raise AssertionError(f"connection closed instead of an error: {msg}")
    return msg


def wait_closed(client, timeout=WAIT):
    return wait_for(client, lambda m: m.get("t") == "closed", timeout, "closed notice")


def wait_until(predicate, timeout=WAIT, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out after {timeout:.1f}s waiting for {what}")


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

class NetworkTests(unittest.TestCase):
    """One server per test; clients are always cleaned up in tearDown."""

    def setUp(self):
        self.server = net_server.GameServer(tcp_port=0, discovery=False)
        self.code, self.port = self.server.start()
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            try:
                client.close()
            except Exception:  # pragma: no cover - cleanup must never fail
                pass
        self.clients.clear()
        self.server.stop()

    # -- helpers ------------------------------------------------------------ #
    def connect(self, name, port=None):
        client = NetClient()
        client.connect(HOST, self.port if port is None else port, name, timeout=5.0)
        self.clients.append(client)
        return client

    def two_players(self, name1="Alice", name2="Bob"):
        c1 = self.connect(name1)
        wait_sync(c1, lambda m: m["state"] == "lobby")
        c2 = self.connect(name2)
        s1 = wait_sync(c1, lambda m: m["state"] == "placing")
        s2 = wait_sync(c2, lambda m: m["state"] == "placing")
        return c1, c2, s1, s2

    def place_both(self, c1, c2):
        fleet1 = engine.random_fleet()
        c1.send_place(fleet1)
        s1 = wait_sync(c1, lambda m: m["state"] == "placing")
        fleet2 = engine.random_fleet()
        c2.send_place(fleet2)
        battle1 = wait_sync(c1, lambda m: m["state"] == "battle")
        battle2 = wait_sync(c2, lambda m: m["state"] == "battle")
        return fleet1, fleet2, battle1, battle2

    def play_out(self, c1, c2, first_sync):
        """Fire at every cell in turn until one fleet is sunk.

        Each action is consumed from *both* clients in lockstep so neither the
        send buffers nor stale snapshots can desynchronise the driver.
        Returns ``(final_sync, successful_shot_count)``.
        """
        peers = [c1, c2]
        coords = [(r, c) for r in range(10) for c in range(10)]
        cursor = [0, 0]
        sync = first_sync
        shots = 0
        for _ in range(400):
            turn = sync["turn"]
            shooter, other = peers[turn], peers[1 - turn]
            if cursor[turn] >= len(coords):  # pragma: no cover - defensive
                self.fail("a player ran out of untried target cells")
            shooter.send_fire(*coords[cursor[turn]])
            cursor[turn] += 1
            shots += 1
            sync = wait_sync(shooter)
            other_sync = wait_sync(other)
            self.assertEqual(sync["state"], other_sync["state"])
            if sync["state"] == "over":
                return sync, shots
            self.assertEqual(sync["turn"], other_sync["turn"])
        self.fail("the game did not finish within 400 shots")

    def fire_ok(self, peers, index, r, c):
        """Fire a legal shot and return the shooter's fresh snapshot."""
        peers[index].send_fire(r, c)
        sync = wait_sync(peers[index])
        other_sync = wait_sync(peers[1 - index])
        self.assertEqual(sync["state"], other_sync["state"])
        return sync

    # -- handshake / schema ------------------------------------------------- #
    def test_handshake_and_lobby_sync(self):
        c1 = self.connect("Alice")
        self.assertTrue(c1.connected)
        self.assertEqual(c1.you, 0)
        self.assertEqual(c1.code, self.code)

        lobby = wait_sync(c1, lambda m: m["state"] == "lobby")
        self.assertEqual(lobby["you"], 0)
        self.assertEqual(lobby["code"], self.code)
        self.assertEqual(lobby["players"][0]["name"], "Alice")
        self.assertIsNone(lobby["players"][1])
        self.assertIsNone(lobby["notice"])

        c2 = self.connect("Bob")
        self.assertEqual(c2.you, 1)
        self.assertEqual(c2.code, self.code)

        s1 = wait_sync(c1, lambda m: m["state"] == "placing")
        s2 = wait_sync(c2, lambda m: m["state"] == "placing")
        for sync in (s1, s2):
            self.assertEqual(sync["state"], "placing")
            self.assertEqual(sync["notice"], "new_opponent")
            self.assertEqual([(p or {}).get("name") for p in sync["players"]], ["Alice", "Bob"])
            self.assertEqual(sync["players"][0]["ready"], False)
            self.assertEqual(sync["players"][1]["ready"], False)
            self.assertEqual(sync["wins"], [0, 0])
            self.assertEqual(sync["starter"], 0)
            self.assertEqual(sync["turn"], 0)
            self.assertIsNone(sync["winner"])
            self.assertFalse(sync["forfeit"])
            self.assertEqual(sync["rematch"], [False, False])
        self.assertEqual(self.server.client_count(), 2)

    def test_sync_schema(self):
        c1, c2, s1, s2 = self.two_players()

        self.assertEqual(set(s1), SYNC_KEYS)
        self.assertEqual(set(s1["your_board"]), {"ships", "shots"})
        self.assertEqual(set(s1["enemy"]), {"shots", "sunk"})
        self.assertNotIn("ships", s1["enemy"], "the enemy snapshot must never leak ships")
        self.assertEqual(set(s1["stats"]), {"shots", "hits"})
        self.assertEqual(len(s1["stats"]["shots"]), 2)
        self.assertEqual(len(s1["stats"]["hits"]), 2)
        self.assertEqual(s1["stats"]["shots"], [0, 0])
        self.assertEqual(s1["stats"]["hits"], [0, 0])
        self.assertEqual(len(s1["players"]), 2)
        self.assertEqual(set(s1["players"][0]), {"name", "ready", "connected"})
        self.assertEqual(s1["players"][0]["connected"], True)
        self.assertEqual(s1["chat"], [])
        self.assertIsInstance(s1["log"], list)

        # After placement our own fleet is visible, the enemy's is not.
        fleet1, fleet2, battle1, battle2 = self.place_both(c1, c2)
        self.assertEqual(battle1["state"], "battle")
        self.assertEqual(battle2["state"], "battle")
        ships = battle1["your_board"]["ships"]
        self.assertEqual(len(ships), 5)
        self.assertEqual(sum(len(ship["cells"]) for ship in ships), engine.TOTAL_SHIP_CELLS)
        self.assertEqual(battle1["players"][0]["ready"], True)
        self.assertEqual(battle1["players"][1]["ready"], True)
        self.assertNotIn("ships", battle1["enemy"])

    # -- placement ---------------------------------------------------------- #
    def test_placement_validation_and_errors(self):
        c1, c2, _s1, _s2 = self.two_players()

        # Firing while still placing is a phase error.
        c1.send_fire(0, 0)
        self.assertEqual(wait_error(c1)["code"], "bad_phase")

        # Fleet with the wrong number of ships.
        c1.send_place(engine.random_fleet()[:4])
        self.assertEqual(wait_error(c1)["code"], "wrong_fleet")

        # Unknown ship name maps onto the generic placement error.
        bad = engine.random_fleet()
        bad[0] = dict(bad[0], name="rowboat")
        c1.send_place(bad)
        self.assertEqual(wait_error(c1)["code"], "bad_placement")

        # Overlapping fleet.
        overlap = engine.random_fleet()
        overlap[4] = {
            "name": "destroyer",
            "cells": [list(overlap[0]["cells"][0]), list(overlap[0]["cells"][1])],
        }
        c1.send_place(overlap)
        self.assertEqual(wait_error(c1)["code"], "overlap")

        # Non-list payload.
        c1.send_place({"nope": True})
        self.assertEqual(wait_error(c1)["code"], "bad_placement")

        # A valid fleet is accepted and marked ready.
        fleet1 = engine.random_fleet()
        c1.send_place(fleet1)
        ready = wait_sync(c1, lambda m: m["state"] == "placing")
        self.assertEqual(ready["players"][0]["ready"], True)
        self.assertEqual(ready["players"][1]["ready"], False)

        # Second fleet starts the battle.
        c2.send_place(engine.random_fleet())
        battle = wait_sync(c1, lambda m: m["state"] == "battle")
        self.assertEqual(battle["state"], "battle")

        # Placing again once the battle started is a phase error.
        c1.send_place(engine.random_fleet())
        self.assertEqual(wait_error(c1)["code"], "bad_phase")

    def test_turn_and_shot_rules(self):
        c1, c2, _s1, _s2 = self.two_players()
        _f1, _f2, battle1, battle2 = self.place_both(c1, c2)
        self.assertEqual(battle1["starter"], battle2["starter"])
        self.assertEqual(battle1["turn"], battle1["starter"])
        sync = battle2
        self.assertEqual(sync["state"], "battle")
        self.assertEqual(sync["turn"], sync["starter"])

        peers = [c1, c2]

        # The player who is not on turn may not fire.
        other = 1 - sync["turn"]
        peers[other].send_fire(0, 0)
        self.assertEqual(wait_error(peers[other])["code"], "not_your_turn")

        # Out-of-bounds coordinates are rejected without mutating the game.
        turn = sync["turn"]
        for row, col in ((0, 10), (-1, 0), (10, 0), (0, -1)):
            peers[turn].send_fire(row, col)
            self.assertEqual(wait_error(peers[turn])["code"], "out_of_bounds")
        self.assertTrue(peers[turn].connected)

        # Fire unique cells until the player to move has already shot, then
        # re-fire one of those cells -> already_shot.  A hit keeps the turn and
        # hands out another shot; a miss passes the turn, so the loop is driven
        # entirely by the authoritative snapshots.
        coords = [(r, c) for r in range(10) for c in range(10)]
        cursor = [0, 0]
        fired = {0: [], 1: []}
        replayed = False
        for _ in range(60):
            current = sync["turn"]
            if fired[current]:
                peers[current].send_fire(*fired[current][0])
                self.assertEqual(wait_error(peers[current])["code"], "already_shot")
                replayed = True
                break
            cell = coords[cursor[current]]
            cursor[current] += 1
            hits_before = sync["stats"]["hits"][current]
            sync = self.fire_ok(peers, current, *cell)
            fired[current].append(cell)
            self.assertEqual(sync["state"], "battle")
            if sync["stats"]["hits"][current] > hits_before:
                self.assertEqual(sync["turn"], current, "a hit keeps the turn")
            else:
                self.assertEqual(sync["turn"], 1 - current, "a miss passes the turn")
        self.assertTrue(replayed, "never reached a second shot by the same player")

        # The connection is still healthy after all those errors.
        self.assertTrue(peers[sync["turn"]].connected)
        self.assertEqual(self.server.client_count(), 2)

    # -- full game ---------------------------------------------------------- #
    def test_full_game_to_completion(self):
        c1, c2, _s1, _s2 = self.two_players()
        _f1, _f2, battle1, battle2 = self.place_both(c1, c2)
        self.assertEqual(battle2["starter"], 0)

        final, shots = self.play_out(c1, c2, battle2)

        self.assertEqual(final["state"], "over")
        self.assertIn(final["winner"], (0, 1))
        self.assertFalse(final["forfeit"])
        winner = final["winner"]
        self.assertEqual(final["wins"][winner], 1)
        self.assertEqual(final["wins"][1 - winner], 0)
        self.assertEqual(sum(final["stats"]["shots"]), shots)
        self.assertTrue(any(entry.get("kind") == "over" for entry in final["log"]))
        self.assertTrue(any(entry.get("kind") == "sunk" for entry in final["log"]))
        # The winner sees all five enemy ships sunk (names + cells, never layouts).
        sunk = final["enemy"]["sunk"]
        self.assertEqual(len(sunk), len(engine.FLEET))
        self.assertEqual({ship["name"] for ship in sunk}, set(engine.FLEET_BY_NAME))
        self.assertEqual(sum(len(ship["cells"]) for ship in sunk), engine.TOTAL_SHIP_CELLS)
        self.assertEqual(self.server.client_count(), 2)

    def test_rematch_alternates_starter_and_keeps_scores(self):
        c1, c2, _s1, _s2 = self.two_players()
        _f1, _f2, battle1, battle2 = self.place_both(c1, c2)
        final, _shots = self.play_out(c1, c2, battle2)
        winner = final["winner"]
        wins_after_first = list(final["wins"])
        self.assertEqual(wins_after_first[winner], 1)

        # A single vote only records intent.
        c1.send_rematch()
        s1 = wait_sync(c1)
        s2 = wait_sync(c2)
        for sync in (s1, s2):
            self.assertEqual(sync["state"], "over")
            self.assertEqual(sync["rematch"], [True, False])

        # The rematch cannot start until both players agree.
        self.assertEqual(s1["starter"], 0)

        c2.send_rematch()
        p1 = wait_sync(c1, lambda m: m["state"] == "placing")
        p2 = wait_sync(c2, lambda m: m["state"] == "placing")
        for sync in (p1, p2):
            self.assertEqual(sync["notice"], "rematch")
            self.assertEqual(sync["wins"], wins_after_first)
            self.assertEqual(sync["starter"], 1)  # starter alternates every match
            self.assertEqual(sync["turn"], 1)
            self.assertEqual(sync["players"][0]["ready"], False)
            self.assertEqual(sync["players"][1]["ready"], False)
            self.assertEqual(sync["rematch"], [False, False])
            self.assertTrue(any(entry.get("kind") == "rematch" for entry in sync["log"]))

        # Rematch votes outside "over" are rejected.
        c1.send_rematch()
        self.assertEqual(wait_error(c1)["code"], "bad_phase")

    # -- chat --------------------------------------------------------------- #
    def test_chat(self):
        c1, c2, _s1, _s2 = self.two_players()

        c1.send_chat("ahoy there")
        s1 = wait_sync(c1)
        s2 = wait_sync(c2)
        for sync in (s1, s2):
            entry = sync["chat"][-1]
            self.assertEqual(entry["from"], "Alice")
            self.assertEqual(entry["text"], "ahoy there")
            self.assertIsInstance(entry["ts"], int)

        # Whitespace-only chat is ignored and does not broadcast.
        c1.send_chat("   ")
        time.sleep(0.15)
        self.assertEqual(c1.poll(), [])
        self.assertEqual(c2.poll(), [])

        # Long chat is trimmed to 200 characters.
        c1.send_chat("x" * 500)
        long_sync = wait_sync(c1)
        self.assertEqual(len(long_sync["chat"][-1]["text"]), 200)
        self.assertEqual(long_sync["chat"][-1]["from"], "Alice")

    # -- disconnect / lobby ------------------------------------------------- #
    def test_forfeit_disconnect_and_lobby(self):
        c1, c2, _s1, _s2 = self.two_players()
        _f1, _f2, battle1, battle2 = self.place_both(c1, c2)
        self.assertEqual(battle2["state"], "battle")

        # Hard-close the second player during battle: the first player wins.
        c2.close()
        over = wait_sync(c1, lambda m: m["state"] == "over")
        self.assertTrue(over["forfeit"])
        self.assertEqual(over["winner"], 0)
        self.assertEqual(over["wins"], [1, 0])
        self.assertEqual(over["players"][1]["connected"], False)
        self.assertEqual(over["notice"], "opponent_left")

        # No opponent left: a rematch is impossible.
        c1.send_rematch()
        self.assertEqual(wait_error(c1)["code"], "bad_phase")

        # But the room can be returned to the lobby.
        c1.send_lobby()
        lobby = wait_sync(c1, lambda m: m["state"] == "lobby")
        self.assertEqual(lobby["wins"], [0, 0])
        self.assertEqual(lobby["players"][0]["name"], "Alice")
        self.assertIsNone(lobby["players"][1])
        self.assertEqual(self.server.client_count(), 1)

        # A new opponent joins -> fresh placing match, wins reset.
        c3 = self.connect("Carol")
        placing = wait_sync(c1, lambda m: m["state"] == "placing")
        self.assertEqual(placing["notice"], "new_opponent")
        self.assertEqual([(p or {}).get("name") for p in placing["players"]], ["Alice", "Carol"])
        self.assertEqual(placing["wins"], [0, 0])

        # A placing disconnect drops the room back to the lobby.
        c3.leave()
        back = wait_sync(c1, lambda m: m["state"] == "lobby")
        self.assertEqual(back["notice"], "opponent_left")
        self.assertEqual(back["wins"], [0, 0])
        self.assertIsNone(back["players"][1])

    def test_room_full_and_name_validation(self):
        c1, c2, _s1, _s2 = self.two_players()

        too_long = NetClient()
        self.clients.append(too_long)
        with self.assertRaises(NetError) as ctx:
            too_long.connect(HOST, self.port, "X" * 17, timeout=5.0)
        self.assertEqual(ctx.exception.code, "name_too_long")

        third = NetClient()
        self.clients.append(third)
        with self.assertRaises(NetError) as ctx:
            third.connect(HOST, self.port, "Eve", timeout=5.0)
        self.assertEqual(ctx.exception.code, "room_full")

        # The rejected connections did not disturb the room.
        self.assertEqual(self.server.client_count(), 2)
        c1.send_chat("still here")
        self.assertEqual(wait_sync(c1)["chat"][-1]["text"], "still here")

    # -- raw protocol / robustness ------------------------------------------ #
    def test_protocol_robustness(self):
        sock = socket.create_connection((HOST, self.port), timeout=WAIT)
        stream = sock.makefile("rwb")
        try:
            def send_raw(payload):
                stream.write(json.dumps(payload).encode("utf-8") + b"\n")
                stream.flush()

            def read_msg():
                line = stream.readline()
                self.assertTrue(line, "the server closed the raw connection")
                return json.loads(line.decode("utf-8"))

            send_raw({"t": "hello", "name": "Raw", "ver": 1})
            self.assertEqual(read_msg()["t"], "welcome")
            self.assertEqual(read_msg()["t"], "sync")  # initial lobby snapshot

            # Malformed JSON.
            stream.write(b"this is not json\n")
            stream.flush()
            err = read_msg()
            self.assertEqual((err["t"], err["code"]), ("error", "bad_message"))

            # A JSON value that is not an object.
            stream.write(b"[1, 2, 3]\n")
            stream.flush()
            err = read_msg()
            self.assertEqual((err["t"], err["code"]), ("error", "bad_message"))

            # Unknown message type.
            send_raw({"t": "nonsense"})
            err = read_msg()
            self.assertEqual((err["t"], err["code"]), ("error", "bad_message"))

            # ... and the connection still works.
            send_raw({"t": "ping"})
            self.assertEqual(read_msg()["t"], "pong")
            send_raw({"t": "leave"})
        finally:
            try:
                stream.close()
            finally:
                sock.close()

    # -- ping / close / lifecycle ------------------------------------------- #
    def test_ping_close_and_server_lifecycle(self):
        client = self.connect("Solo")
        wait_sync(client, lambda m: m["state"] == "lobby")

        client.send_ping()
        wait_for(client, lambda m: m.get("t") == "pong")

        client.leave()
        wait_closed(client)
        self.assertFalse(client.connected)
        with self.assertRaises(NetError) as ctx:
            client.send_ping()
        self.assertEqual(ctx.exception.code, "not_connected")

        wait_until(lambda: self.server.client_count() == 0, what="empty room")

        # stop() is idempotent and the server can be started again.
        self.server.stop()
        self.server.stop()
        self.assertFalse(self.server.running)
        self.code, self.port = self.server.start()
        self.assertTrue(self.server.running)
        again = self.connect("Again")
        wait_sync(again, lambda m: m["state"] == "lobby")

    # -- discovery ---------------------------------------------------------- #
    def test_discovery(self):
        server = net_server.GameServer(tcp_port=0, discovery=True)
        try:
            code, port = server.start()
            if not server.discovery_active:
                self.skipTest("UDP discovery port 54321 is unavailable in this environment")
            client = NetClient()
            self.clients.append(client)
            client.connect(HOST, port, "Alice", timeout=5.0)
            wait_sync(client, lambda m: m["state"] == "lobby")

            rooms = discover_rooms(timeout=1.2)
            matches = [room for room in rooms if room["code"] == code and room["port"] == port]
            self.assertTrue(matches, f"room {code}:{port} was not discovered (got {rooms})")
            self.assertEqual(len(matches), 1, "discovery replies were not de-duplicated")
            room = matches[0]
            self.assertIn("Alice", room["names"])
            self.assertEqual(room["state"], "lobby")
            self.assertTrue(room["host"])
        finally:
            for client in self.clients:
                try:
                    client.close()
                except Exception:  # pragma: no cover
                    pass
            self.clients.clear()
            server.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
