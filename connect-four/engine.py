#!/usr/bin/env python3
"""Pure Connect Four rules engine -- no UI, no sockets, no third-party code.

Coordinates
-----------
``cells[row][col]`` with ``row = 0`` at the **top** and ``row = ROWS - 1`` at the
bottom, exactly like the screen.  ``col = 0`` is the leftmost column.

Seats
-----
``RED`` (1) moves first, ``YELLOW`` (2) second, ``EMPTY`` (0) is a hole.  A
"seat" is ``RED`` or ``YELLOW``.

The engine is authoritative: the LAN server validates every move against it and
the pygame client only renders snapshots, so a buggy client cannot cheat.
"""

from __future__ import annotations

import copy
from typing import Iterable, List, Optional, Sequence

ROWS = 6
COLS = 7
WIN = 4
CELLS = ROWS * COLS

EMPTY = 0
RED = 1
YELLOW = 2
SEATS = (RED, YELLOW)

STATUS_PLAYING = "playing"
STATUS_WIN = "win"
STATUS_DRAW = "draw"

#: ``(dr, dc)`` for the four winning directions: horizontal, vertical, and both
#: diagonals.  Moving along ``+d`` and ``-d`` from the last disc builds the line.
DIRECTIONS = ((0, 1), (1, 0), (1, 1), (1, -1))


class MoveError(Exception):
    """Raised when a move is not legal.  ``code`` is the wire-safe reason."""

    CODES = {
        "bad_column": "bad_column",
        "column_full": "column_full",
        "game_over": "game_over",
    }

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


# --------------------------------------------------------------------------- #
# Board helpers (usable without instantiating a Game)
# --------------------------------------------------------------------------- #

def empty_cells() -> List[List[int]]:
    return [[EMPTY] * COLS for _ in range(ROWS)]


def clone_cells(cells: Sequence[Sequence[int]]) -> List[List[int]]:
    return [list(row) for row in cells]


def in_bounds(row: int, col: int) -> bool:
    return 0 <= row < ROWS and 0 <= col < COLS


def landing_row(cells: Sequence[Sequence[int]], col: int) -> Optional[int]:
    """Lowest empty row in ``col`` (a disc falls from the top), or ``None``."""
    if not 0 <= col < COLS:
        return None
    for row in range(ROWS - 1, -1, -1):
        if cells[row][col] == EMPTY:
            return row
    return None


def other_seat(seat: int) -> int:
    return YELLOW if seat == RED else RED


def winning_line(
    cells: Sequence[Sequence[int]], row: int, col: int, seat: int
) -> Optional[List[List[int]]]:
    """Return every cell of the winning run through ``(row, col)``, or ``None``.

    The list is sorted top-to-bottom / left-to-right so clients can animate it.
    Returns ``None`` unless ``(row, col)`` itself holds ``seat``.
    """
    if not in_bounds(row, col) or cells[row][col] != seat:
        return None
    for dr, dc in DIRECTIONS:
        line = [(row, col)]
        for sign in (1, -1):
            r, c = row + dr * sign, col + dc * sign
            while in_bounds(r, c) and cells[r][c] == seat:
                line.append((r, c))
                r += dr * sign
                c += dc * sign
        if len(line) >= WIN:
            return [[r, c] for r, c in sorted(line)]
    return None


def find_winner(cells: Sequence[Sequence[int]]) -> Optional[int]:
    """Seat that already has four in a row anywhere on the board, or ``None``."""
    for row in range(ROWS):
        for col in range(COLS):
            seat = cells[row][col]
            if seat != EMPTY and winning_line(cells, row, col, seat):
                return seat
    return None


def board_full(cells: Sequence[Sequence[int]]) -> bool:
    return all(cells[0][col] != EMPTY for col in range(COLS))


def legal_moves(cells: Sequence[Sequence[int]]) -> List[int]:
    return [col for col in range(COLS) if cells[0][col] == EMPTY]


def as_board(cells: Sequence[Sequence[int]]) -> List[List[int]]:
    """JSON-safe copy of the board (lists, not tuples)."""
    return [[int(v) for v in row] for row in cells]


def cells_from_moves(moves: Iterable[int]) -> List[List[int]]:
    """Rebuild a board from a bare column list; seats alternate from RED."""
    cells = empty_cells()
    seat = RED
    for col in moves:
        row = landing_row(cells, col)
        if row is None:
            raise MoveError("column_full", f"column {col} is full")
        cells[row][col] = seat
        seat = other_seat(seat)
    return cells


# --------------------------------------------------------------------------- #
# Game
# --------------------------------------------------------------------------- #

class Game:
    """One Connect Four game.  Mutable state, replayable move log."""

    __slots__ = ("cells", "moves", "turn", "winner", "win_line", "draw")

    def __init__(self) -> None:
        self.cells: List[List[int]] = empty_cells()
        #: ``[{"seat": 1, "col": 3, "row": 5}, ...]`` in play order.
        self.moves: List[dict] = []
        self.turn: int = RED
        self.winner: Optional[int] = None
        self.win_line: Optional[List[List[int]]] = None
        self.draw: bool = False

    # -- queries ----------------------------------------------------------- #

    @property
    def over(self) -> bool:
        return self.winner is not None or self.draw

    @property
    def status(self) -> str:
        if self.winner is not None:
            return STATUS_WIN
        if self.draw:
            return STATUS_DRAW
        return STATUS_PLAYING

    def legal_moves(self) -> List[int]:
        return [] if self.over else legal_moves(self.cells)

    def can_play(self, col: int) -> bool:
        return (not self.over) and self.landing_row(col) is not None

    def landing_row(self, col: int) -> Optional[int]:
        return landing_row(self.cells, col)

    def board(self) -> List[List[int]]:
        return as_board(self.cells)

    def last_move(self) -> Optional[dict]:
        return dict(self.moves[-1]) if self.moves else None

    def clone(self) -> "Game":
        return copy.deepcopy(self)

    # -- mutation ---------------------------------------------------------- #

    def drop(self, col: int) -> int:
        """Drop the current seat's disc into ``col``; return the landing row."""
        if self.over:
            raise MoveError("game_over", "the game is already over")
        if not isinstance(col, int) or not 0 <= col < COLS:
            raise MoveError("bad_column", f"column must be 0..{COLS - 1}")
        row = self.landing_row(col)
        if row is None:
            raise MoveError("column_full", f"column {col} is full")

        seat = self.turn
        self.cells[row][col] = seat
        self.moves.append({"seat": seat, "col": col, "row": row})

        line = winning_line(self.cells, row, col, seat)
        if line:
            self.winner = seat
            self.win_line = line
        elif board_full(self.cells):
            self.draw = True
        else:
            self.turn = other_seat(seat)
        return row

    def undo(self) -> Optional[dict]:
        """Take back the last move (used by tests, AI search and replay)."""
        if not self.moves:
            return None
        move = self.moves.pop()
        self.cells[move["row"]][move["col"]] = EMPTY
        self.winner = None
        self.win_line = None
        self.draw = False
        self.turn = move["seat"]
        return move

    def reset(self) -> None:
        self.__init__()  # type: ignore[misc]

    # -- serialisation ----------------------------------------------------- #

    def snapshot(self) -> dict:
        """Everything a client needs to render this position."""
        return {
            "board": self.board(),
            "moves": [dict(m) for m in self.moves],
            "turn": self.turn,
            "winner": self.winner,
            "win_line": self.win_line,
            "draw": self.draw,
            "status": self.status,
        }


# --------------------------------------------------------------------------- #
# Replay
# --------------------------------------------------------------------------- #

def replay_frames(moves: Sequence[Sequence[int]]) -> List[dict]:
    """Turn a record's move list into animation frames.

    ``moves`` may be bare columns (``[3, 3, 4]``) or dicts with ``col``.
    Each frame is a :meth:`Game.snapshot` after that move.
    """
    game = Game()
    frames: List[dict] = []
    for item in moves:
        col = item["col"] if isinstance(item, dict) else int(item)
        game.drop(col)
        frames.append(game.snapshot())
    return frames


def make_record(
    names: dict,
    moves: Sequence[dict],
    winner: Optional[int],
    draw: bool,
    started: str,
    ended: str,
    ai: Optional[dict] = None,
) -> dict:
    """A self-contained, replayable game record (JSON-serialisable)."""
    clean_moves = [
        {"seat": int(m["seat"]), "col": int(m["col"])} for m in moves
    ]
    return {
        "v": 1,
        "game": "connect4",
        "started": started,
        "ended": ended,
        "players": {
            str(RED): {"name": names.get("1", "Red"), "ai": bool((ai or {}).get("seat") == RED)},
            str(YELLOW): {"name": names.get("2", "Yellow"), "ai": bool((ai or {}).get("seat") == YELLOW)},
        },
        "ai_level": (ai or {}).get("level"),
        "moves": clean_moves,
        "columns": [m["col"] for m in clean_moves],
        "winner": winner,
        "draw": bool(draw),
        "move_count": len(clean_moves),
    }
