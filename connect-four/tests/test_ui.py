#!/usr/bin/env python3
"""Headless pygame UI: every screen renders, and a real match is played by
clicking the board.  Runs on the SDL dummy video driver, so no window opens."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

warnings.simplefilter("ignore", ResourceWarning)

import pygame  # noqa: E402

import engine  # noqa: E402
import i18n  # noqa: E402
import net_client  # noqa: E402
import net_server  # noqa: E402
import records  # noqa: E402
import ui  # noqa: E402


def colours(surface: pygame.Surface) -> int:
    """Cheap "did anything get drawn" check."""
    seen = set()
    for y in range(0, surface.get_height(), 37):
        for x in range(0, surface.get_width(), 41):
            seen.add(surface.get_at((x, y))[:3])
    return len(seen)


class ScreenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = ui.App(lang="zh", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.app.shutdown()

    def test_every_screen_draws_in_both_languages(self):
        app = self.app
        for lang in ("zh", "en"):
            i18n.set_lang(lang)
            for name, kwargs in (
                ("menu", {"skip_scan": True}),
                ("join", {}),
                ("game", {"mode": "guest"}),
                ("replays", {}),
            ):
                with self.subTest(lang=lang, screen=name):
                    app.current = name
                    app.screens[name].enter(**kwargs)
                    for _ in range(3):
                        app.screens[name].update(1 / 60, (400, 300))
                    app.canvas.fill((0, 0, 0))
                    app.screens[name].draw(app.canvas)
                    self.assertGreater(colours(app.canvas), 12)
            i18n.set_lang("zh")

    def test_game_screen_renders_a_finished_match(self):
        app = self.app
        screen = app.screens["game"]
        app.current = "game"
        screen.enter(mode="guest")
        screen.snap = ui._demo_snapshot([3, 0, 3, 1, 3, 2, 3], ("Kyle", "Amy"))
        screen.snap["phase"] = "over"
        screen.snap["winner"] = engine.RED
        screen.snap["win_line"] = [[2, 3], [3, 3], [4, 3], [5, 3]]
        screen.snap["status"] = engine.STATUS_WIN
        screen.anim = None
        app.canvas.fill((0, 0, 0))
        screen.draw(app.canvas)
        self.assertGreater(colours(app.canvas), 12)
        self.assertEqual(screen._overlay(), "over")
        labels = [button.label for button in screen._overlay_buttons_positioned()]
        self.assertEqual(len(labels), 2)

    def test_replay_screen_steps_through_frames(self):
        app = self.app
        original = records.records_dir
        with tempfile.TemporaryDirectory() as tmp:
            game = engine.Game()
            for col in (3, 0, 4, 1, 5):
                game.drop(col)
            record = engine.make_record(
                names={"1": "Kyle", "2": "Amy"},
                moves=game.moves,
                winner=None,
                draw=False,
                started="2026-10-25T10:00:00",
                ended="2026-10-25T10:01:00",
            )
            records.records_dir = lambda *a, **k: Path(tmp)  # type: ignore[assignment]
            try:
                records.save(record)
                app.current = "replays"
                screen = app.screens["replays"]
                screen.enter()
                self.assertEqual(len(screen.entries), 1)
                self.assertEqual(len(screen.frames), len(game.moves))
                screen.index = 0
                screen.playing = False
                app.canvas.fill((0, 0, 0))
                screen.draw(app.canvas)
                for _ in range(len(screen.frames) + 2):
                    screen.step(1)
                    screen.draw(app.canvas)
                self.assertEqual(screen.index, len(screen.frames))
                screen.step(-1)
                self.assertEqual(screen.index, len(screen.frames) - 1)
                self.assertTrue(screen.current().get("board"))
            finally:
                records.records_dir = original  # type: ignore[assignment]


class JoinTests(unittest.TestCase):
    def test_join_screen_connects_to_a_running_room(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = net_server.GameServer(
                tcp_port=0,
                discovery=False,
                records_dir=tmp,
                host_name="Host",
                log=lambda *a, **k: None,
            )
            code, port = server.start()
            app = ui.App(lang="en", headless=True)
            try:
                app.current = "join"
                screen = app.screens["join"]
                screen.enter(host="127.0.0.1", port=port)
                screen.connect()
                deadline = time.time() + 6.0
                while time.time() < deadline and app.current != "game":
                    app.screens[app.current].update(1 / 60, (-100, -100))
                    time.sleep(0.02)
                self.assertEqual(app.current, "game", getattr(screen, "error", ""))

                game = app.screens["game"]
                deadline = time.time() + 6.0
                while time.time() < deadline and game.snap is None:
                    game._poll()
                    time.sleep(0.02)
                self.assertIsNotNone(game.snap)
                self.assertEqual((game.welcome or {}).get("seat"), engine.RED)
                self.assertEqual(game.room, code)
                self.assertEqual(game.snap["phase"], "lobby")
                game.draw(app.canvas)
            finally:
                app.shutdown()
                server.stop()


class AiUiTests(unittest.TestCase):
    def test_host_versus_ai(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = records.records_dir
            records.records_dir = lambda *a, **k: Path(tmp)  # type: ignore[assignment]
            app = ui.App(lang="en", headless=True)
            try:
                app.current = "game"
                screen = app.screens["game"]
                screen.enter(mode="host", ai_level="easy")
                deadline = time.time() + 8.0
                while time.time() < deadline:
                    screen._poll()
                    if (screen.snap or {}).get("phase") == "playing":
                        break
                    time.sleep(0.02)
                self.assertEqual((screen.snap or {}).get("phase"), "playing")
                self.assertEqual(screen.snap["ai"]["seat"], engine.YELLOW)
                self.assertEqual(screen.me_seat, engine.RED)

                pos = screen.board.center(0, 3)
                screen.handle(pygame.event.Event(pygame.MOUSEBUTTONDOWN, {"pos": pos, "button": 1}))
                deadline = time.time() + 8.0
                while time.time() < deadline:
                    screen._poll()
                    if int((screen.snap or {}).get("moves") or 0) >= 2:
                        break
                    time.sleep(0.02)
                self.assertEqual(screen.snap["moves"], 2)
                self.assertEqual(screen.snap["last"]["seat"], engine.YELLOW)
                self.assertEqual(screen.snap["turn"], engine.RED)
                screen.draw(app.canvas)
            finally:
                app.shutdown()
                records.records_dir = original  # type: ignore[assignment]


class MatchTests(unittest.TestCase):
    """Host through the UI, join as a raw client, and click out a full win."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_dir = records.records_dir
        records.records_dir = lambda *a, **k: Path(self.tmp.name)  # type: ignore[assignment]
        self.app = ui.App(lang="en", headless=True)
        self.app.current = "game"
        self.screen = self.app.screens["game"]
        self.guest = None
        self.guest_sync = None
        self.guest_inbox: list[dict] = []
        self.screen.enter(mode="host")
        self.assertTrue(self._pump(lambda: self.screen.snap is not None), "host never got a sync")
        self.port = self.screen.server.port
        self.guest = net_client.NetClient("127.0.0.1", self.port, "Amy", "player")
        self.guest.start()

    def tearDown(self):
        if self.guest is not None:
            self.guest.close()
        self.screen.leave()
        self.app.shutdown()
        records.records_dir = self._orig_dir  # type: ignore[assignment]
        self.tmp.cleanup()

    # -- helpers ----------------------------------------------------------- #

    def _pump(self, pred, timeout: float = 6.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.screen._poll()
            self.screen.update(1 / 60, (-100, -100))
            self._guest_pump()
            if pred():
                return True
            time.sleep(0.01)
        return False

    def _guest_pump(self) -> None:
        if self.guest is None:
            return
        for msg in self.guest.poll():
            if msg.get("t") == "sync":
                self.guest_sync = msg
            else:
                self.guest_inbox.append(msg)

    def _wait_guest(self, pred, timeout: float = 6.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._guest_pump()
            if self.guest_sync and pred(self.guest_sync):
                return self.guest_sync
            time.sleep(0.01)
        raise AssertionError(f"guest timed out (last sync={self.guest_sync})")

    def _click(self, col: int, row: int = 0) -> None:
        pos = self.screen.board.center(row, col)
        self.screen.handle(pygame.event.Event(pygame.MOUSEBUTTONDOWN, {"pos": pos, "button": 1}))

    def _moves(self) -> int:
        return int((self.screen.snap or {}).get("moves") or 0)

    # -- the match --------------------------------------------------------- #

    def test_click_driven_match(self):
        screen = self.screen
        self.assertTrue(self._pump(lambda: (screen.snap or {}).get("phase") == "playing"))
        self.assertEqual(screen.me_seat, engine.RED)
        self.assertFalse(screen.is_spectator)
        self.assertEqual(self._moves(), 0)

        plan = [3, 0, 3, 1, 3, 2, 3]  # RED wins vertically in column 3
        for index, col in enumerate(plan):
            expected = index + 1
            if index % 2 == 0:
                self._click(col)
                self.assertTrue(
                    self._pump(lambda: self._moves() == expected),
                    f"host click did not register (snap={screen.snap})",
                )
                self.assertIsNotNone(screen.anim)
            else:
                self.guest.send({"t": "move", "col": col})
                self.assertTrue(
                    self._pump(lambda: self._moves() == expected),
                    f"guest move did not reach the UI (snap={screen.snap})",
                )
            screen.draw(self.app.canvas)
            self._wait_guest(lambda snap, n=expected: int(snap["moves"]) == n)

        self.assertTrue(self._pump(lambda: (screen.snap or {}).get("phase") == "over"))
        self.assertEqual(screen.snap["winner"], engine.RED)
        self.assertEqual(len(screen.snap["win_line"]), engine.WIN)
        self.assertEqual(screen.snap["scores"], {"1": 1, "2": 0})
        self.assertEqual(screen._overlay(), "over")

        files = list(Path(self.tmp.name).glob("*.json"))
        self.assertEqual(len(files), 1)
        record = records.load(files[0])
        self.assertIsNotNone(record)
        self.assertEqual(record["columns"], plan)
        self.assertEqual(len(records.frames_for(record)), len(plan))

        # spectators can watch the finished room
        watcher = net_client.NetClient("127.0.0.1", self.port, "Watcher", "spec")
        watcher.start()
        try:
            deadline = time.time() + 6.0
            snap = None
            while time.time() < deadline and snap is None:
                for msg in watcher.poll():
                    if msg.get("t") == "sync":
                        snap = msg
                time.sleep(0.01)
            self.assertIsNotNone(snap)
            self.assertEqual(snap["phase"], "over")
            self.assertEqual(snap["winner"], engine.RED)
        finally:
            watcher.close()

        # out-of-turn clicks change nothing (the fresh game starts with RED anyway)
        self._pump(lambda: False, timeout=0.1)

        # both rematch votes start a clean game and keep the score
        buttons = screen._overlay_buttons_positioned()
        event = pygame.event.Event(
            pygame.MOUSEBUTTONUP,
            {"pos": buttons[0].rect.center, "button": 1},
        )
        screen.handle(event)
        self.guest.send({"t": "rematch"})
        self.assertTrue(
            self._pump(lambda: (screen.snap or {}).get("phase") == "playing" and self._moves() == 0),
            "rematch did not start",
        )
        self.assertEqual(screen.snap["scores"], {"1": 1, "2": 0})
        self.assertIsNone(screen.snap["winner"])

    def test_clicking_out_of_turn_is_ignored(self):
        screen = self.screen
        self.assertTrue(self._pump(lambda: (screen.snap or {}).get("phase") == "playing"))
        self._click(0)  # RED opens
        self.assertTrue(self._pump(lambda: self._moves() == 1))
        self.assertEqual(screen.snap["turn"], engine.YELLOW)
        self._click(4)  # clicking on someone else's turn must be ignored
        self._pump(lambda: False, timeout=0.35)
        self.assertEqual(self._moves(), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
