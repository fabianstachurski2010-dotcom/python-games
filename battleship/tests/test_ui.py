"""UI smoke tests — construct the real Tk app, never open modal dialogs.

The tests skip cleanly on machines where Tk cannot start (headless CI), which
matches the project contract: the UI test must not fail just because there is
no display. One Tk root is shared by the whole module (creating and destroying
many roots makes the macOS window server stall); each test builds a fresh app
on it and tears the widgets down.
"""

from __future__ import annotations

import gc
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import engine  # noqa: E402
import i18n  # noqa: E402
import ui  # noqa: E402


class UiTestBase(unittest.TestCase):
    """Share one Tk root; build and dispose one app per test."""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            import tkinter as tk
        except Exception as exc:  # pragma: no cover - Tk missing entirely
            raise unittest.SkipTest(f"tkinter unavailable: {exc}")
        try:
            cls.root = tk.Tk()
        except Exception as exc:  # pragma: no cover - no display
            raise unittest.SkipTest(f"Tk cannot start: {exc}")
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.root.destroy()
        except Exception:  # noqa: BLE001 - teardown must never fail
            pass
        # Tkinter font objects finalise through Tcl; collect them on the main
        # thread so a later socket thread cannot hit Tcl finalisation.
        gc.collect()

    def setUp(self) -> None:
        self._lang_before = i18n.get_lang()
        i18n.set_lang("en")
        self.app = ui.BattleshipApp(root=self.root, mode="menu", lang="en")
        self.root.update_idletasks()
        self.addCleanup(self._teardown)

    def _teardown(self) -> None:
        app = self.app
        # Stop timers/toast/sockets but keep the shared root alive: repeatedly
        # destroying Tk roots on macOS after a battle screen can wedge Tk.
        app.close(destroy_root=False)
        for child in list(self.root.winfo_children()):
            try:
                child.destroy()
            except Exception:  # noqa: BLE001
                pass
        self.app = None
        i18n.set_lang(self._lang_before)
        # Fonts/frames are Tcl-backed; finalise them here on the main thread
        # instead of leaving them for a worker thread to collect later.
        gc.collect()

    # -- helpers ----------------------------------------------------------- #
    def widgets(self, cls):
        found = []

        def walk(widget):
            for child in widget.winfo_children():
                if isinstance(child, cls):
                    found.append(child)
                walk(child)

        walk(self.root)
        return found


class TestMenuScreen(UiTestBase):
    def test_menu_builds_without_errors(self) -> None:
        self.assertEqual(self.app.screen_name, "menu")
        self.assertIsInstance(self.app.screen, ui.MenuScreen)
        menu = self.app.screen

        self.assertTrue(menu.title_label.winfo_exists())
        self.assertEqual(menu.title_label.cget("text"), i18n.t("app.title"))
        self.assertEqual(len(menu.cards), 3)
        for card in menu.cards:
            self.assertIsInstance(card, ui.MenuCard)
            self.assertTrue(card.winfo_exists())
        self.assertIsNotNone(self.app.lang_button)
        self.assertTrue(self.app.lang_button.winfo_exists())
        self.assertTrue(menu.help_button.winfo_exists())
        self.assertTrue(menu.quit_button.winfo_exists())

    def test_window_contract(self) -> None:
        self.assertEqual(self.root.minsize(), (ui.MIN_W, ui.MIN_H))
        self.assertEqual(self.root.cget("bg"), ui.BG)
        self.assertEqual(self.root.title(), i18n.t("app.title"))

    def test_canvas_widgets_actually_draw(self) -> None:
        # Menu cards paint immediately (no idle callback needed).
        for card in self.app.menu_cards:
            self.assertGreater(len(card.find_all()), 0)

    def test_language_toggle_rerenders_menu(self) -> None:
        self.assertEqual(i18n.get_lang(), "en")
        self.app.toggle_language()
        self.app.root.update_idletasks()
        self.assertEqual(i18n.get_lang(), "zh")
        self.assertEqual(self.app.screen_name, "menu")
        self.assertEqual(self.app.screen.title_label.cget("text"),
                         i18n.t("app.title"))
        self.assertEqual(self.app.screen.title_label.cget("text"), "海戰棋")
        self.assertEqual(self.app.lang_button.text, i18n.language_name("en"))


class TestPlacementAndBattle(UiTestBase):
    def _enter_placement(self) -> ui.PlacementScreen:
        # Going through the real AI entry point keeps app state coherent.
        self.app.start_ai("normal")
        self.root.update_idletasks()
        self.assertEqual(self.app.screen_name, "placement")
        screen = self.app.screen
        self.assertIsInstance(screen, ui.PlacementScreen)
        return screen

    def test_placement_board_and_dock_build(self) -> None:
        screen = self._enter_placement()
        self.assertIsInstance(screen.board, ui.BoardCanvas)
        self.assertEqual(len(screen.chips), 5)
        self.assertEqual(len(self.widgets(ui.BoardCanvas)), 1)
        self.assertTrue(screen.ready_button.winfo_exists())
        self.assertFalse(screen.ready_button.enabled)
        # Board drew its ocean, coordinates and grid.
        self.assertGreater(len(screen.board.find_all()), 10)

    def test_random_fleet_enables_ready_and_shows_ships(self) -> None:
        screen = self._enter_placement()
        screen._random()
        self.root.update_idletasks()
        self.assertEqual(len(self.app.placed), 5)
        self.assertTrue(screen.ready_button.enabled)
        self.assertEqual(len(screen.board.ships), 5)
        # One filled hull cell per deck plus board chrome.
        self.assertGreater(len(screen.board.find_all()),
                           engine.TOTAL_SHIP_CELLS + 20)
        for chip in screen.chips.values():
            self.assertTrue(chip.placed)

    def test_ready_starts_local_battle_screen(self) -> None:
        screen = self._enter_placement()
        screen._random()
        screen._ready()
        self.root.update_idletasks()
        self.assertEqual(self.app.screen_name, "battle")
        battle = self.app.screen
        self.assertIsInstance(battle, ui.BattleScreen)
        self.assertIsInstance(battle.own_board, ui.BoardCanvas)
        self.assertIsInstance(battle.enemy_board, ui.BoardCanvas)
        self.assertIn(battle.banner.cget("text"),
                      (i18n.t("battle.your_turn"), i18n.t("battle.enemy_turn")))
        self.assertEqual(battle.enemy_board.kind, "enemy")
        self.assertEqual(len(battle.fleet_labels), 5)

    def test_placement_view_round_trip_keeps_fleet(self) -> None:
        screen = self._enter_placement()
        screen._random()
        view = self.app.local_view()
        self.assertIsNotNone(view)
        self.assertEqual(view["state"], "placing")
        self.assertEqual(view["players"][1]["name"], self.app.ai_label())
        # A rebuild (e.g. language toggle) must not lose placed ships.
        self.app._rebuild()
        self.root.update_idletasks()
        self.assertEqual(self.app.screen_name, "placement")
        self.assertEqual(len(self.app.placed), 5)

    def test_local_shot_updates_boards(self) -> None:
        screen = self._enter_placement()
        screen._random()
        screen._ready()
        self.root.update_idletasks()
        battle = self.app.screen
        # Local game: player 0 starts, so firing is allowed immediately.
        fired = self.app.fire_at(0, 0)
        self.root.update_idletasks()
        if fired:
            self.assertTrue(battle.enemy_board.shots or battle.is_over())
        self.assertIsNotNone(self.app.local_view())


if __name__ == "__main__":
    unittest.main()
