#!/usr/bin/env python3
"""Connect Four AI: bitboard negamax with alpha-beta, transposition table and
iterative deepening.  Three difficulty levels, no third-party code.

Bitboard layout
---------------
One 64-bit integer per player.  Bit ``row*8 + col`` is set when that player owns
the cell, with ``row = 0`` at the **bottom**.  Columns 0..6 are used; column 7
is a permanently empty sentinel so shifts by 1 never wrap between rows.  A
column is full when its top bit (row 5) is set.

Drop a disc with ``bit = 1 << (height[col] * 8 + col)``.
"""

from __future__ import annotations

import random
import time
from typing import List, Optional, Sequence

H = 6
W = 7
STRIDE = 8  # 7 columns + 1 sentinel
CELLS = H * W

#: Bit of the top cell of every column — used to detect a full column.
TOP_BITS = sum(1 << (STRIDE * (H - 1) + c) for c in range(W))
#: All usable cells (rows 0..H-1, cols 0..W-1).
BOARD_BITS = 0
for _r in range(H):
    for _c in range(W):
        BOARD_BITS |= 1 << (_r * STRIDE + _c)

CENTER_MASK = sum(1 << (STRIDE * r + 3) for r in range(H))

WIN_SCORE = 1_000_000
INF = 10 ** 9

#: Search order: centre column first — it maximises the number of threats.
MOVE_ORDER = (3, 2, 4, 1, 5, 0, 6)

LEVELS = ("easy", "normal", "hard")
#: ``learned`` is the neural-network player trained by ``ai_train.py``.  It is
#: kept out of ``LEVELS`` so older code that loops over the three search levels
#: keeps working; ``ALL_LEVELS`` is what the server and UI accept.
ALL_LEVELS = LEVELS + ("learned",)

#: Search settings of the learned AI: it is the same alpha-beta search as the
#: other levels, but the position evaluator is a neural network (``ai_net.py``).
LEARNED_DEPTH = 8
LEARNED_BUDGET = 1.0  # seconds per move


def _build_windows() -> List[int]:
    """Every four-in-a-row window on the board, as a bit mask."""
    windows: List[int] = []
    for r in range(H):
        for c in range(W):
            for dr, dc in ((0, 1), (1, 0), (1, 1), (1, -1)):
                rr, cc = r + dr * 3, c + dc * 3
                if 0 <= rr < H and 0 <= cc < W:
                    m = 0
                    for k in range(4):
                        m |= 1 << ((r + dr * k) * STRIDE + (c + dc * k))
                    windows.append(m)
    return windows


WINDOWS = _build_windows()


def has_won(pos: int) -> bool:
    """True when ``pos`` contains four in a row."""
    for shift in (1, STRIDE, STRIDE - 1, STRIDE + 1):
        m = pos & (pos >> shift)
        if m & (m >> (2 * shift)):
            return True
    return False


def to_bitboards(cells: Sequence[Sequence[int]]) -> tuple[int, int]:
    """``(seat1_bits, seat2_bits)`` from an engine board (row 0 = top)."""
    red = yellow = 0
    for r in range(H):
        base = (H - 1 - r) * STRIDE
        for c in range(W):
            v = cells[r][c]
            if v == 1:
                red |= 1 << (base + c)
            elif v == 2:
                yellow |= 1 << (base + c)
    return red, yellow


class _Timeout(Exception):
    pass


class Connect4AI:
    """A reusable, thread-safe-ish (one instance per thread) Connect Four AI."""

    def __init__(
        self,
        level: str = "normal",
        seed: Optional[int] = None,
        depth: Optional[int] = None,
        budget: Optional[float] = None,
        value_net=None,
    ) -> None:
        if level not in ALL_LEVELS:
            raise ValueError(f"level must be one of {ALL_LEVELS}")
        self.level = level
        #: Optional overrides (used by the trainer): fixed search depth and
        #: time budget in seconds (``None`` budget = no time limit).
        self._depth_override = depth
        self._budget_override = budget
        self._override = depth is not None
        self.learned_ready = level != "learned"
        #: Network used to score positions (``learned`` level).  ``None`` = the
        #: hand-written heuristic.  The trainer passes its own net here.
        self._explicit_net = value_net
        self._vnet = value_net
        self._salt = 0
        self._rng = random.Random(seed)
        self.nodes = 0
        self.last_depth = 0
        self.last_elapsed = 0.0
        self.last_score = 0
        self._deadline = 0.0
        self._tt: dict = {}

    # -- public ------------------------------------------------------------ #

    def choose_move(self, cells: Sequence[Sequence[int]], seat: int) -> int:
        """Best column for ``seat`` on the engine board ``cells``."""
        red, yellow = to_bitboards(cells)
        cur, other = (red, yellow) if seat == 1 else (yellow, red)
        mask = cur | other
        heights = [0] * W
        for c in range(W):
            h = 0
            while h < H and mask & (1 << (h * STRIDE + c)):
                h += 1
            heights[c] = h
        return self.choose_bits(cur, other, heights)

    def choose_bits(self, cur: int, other: int, heights: Sequence[int]) -> int:
        """Same as :meth:`choose_move`, but on bitboards (used by the trainer)."""
        mask = cur | other
        heights = list(heights)

        legal = [c for c in MOVE_ORDER if heights[c] < H]
        if not legal:
            raise ValueError("no legal move: the board is full")

        self._vnet = self._explicit_net
        # a little per-game variety for the learned AI (see _evaluate)
        self._salt = self._rng.getrandbits(20)

        if self._override:
            depth, budget = int(self._depth_override or 2), self._budget_override
        elif self.level == "learned":
            net = _learned_net()
            if net is not None:
                self._vnet = net
                self.learned_ready = True
                depth, budget = LEARNED_DEPTH, LEARNED_BUDGET
            else:
                # No trained model yet: behave like "normal" so the game still works.
                self.learned_ready = False
                depth, budget = 4, 0.35
        # Easy: shallow search, and sometimes just play a plausible random move.
        elif self.level == "easy":
            if self._rng.random() < 0.35:
                return self._rng.choice(legal)
            depth, budget = 2, 0.05
        elif self.level == "normal":
            depth, budget = 4, 0.35
        else:
            depth, budget = 8, 1.6

        self.nodes = 0
        self.last_depth = 0
        self._tt = {}
        started = time.monotonic()
        self._deadline = started + budget if budget is not None else float("inf")
        best = legal[0]

        # Iterative deepening: keep the last fully completed depth's answer.
        try:
            for d in range(2, depth + 1):
                move = self._root(cur, other, mask, heights, d)
                if move is not None:
                    best = move
                    self.last_depth = d
                if time.monotonic() > self._deadline:
                    break
        except _Timeout:
            pass
        self.last_elapsed = time.monotonic() - started
        return best

    # -- search ------------------------------------------------------------ #

    def _root(self, cur: int, other: int, mask: int, heights: List[int], depth: int) -> Optional[int]:
        best_move: Optional[int] = None
        best_score = -INF
        alpha = -INF
        beta = INF
        for c in MOVE_ORDER:
            h = heights[c]
            if h >= H:
                continue
            bit = 1 << (h * STRIDE + c)
            heights[c] = h + 1
            self.nodes += 1
            if has_won(cur | bit):
                heights[c] = h
                self.last_score = WIN_SCORE
                return c  # immediate win, nothing can be better
            score = -self._negamax(
                other, cur | bit, mask | bit, heights, depth - 1, -beta, -alpha, 1
            )
            heights[c] = h
            if score > best_score:
                best_score, best_move = score, c
            if score > alpha:
                alpha = score
        self.last_score = best_score
        return best_move

    def _negamax(
        self,
        cur: int,
        other: int,
        mask: int,
        heights: List[int],
        depth: int,
        alpha: int,
        beta: int,
        ply: int,
    ) -> int:
        self.nodes += 1
        if depth <= 0:
            return self._evaluate(cur, other)
        if mask == BOARD_BITS:
            return 0

        key = (cur, other, depth)
        entry = self._tt.get(key)
        tt_move: Optional[int] = None
        if entry is not None:
            score, flag, tt_move = entry
            if flag == 0:
                return score
            if flag == 1 and score > alpha:
                alpha = score
            elif flag == -1 and score < beta:
                beta = score
            if alpha >= beta:
                return score

        if self.nodes & 1023 == 0 and time.monotonic() > self._deadline:
            raise _Timeout

        order = MOVE_ORDER
        if tt_move is not None:
            order = (tt_move,) + tuple(c for c in MOVE_ORDER if c != tt_move)

        alpha_orig = alpha
        best = -INF
        best_col: Optional[int] = None
        for c in order:
            h = heights[c]
            if h >= H:
                continue
            bit = 1 << (h * STRIDE + c)
            heights[c] = h + 1
            if has_won(cur | bit):
                heights[c] = h
                return WIN_SCORE - ply
            score = -self._negamax(
                other, cur | bit, mask | bit, heights, depth - 1, -beta, -alpha, ply + 1
            )
            heights[c] = h
            if score > best:
                best, best_col = score, c
            if score > alpha:
                alpha = score
            if alpha >= beta:
                break

        # Compare with the window we *entered* with.  (Using the updated alpha
        # labelled every beta cut-off as an upper bound, which is wrong.)
        if best <= alpha_orig:
            flag = -1  # upper bound
        elif best >= beta:
            flag = 1  # lower bound
        else:
            flag = 0
        if len(self._tt) < 400_000:
            self._tt[key] = (best, flag, best_col)
        return best

    # -- evaluation -------------------------------------------------------- #

    def _evaluate(self, cur: int, other: int) -> int:
        """Score from ``cur``'s point of view (learned network, else heuristic)."""
        net = self._vnet
        if net is not None:
            score = int(net.value(cur, other) * 2000)
            # tiny position-dependent jitter: AI-vs-AI games differ from each other
            return score + (((cur ^ (other * 31) ^ self._salt) * 2654435761 >> 12) % 7) - 3
        score = 0
        for w in WINDOWS:
            mine = (w & cur).bit_count()
            theirs = (w & other).bit_count()
            if mine and theirs:
                continue
            if mine:
                score += (0, 1, 3, 12)[mine]
            elif theirs:
                score -= (0, 1, 3, 12)[theirs]
        # Occupying the centre is structurally valuable.
        score += 4 * (cur & CENTER_MASK).bit_count()
        score -= 4 * (other & CENTER_MASK).bit_count()
        return score


def _learned_net():
    """The trained policy network, or ``None`` (no model file / no numpy)."""
    try:
        import ai_net

        return ai_net.load_model()
    except Exception:
        return None


def learned_available() -> bool:
    """True when ``connect4_model.npz`` exists and can be loaded."""
    return _learned_net() is not None


def choose_move(cells: Sequence[Sequence[int]], seat: int, level: str = "normal") -> int:
    """Convenience one-shot helper used by tests and the simulation mode."""
    return Connect4AI(level).choose_move(cells, seat)
