#!/usr/bin/env python3
"""Rules engine, bitboard AI and replay storage -- no pygame, no sockets."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ai  # noqa: E402
import engine  # noqa: E402
import records  # noqa: E402


def play(cols):
    game = engine.Game()
    for col in cols:
        game.drop(col)
    return game


class EngineTests(unittest.TestCase):
    def test_start_state(self):
        game = engine.Game()
        self.assertEqual(game.turn, engine.RED)
        self.assertEqual(len(game.cells), engine.ROWS)
        self.assertEqual(len(game.cells[0]), engine.COLS)
        self.assertTrue(all(cell == engine.EMPTY for row in game.cells for cell in row))
        self.assertEqual(game.legal_moves(), list(range(engine.COLS)))
        self.assertFalse(game.over)

    def test_discs_stack_from_the_bottom(self):
        game = engine.Game()
        rows = [game.drop(3) for _ in range(engine.ROWS)]
        self.assertEqual(rows, [5, 4, 3, 2, 1, 0])
        self.assertEqual(game.legal_moves(), [0, 1, 2, 4, 5, 6])
        with self.assertRaises(engine.MoveError) as ctx:
            game.drop(3)
        self.assertEqual(ctx.exception.code, "column_full")

    def test_bad_column(self):
        game = engine.Game()
        for bad in (-1, engine.COLS, 99):
            with self.assertRaises(engine.MoveError) as ctx:
                game.drop(bad)
            self.assertEqual(ctx.exception.code, "bad_column")

    def test_move_after_game_over(self):
        game = play([3, 0, 3, 1, 3, 2, 3])
        self.assertTrue(game.over)
        self.assertEqual(game.winner, engine.RED)
        with self.assertRaises(engine.MoveError) as ctx:
            game.drop(0)
        self.assertEqual(ctx.exception.code, "game_over")

    def test_win_horizontal(self):
        # RED bottom row of columns 0..3, YELLOW drops into the row above.
        game = play([0, 0, 1, 1, 2, 2, 3])
        self.assertEqual(game.winner, engine.RED)
        self.assertEqual(game.win_line, [[5, 0], [5, 1], [5, 2], [5, 3]])

    def test_win_vertical(self):
        game = play([3, 0, 3, 1, 3, 2, 3])
        self.assertEqual(game.winner, engine.RED)
        self.assertEqual(game.win_line, [[2, 3], [3, 3], [4, 3], [5, 3]])

    def test_win_diagonal_up_right(self):
        game = play([0, 1, 1, 2, 3, 2, 2, 3, 3, 6, 3])
        self.assertEqual(game.winner, engine.RED)
        self.assertEqual(sorted(game.win_line), [[2, 3], [3, 2], [4, 1], [5, 0]])

    def test_win_diagonal_up_left(self):
        game = play([3, 2, 2, 1, 0, 1, 1, 0, 0, 6, 0])
        self.assertEqual(game.winner, engine.RED)
        self.assertEqual(sorted(game.win_line), [[2, 0], [3, 1], [4, 2], [5, 3]])

    def test_no_win_with_three(self):
        game = play([0, 0, 1, 1, 2])
        self.assertFalse(game.over)
        self.assertIsNone(game.winner)

    def test_winning_line_uses_the_dropped_disc(self):
        game = play([0, 0, 1, 1, 2, 2])
        self.assertIsNone(engine.winning_line(game.cells, 5, 3, engine.RED))
        game.drop(3)
        self.assertEqual(engine.winning_line(game.cells, 5, 3, engine.RED), game.win_line)

    def test_undo_restores_state(self):
        game = play([3, 3])
        before = game.clone()
        game.drop(4)
        game.undo()
        self.assertEqual(game.cells, before.cells)
        self.assertEqual(game.turn, before.turn)
        self.assertEqual(game.board(), before.board())

    def test_snapshot_and_replay(self):
        cols = [3, 0, 4, 1]
        game = play(cols)
        snap = game.snapshot()
        self.assertEqual(len(snap["moves"]), len(cols))
        self.assertEqual(snap["status"], engine.STATUS_PLAYING)
        frames = engine.replay_frames(cols)
        self.assertEqual(len(frames), len(cols))
        self.assertEqual(frames[-1]["board"], snap["board"])
        self.assertEqual(engine.cells_from_moves(cols), game.cells)

    def test_find_winner_agrees_with_bitboard(self):
        rng = __import__("random").Random(20241025)
        for _ in range(300):
            game = engine.Game()
            while not game.over:
                game.drop(rng.choice(game.legal_moves()))
            bit_red, bit_yellow = ai.to_bitboards(game.cells)
            self.assertEqual(bool(ai.has_won(bit_red)), game.winner == engine.RED)
            self.assertEqual(bool(ai.has_won(bit_yellow)), game.winner == engine.YELLOW)
            self.assertEqual(engine.find_winner(game.cells), game.winner)

    def test_full_board_is_a_draw(self):
        # Alternating columns fill the board without either side lining up four.
        cells = engine.empty_cells()
        pattern = [
            [1, 2, 1, 2, 1, 2, 1],
            [1, 2, 1, 2, 1, 2, 1],
            [2, 1, 2, 1, 2, 1, 2],
            [2, 1, 2, 1, 2, 1, 2],
            [1, 2, 1, 2, 1, 2, 1],
            [1, 2, 1, 2, 1, 2, 1],
        ]
        for row in range(engine.ROWS):
            for col in range(engine.COLS):
                cells[row][col] = pattern[row][col]
        self.assertTrue(engine.board_full(cells))
        self.assertIsNone(engine.find_winner(cells))
        self.assertEqual(engine.legal_moves(cells), [])


class AiTests(unittest.TestCase):
    def test_takes_an_immediate_win(self):
        cells = engine.empty_cells()
        # YELLOW has three in a row on the bottom; RED has nothing useful.
        for col in (0, 1, 2):
            cells[5][col] = engine.YELLOW
        cells[5][6] = engine.RED
        move = ai.choose_move(cells, engine.YELLOW, "hard")
        self.assertIn(move, (3, 4, 5))

    def test_blocks_the_opponent(self):
        cells = engine.empty_cells()
        for col in (0, 1, 2):
            cells[5][col] = engine.RED
        cells[4][5] = engine.RED
        move = ai.choose_move(cells, engine.YELLOW, "normal")
        self.assertEqual(move, 3)

    def test_levels_play_legal_moves(self):
        for level in ("easy", "normal"):
            game = engine.Game()
            while not game.over and len(game.moves) < 12:
                col = ai.choose_move(game.cells, game.turn, level)
                self.assertIn(col, game.legal_moves())
                game.drop(col)

    def test_easy_is_fast(self):
        game = engine.Game()
        import time

        started = time.perf_counter()
        for _ in range(6):
            game.drop(ai.choose_move(game.cells, game.turn, "easy"))
        self.assertLess(time.perf_counter() - started, 2.0)


class RecordTests(unittest.TestCase):
    def make_record(self, winner=engine.RED, draw=False):
        game = play([3, 0, 3, 1, 3, 2, 3] if winner else [0, 1, 1, 0, 2])
        return engine.make_record(
            names={"1": "Kyle", "2": "Amy"},
            moves=game.moves,
            winner=winner,
            draw=draw,
            started="2026-10-25T10:18:00",
            ended="2026-10-25T10:19:30",
        )

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = self.make_record()
            path = records.save(record, directory=tmp)
            self.assertIsNotNone(path)
            self.assertTrue(Path(path).exists())
            loaded = records.load(path)
            self.assertEqual(loaded["columns"], record["columns"])
            self.assertEqual(loaded["winner"], engine.RED)
            self.assertEqual(len(records.frames_for(loaded)), len(record["columns"]))
            listing = records.list_records(tmp)
            self.assertEqual(len(listing), 1)
            info = listing[0]["info"]
            self.assertEqual((info["red"], info["yellow"]), ("Kyle", "Amy"))
            self.assertEqual(info["winner"], engine.RED)
            self.assertIn("Kyle", info["when"] + "Kyle")

    def test_saving_twice_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = self.make_record()
            records.save(record, directory=tmp)
            records.save(record, directory=tmp)
            self.assertEqual(len(list(Path(tmp).glob("*.json"))), 1)

    def test_rejects_foreign_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.json"
            path.write_text('{"game": "battleship"}', encoding="utf-8")
            self.assertIsNone(records.load(path))

    def test_pretty_when_has_both_languages(self):
        record = self.make_record()
        zh = records.pretty_when(record, lang="zh")
        en = records.pretty_when(record, lang="en")
        self.assertTrue(zh)
        self.assertTrue(en)
        self.assertNotEqual(zh, en)


if __name__ == "__main__":
    unittest.main(verbosity=2)
