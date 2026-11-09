#!/usr/bin/env python3
"""Unit tests for the Battleship AI (ai.py).

Run from the project directory:
    python3 -m unittest tests.test_ai -v
    python3 tests/test_ai.py

The tests cover legality (no out-of-bounds or repeated shots), deterministic
behaviour for a fixed seed, the fairness rule (identical public knowledge must
produce an identical choice even when the hidden fleet differs), and the
relative strength of the difficulties in seeded self-play tournaments.
"""

from __future__ import annotations

import os
import random
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine  # noqa: E402
from ai import BattleshipAI  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

class _PublicBoard:
    """Board view exposing *only* the public facts ``choose_attack`` may use.

    Passing this proxy to the AI proves it never peeks at the hidden fleet: any
    access to ``board.ships`` (or any other private attribute) would blow up
    with ``AttributeError``.
    """

    def __init__(self, board: engine.Board):
        self._board = board

    @property
    def shots(self) -> dict[tuple[int, int], str]:
        return self._board.shots

    @property
    def sunk_ships(self) -> list:
        return self._board.sunk_ships

    def available_cells(self) -> list[tuple[int, int]]:
        return self._board.available_cells()

    def hit_cells(self) -> list[tuple[int, int]]:
        return self._board.hit_cells()


def _new_board(fleet_seed: int = 0, shots: tuple[tuple[int, int], ...] = ()) -> engine.Board:
    """A Board with a seeded random fleet and a fixed, legal sequence of shots."""
    board = engine.Board()
    board.place_fleet(engine.random_fleet(random.Random(fleet_seed)))
    for cell in shots:
        board.fire(*cell)
    return board


def _new_game(starter: int = 0, fleet_seed: int = 0) -> engine.Game:
    """A Game with two seeded random fleets, ready for battle."""
    game = engine.Game()
    game.place_fleet(0, engine.random_fleet(random.Random(fleet_seed)))
    game.place_fleet(1, engine.random_fleet(random.Random(fleet_seed + 1)))
    game.start_battle(starter)
    return game


def _play_game(
    players: tuple[BattleshipAI, BattleshipAI],
    starter: int = 0,
    fleet_seed: int = 0,
) -> int:
    """Play one full game through the engine and return the winning index."""
    game = _new_game(starter=starter, fleet_seed=fleet_seed)
    while game.phase == "battle":
        index = game.turn
        board = game.boards[1 - index]
        cell = players[index].choose_attack(board)
        game.fire(index, *cell)
    return game.winner


# Hidden fleets for the fairness test: everything except the submarine is
# identical across the variants, and the announced sunk ships are identical too.
# (Touching ships are legal.)
_FAIR_SHARED_SHIPS = [
    {"name": "carrier", "cells": [[2, 2], [2, 3], [2, 4], [2, 5], [2, 6]]},
    {"name": "battleship", "cells": [[4, 2], [4, 3], [4, 4], [4, 5]]},
    {"name": "cruiser", "cells": [[6, 2], [6, 3], [6, 4]]},
    {"name": "destroyer", "cells": [[0, 0], [0, 1]]},
]

_FAIR_SUBMARINES = (
    [[8, 2], [8, 3], [8, 4]],
    [[8, 6], [8, 7], [8, 8]],
    [[1, 9], [2, 9], [3, 9]],
    [[6, 8], [7, 8], [8, 8]],
)

# Shots that resolve identically on every fairness board: the shared destroyer
# and battleship are sunk, the rest are misses on every variant.
_FAIR_SHOTS = (
    (0, 0),
    (0, 1),
    (4, 2),
    (4, 3),
    (4, 4),
    (4, 5),
    (1, 0),
    (3, 0),
    (9, 8),
    (9, 9),
)


def _fair_board(submarine: list[list[int]]) -> engine.Board:
    board = engine.Board()
    board.place_fleet(
        _FAIR_SHARED_SHIPS + [{"name": "submarine", "cells": submarine}]
    )
    for cell in _FAIR_SHOTS:
        board.fire(*cell)
    return board


# --------------------------------------------------------------------------- #
# Contract / placement
# --------------------------------------------------------------------------- #

class ContractTest(unittest.TestCase):
    def test_difficulties(self):
        self.assertEqual(BattleshipAI.DIFFICULTIES, ("easy", "normal", "hard"))
        with self.assertRaises(ValueError):
            BattleshipAI("impossible")

    def test_choose_placement_is_legal_and_reproducible(self):
        for difficulty in BattleshipAI.DIFFICULTIES:
            with self.subTest(difficulty=difficulty):
                placements = BattleshipAI(difficulty, seed=7).choose_placement()
                ships = engine.validate_placement(placements)
                self.assertEqual(len(ships), len(engine.FLEET))
                self.assertEqual(
                    {ship.name for ship in ships}, set(engine.SHIP_NAMES)
                )
                self.assertEqual(
                    sum(len(placement["cells"]) for placement in placements),
                    engine.TOTAL_SHIP_CELLS,
                )
                twin = BattleshipAI(difficulty, seed=7).choose_placement()
                self.assertEqual(placements, twin)


# --------------------------------------------------------------------------- #
# Legality in real games
# --------------------------------------------------------------------------- #

class AttackTest(unittest.TestCase):
    def test_full_games_never_pick_illegal_or_repeated_cells(self):
        for difficulty in BattleshipAI.DIFFICULTIES:
            with self.subTest(difficulty=difficulty):
                ai = BattleshipAI(difficulty, seed=11)
                opponent = BattleshipAI("normal", seed=12)
                game = _new_game(starter=0, fleet_seed=101)
                moves = 0
                while game.phase == "battle":
                    index = game.turn
                    board = game.boards[1 - index]
                    available = set(board.available_cells())
                    player = ai if index == 0 else opponent
                    cell = player.choose_attack(board)
                    self.assertIsInstance(cell, tuple)
                    self.assertEqual(len(cell), 2)
                    self.assertTrue(engine.in_bounds(*cell))
                    self.assertIn(cell, available)
                    self.assertNotIn(cell, board.shots)
                    game.fire(index, *cell)
                    self.assertIn(cell, board.shots)
                    moves += 1
                    self.assertLess(moves, 300, "game did not terminate")
                self.assertEqual(game.phase, "over")
                self.assertIn(game.winner, (0, 1))

    def test_attack_is_deterministic_for_a_fixed_seed(self):
        shots = ((0, 0), (0, 1), (4, 4), (4, 5), (7, 7), (2, 8))
        for difficulty in BattleshipAI.DIFFICULTIES:
            with self.subTest(difficulty=difficulty):
                first = BattleshipAI(difficulty, seed=123)
                second = BattleshipAI(difficulty, seed=123)
                board_a = _new_board(fleet_seed=3, shots=shots)
                board_b = _new_board(fleet_seed=3, shots=shots)
                choice = first.choose_attack(board_a)
                self.assertEqual(choice, second.choose_attack(board_b))
                # The same public state always yields the same answer.
                self.assertEqual(choice, first.choose_attack(board_a))


# --------------------------------------------------------------------------- #
# Fairness: public knowledge only
# --------------------------------------------------------------------------- #

class FairnessTest(unittest.TestCase):
    def test_choices_ignore_hidden_fleet_layout(self):
        boards = [_fair_board(submarine) for submarine in _FAIR_SUBMARINES]
        reference = boards[0]

        # The public state is identical on every board ...
        for board in boards[1:]:
            self.assertEqual(dict(reference.shots), dict(board.shots))
            self.assertEqual(
                [(ship.name, ship.cells) for ship in reference.sunk_ships],
                [(ship.name, ship.cells) for ship in board.sunk_ships],
            )
            self.assertEqual(
                {ship.name for ship in reference.sunk_ships},
                {"destroyer", "battleship"},
            )
            # ... while the still-afloat layouts differ.
            afloat_reference = {
                cell
                for ship in reference.ships
                if ship.afloat
                for cell in ship.cells
            }
            afloat_board = {
                cell for ship in board.ships if ship.afloat for cell in ship.cells
            }
            self.assertNotEqual(afloat_reference, afloat_board)

        for difficulty in BattleshipAI.DIFFICULTIES:
            with self.subTest(difficulty=difficulty):
                proxies = [_PublicBoard(board) for board in boards]
                self.assertFalse(hasattr(proxies[0], "ships"))
                choices = [
                    BattleshipAI(difficulty, seed=99).choose_attack(proxy)
                    for proxy in proxies
                ]
                self.assertEqual(len(set(choices)), 1)
                self.assertIn(choices[0], set(reference.available_cells()))


# --------------------------------------------------------------------------- #
# Exhaustion edge cases
# --------------------------------------------------------------------------- #

class ExhaustionTest(unittest.TestCase):
    def test_no_available_cell_raises_no_moves(self):
        board = engine.Board()
        board.shots = {
            (r, c): "miss"
            for r in range(engine.SIZE)
            for c in range(engine.SIZE)
        }
        for difficulty in BattleshipAI.DIFFICULTIES:
            with self.subTest(difficulty=difficulty):
                with self.assertRaises(engine.ShotError) as ctx:
                    BattleshipAI(difficulty, seed=1).choose_attack(board)
                self.assertEqual(ctx.exception.code, "no_moves")

    def test_single_remaining_cell_is_returned(self):
        board = engine.Board()
        board.shots = {
            (r, c): "miss"
            for r in range(engine.SIZE)
            for c in range(engine.SIZE)
            if (r, c) != (5, 5)
        }
        for difficulty in BattleshipAI.DIFFICULTIES:
            with self.subTest(difficulty=difficulty):
                self.assertEqual(
                    BattleshipAI(difficulty, seed=1).choose_attack(board), (5, 5)
                )


# --------------------------------------------------------------------------- #
# Speed
# --------------------------------------------------------------------------- #

class SpeedTest(unittest.TestCase):
    def test_hard_moves_stay_fast(self):
        """Hard moves are sub-millisecond in steady state.

        The slowest single move is checked against a generous cap instead of a
        tight one: a GC pause or a busy machine (CI) can add tens of
        milliseconds to one call, which says nothing about the AI itself. The
        median is the number that matters.
        """
        game = _new_game(starter=0, fleet_seed=909)
        hard = BattleshipAI("hard", seed=31)
        easy = BattleshipAI("easy", seed=32)
        times = []
        while game.phase == "battle":
            index = game.turn
            board = game.boards[1 - index]
            if index == 0:
                started = time.perf_counter()
                cell = hard.choose_attack(board)
                times.append(time.perf_counter() - started)
            else:
                cell = easy.choose_attack(board)
            game.fire(index, *cell)
        times.sort()
        median = times[len(times) // 2]
        self.assertLess(median, 0.005, f"median hard move took {median * 1000:.2f} ms")
        self.assertLess(times[-1], 0.25, f"slowest hard move took {times[-1] * 1000:.1f} ms")


# --------------------------------------------------------------------------- #
# Seeded tournaments
# --------------------------------------------------------------------------- #

def _win_rate(
    difficulty: str,
    opponent: str,
    games: int = 60,
    seed_base: int = 0,
) -> float:
    """Win rate of *difficulty* (as player 0) against *opponent* over seeded games."""
    wins = 0
    for index in range(games):
        player = BattleshipAI(difficulty, seed=seed_base + 2 * index)
        rival = BattleshipAI(opponent, seed=seed_base + 2 * index + 1)
        # Alternate the starter so the first-move advantage cancels out.
        winner = _play_game(
            (player, rival),
            starter=index % 2,
            fleet_seed=seed_base + 13 * index,
        )
        wins += int(winner == 0)
    return wins / games


class TournamentTest(unittest.TestCase):
    def test_hard_beats_easy(self):
        rate = _win_rate("hard", "easy", games=60, seed_base=2000)
        self.assertGreater(rate, 0.75, f"hard win rate was {rate:.2%}")

    def test_normal_beats_easy(self):
        rate = _win_rate("normal", "easy", games=60, seed_base=3000)
        self.assertGreater(rate, 0.5, f"normal win rate was {rate:.2%}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
