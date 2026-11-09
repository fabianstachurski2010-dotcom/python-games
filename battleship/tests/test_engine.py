#!/usr/bin/env python3
"""Unit tests for the rule engine (engine.py).

Run from the project directory:
    python3 -m unittest tests.test_engine -v
    python3 tests/test_engine.py
"""

from __future__ import annotations

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine  # noqa: E402


# A deterministic, legal fleet: each ship lies in its own row.
FLEET = [
    {"name": "carrier", "cells": [[0, 0], [0, 1], [0, 2], [0, 3], [0, 4]]},
    {"name": "battleship", "cells": [[2, 0], [2, 1], [2, 2], [2, 3]]},
    {"name": "cruiser", "cells": [[4, 0], [4, 1], [4, 2]]},
    {"name": "submarine", "cells": [[6, 0], [6, 1], [6, 2]]},
    {"name": "destroyer", "cells": [[8, 0], [8, 1]]},
]


def _two_player_game(starter: int = 0) -> engine.Game:
    game = engine.Game()
    game.place_fleet(0, FLEET)
    game.place_fleet(1, FLEET)
    game.start_battle(starter)
    return game


class ConstantsTest(unittest.TestCase):
    def test_grid_and_fleet(self):
        self.assertEqual(engine.SIZE, 10)
        self.assertEqual([size for _, size in engine.FLEET], [5, 4, 3, 3, 2])
        self.assertEqual(engine.TOTAL_SHIP_CELLS, 17)
        self.assertEqual(len(engine.FLEET), 5)


class PlacementTest(unittest.TestCase):
    def test_ship_cells_horizontal_and_vertical(self):
        self.assertEqual(
            engine.ship_cells(3, 2, 4, True), ((2, 4), (2, 5), (2, 6))
        )
        self.assertEqual(
            engine.ship_cells(3, 2, 4, False), ((2, 4), (3, 4), (4, 4))
        )

    def test_ship_cells_out_of_bounds(self):
        with self.assertRaises(engine.PlacementError) as ctx:
            engine.ship_cells(4, 0, 8, True)
        self.assertEqual(ctx.exception.code, "out_of_bounds")

    def test_validate_returns_ships(self):
        ships = engine.validate_placement(FLEET)
        self.assertEqual(len(ships), 5)
        self.assertEqual({ship.name for ship in ships}, set(engine.SHIP_NAMES))
        self.assertEqual(sum(ship.size for ship in ships), 17)
        for ship in ships:
            self.assertFalse(ship.sunk)

    def test_accepts_shorthand_and_ship_objects(self):
        shorthand = [
            {"name": "carrier", "r": 0, "c": 0, "horizontal": True},
            {"name": "battleship", "r": 2, "c": 0, "horizontal": True},
            {"name": "cruiser", "r": 4, "c": 0, "horizontal": True},
            {"name": "submarine", "r": 6, "c": 0, "horizontal": True},
            {"name": "destroyer", "r": 8, "c": 0, "horizontal": True},
        ]
        ships = engine.validate_placement(shorthand)
        self.assertEqual(len(ships), 5)
        rebuilt = engine.validate_placement([ship for ship in ships])
        self.assertEqual(len(rebuilt), 5)

    def test_wrong_fleet(self):
        bad = FLEET[:-1]
        with self.assertRaises(engine.PlacementError) as ctx:
            engine.validate_placement(bad)
        self.assertEqual(ctx.exception.code, "wrong_fleet")

    def test_overlap(self):
        bad = [dict(item) for item in FLEET]
        bad[-1] = {"name": "destroyer", "cells": [[0, 0], [0, 1]]}
        with self.assertRaises(engine.PlacementError) as ctx:
            engine.validate_placement(bad)
        self.assertEqual(ctx.exception.code, "overlap")

    def test_not_straight(self):
        bad = [dict(item) for item in FLEET]
        bad[0] = {"name": "carrier", "cells": [[0, 0], [0, 1], [1, 2], [0, 3], [0, 4]]}
        with self.assertRaises(engine.PlacementError) as ctx:
            engine.validate_placement(bad)
        self.assertEqual(ctx.exception.code, "bad_shape")

    def test_wrong_ship_size(self):
        bad = [dict(item) for item in FLEET]
        bad[4] = {"name": "destroyer", "cells": [[8, 0], [8, 1], [8, 2]]}
        with self.assertRaises(engine.PlacementError) as ctx:
            engine.validate_placement(bad)
        self.assertEqual(ctx.exception.code, "bad_size")

    def test_touch_allowed_but_optional(self):
        touching = [
            {"name": "carrier", "cells": [[0, 0], [0, 1], [0, 2], [0, 3], [0, 4]]},
            {"name": "battleship", "cells": [[1, 0], [1, 1], [1, 2], [1, 3]]},
            {"name": "cruiser", "cells": [[2, 0], [2, 1], [2, 2]]},
            {"name": "submarine", "cells": [[3, 0], [3, 1], [3, 2]]},
            {"name": "destroyer", "cells": [[4, 0], [4, 1]]},
        ]
        self.assertEqual(len(engine.validate_placement(touching)), 5)
        with self.assertRaises(engine.PlacementError) as ctx:
            engine.validate_placement(touching, allow_touch=False)
        self.assertEqual(ctx.exception.code, "touch")

    def test_random_fleet_is_legal(self):
        rng = random.Random(1234)
        for _ in range(100):
            fleet = engine.random_fleet(rng)
            self.assertEqual(sum(len(item["cells"]) for item in fleet), 17)
            self.assertEqual(len(engine.validate_placement(fleet)), 5)


class BoardTest(unittest.TestCase):
    def setUp(self):
        self.board = engine.Board()
        self.board.place_fleet(FLEET)

    def test_fire_hit_and_miss(self):
        result = self.board.fire(0, 0)
        self.assertTrue(result["hit"])
        self.assertIsNone(result["sunk"])
        result = self.board.fire(9, 9)
        self.assertFalse(result["hit"])
        self.assertEqual(self.board.shots[(9, 9)], "miss")

    def test_fire_errors(self):
        self.board.fire(0, 0)
        with self.assertRaises(engine.ShotError) as ctx:
            self.board.fire(0, 0)
        self.assertEqual(ctx.exception.code, "already_shot")
        with self.assertRaises(engine.ShotError) as ctx:
            self.board.fire(-1, 0)
        self.assertEqual(ctx.exception.code, "out_of_bounds")

    def test_sink_ship_and_counts(self):
        for r in range(5):
            result = self.board.fire(0, r)
        self.assertEqual(result["sunk"], "carrier")
        self.assertEqual(result["ship_size"], 5)
        self.assertEqual(len(self.board.sunk_ships), 1)
        self.assertEqual(self.board.remaining_cells, 17 - 5)
        self.assertFalse(self.board.all_sunk)

    def test_available_cells(self):
        self.board.fire(0, 0)
        cells = self.board.available_cells()
        self.assertEqual(len(cells), 99)
        self.assertNotIn((0, 0), cells)

    def test_enemy_view_hides_unsunk_ships(self):
        for r in range(5):
            self.board.fire(0, r)
        self.board.fire(5, 5)
        view = self.board.enemy_view()
        self.assertEqual([ship["name"] for ship in view["sunk"]], ["carrier"])
        # Only the sunk ship's cells are revealed; there is no afloat ship data.
        flat = [(r, c) for ship in view["sunk"] for r, c in ship["cells"]]
        self.assertEqual(len(flat), 5)


class GameFlowTest(unittest.TestCase):
    def test_placement_required_before_battle(self):
        game = engine.Game()
        game.place_fleet(0, FLEET)
        with self.assertRaises(engine.PhaseError):
            game.start_battle()
        game.place_fleet(1, FLEET)
        self.assertTrue(game.both_placed())
        game.start_battle(starter=1)
        self.assertEqual(game.phase, "battle")
        self.assertEqual(game.turn, 1)

    def test_hit_grants_extra_shot_miss_passes_turn(self):
        game = _two_player_game(starter=0)
        result = game.fire(0, 0, 0)  # P0 hits P1's carrier
        self.assertTrue(result["hit"])
        self.assertEqual(game.turn, 0)
        self.assertEqual(game.shots_fired[0], 1)
        result = game.fire(0, 9, 9)  # miss -> turn passes
        self.assertFalse(result["hit"])
        self.assertEqual(game.turn, 1)
        with self.assertRaises(engine.ShotError) as ctx:
            game.fire(0, 1, 1)
        self.assertEqual(ctx.exception.code, "not_your_turn")

    def test_full_game_sinks_entire_fleet(self):
        game = _two_player_game(starter=0)
        for _, size in engine.FLEET:
            pass
        for ship in FLEET:
            for r, c in ship["cells"]:
                self.assertEqual(game.phase, "battle")
                game.fire(0, r, c)
        self.assertEqual(game.phase, "over")
        self.assertEqual(game.winner, 0)
        self.assertEqual(game.boards[1].remaining_cells, 0)
        self.assertEqual(game.log[-1]["kind"], "over")

    def test_forfeit(self):
        game = _two_player_game(starter=0)
        entry = game.forfeit(1)
        self.assertEqual(entry["winner"], 0)
        self.assertEqual(game.winner, 0)
        self.assertEqual(game.phase, "over")
        self.assertTrue(game.forfeited)
        self.assertEqual(game.forfeit(0), {})

    def test_view_for(self):
        game = _two_player_game(starter=0)
        game.fire(0, 0, 0)
        view = game.view_for(0)
        self.assertEqual(view["phase"], "battle")
        self.assertEqual(view["your_board"]["ships"][0]["hits"], [])
        self.assertEqual(view["enemy"]["shots"], [[0, 0, "hit"]])
        self.assertEqual(view["enemy"]["sunk"], [])
        other = game.view_for(1)
        self.assertEqual(other["your_board"]["ships"][0]["hits"], [[0, 0]])
        self.assertEqual(other["enemy"]["shots"], [])


class CoordinateTest(unittest.TestCase):
    def test_roundtrip(self):
        for r in range(engine.SIZE):
            for c in range(engine.SIZE):
                self.assertEqual(engine.parse_cell(engine.cell_name(r, c)), (r, c))

    def test_names(self):
        self.assertEqual(engine.cell_name(0, 0), "A1")
        self.assertEqual(engine.cell_name(9, 9), "J10")
        with self.assertRaises(ValueError):
            engine.parse_cell("K1")
        with self.assertRaises(ValueError):
            engine.parse_cell("A0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
