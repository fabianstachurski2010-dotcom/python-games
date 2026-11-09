#!/usr/bin/env python3
"""Tic-Tac-Toe — you (X) against a perfect minimax opponent, or a friend.

GUI controls
    Click a square        place your X
    1-9 keys              place your X (1 = top left, 9 = bottom right)
    R / Enter             new game
    M                     switch opponent (perfect / easy / two players)

The default opponent searches the whole game tree, so it can never be beaten —
the best you can get is a draw. ``--selftest`` proves that by brute force.

Other modes
    --text                play in the terminal
    --selftest            run the logic tests and exit
"""

from __future__ import annotations

import argparse
import random
import sys
from functools import lru_cache

LINES = (
    (0, 1, 2), (3, 4, 5), (6, 7, 8),   # rows
    (0, 3, 6), (1, 4, 7), (2, 5, 8),   # columns
    (0, 4, 8), (2, 4, 6),              # diagonals
)

# Tie-break order for equally good moves: centre, then corners, then edges.
PREFERRED_ORDER = (4, 0, 2, 6, 8, 1, 3, 5, 7)

MODES = {
    "perfect": "perfect AI (unbeatable)",
    "easy": "easy AI (random moves)",
    "two": "two players",
}

try:  # tkinter is optional: --text and --selftest need no display
    import tkinter as tk

    TK_AVAILABLE = True
except ImportError:  # pragma: no cover
    TK_AVAILABLE = False


# --------------------------------------------------------------------------- #
# Game logic — no UI code in here, so it is fully testable.
# --------------------------------------------------------------------------- #


class TicTacToe:
    """Board plus a negamax opponent. X always moves first."""

    def __init__(self, ai_mode: str = "perfect", rng: random.Random | None = None,
                 human: str = "X"):
        if ai_mode not in MODES:
            raise ValueError(f"unknown mode: {ai_mode}")
        self.ai_mode = ai_mode
        self.rng = rng or random.Random()
        self.human = human
        self.ai_mark = self.other(human)
        self.reset()

    # -- setup ------------------------------------------------------------- #

    def reset(self) -> None:
        self.board: list[str | None] = [None] * 9
        self.current = "X"
        self.winner: str | None = None
        self.win_line: tuple[int, int, int] | None = None
        self.over = False

    @staticmethod
    def other(mark: str) -> str:
        return "O" if mark == "X" else "X"

    # -- board queries (also usable standalone, which the tests rely on) ---- #

    @staticmethod
    def winner_of(board: list[str | None]) -> str | None:
        for a, b, c in LINES:
            if board[a] is not None and board[a] == board[b] == board[c]:
                return board[a]
        return None

    @staticmethod
    def win_line_of(board: list[str | None]) -> tuple[int, int, int] | None:
        for line in LINES:
            a, b, c = line
            if board[a] is not None and board[a] == board[b] == board[c]:
                return line
        return None

    @staticmethod
    def available_in(board: list[str | None]) -> list[int]:
        return [i for i, value in enumerate(board) if value is None]

    @staticmethod
    def is_full(board: list[str | None]) -> bool:
        return all(value is not None for value in board)

    # -- the opponent ------------------------------------------------------ #

    @staticmethod
    @lru_cache(maxsize=None)
    def _solve(state: tuple[str | None, ...], to_move: str) -> int:
        """Perfect-play result for ``to_move``: +1 win, 0 draw, -1 loss.

        Memoised on the position, which keeps the search instant — the board
        has only 3**9 states, so the GUI can ask for a move every turn.
        """
        if TicTacToe.winner_of(state) is not None:
            return -1  # the side to move has already lost
        if all(value is not None for value in state):
            return 0
        best = -1
        for index, value in enumerate(state):
            if value is None:
                child = state[:index] + (to_move,) + state[index + 1:]
                score = -TicTacToe._solve(child, TicTacToe.other(to_move))
                if score > best:
                    best = score
                    if best == 1:
                        break  # nothing beats winning right now
        return best

    @classmethod
    def minimax(cls, board: list[str | None], to_move: str, depth: int = 0) -> int:
        """Perfect-play result for ``to_move``: +1 win, 0 draw, -1 loss.

        ``depth`` is accepted so call sites read like the classic algorithm;
        the score describes the result, not how quickly it is reached.
        """
        return cls._solve(tuple(board), to_move)

    @classmethod
    def best_move_for(cls, board: list[str | None], mark: str = "O") -> int | None:
        """The best square for ``mark``, or None on a finished board.

        A win that is available right now is always taken, then the move is
        chosen by perfect play. Equally good moves are ordered centre, corners,
        then edges, which is what makes the computer's play look natural.
        """
        if cls.winner_of(board) is not None or cls.is_full(board):
            return None

        # The outcome score below only says "this wins eventually", so check
        # for an immediate win first — taking it is what a player expects.
        for index in cls.available_in(board):
            child = list(board)
            child[index] = mark
            if cls.winner_of(child) == mark:
                return index

        best_score: int | None = None
        best_moves: list[int] = []
        for index in cls.available_in(board):
            child = list(board)
            child[index] = mark
            score = -cls._solve(tuple(child), cls.other(mark))
            if best_score is None or score > best_score:
                best_score, best_moves = score, [index]
            elif score == best_score:
                best_moves.append(index)
        for index in PREFERRED_ORDER:
            if index in best_moves:
                return index
        return best_moves[0]

    # -- moves ------------------------------------------------------------- #

    def play(self, index: int, mark: str | None = None) -> bool:
        """Place ``mark`` (default: whoever is to move). False if illegal."""
        if self.over or not isinstance(index, int) or not 0 <= index < 9:
            return False
        if self.board[index] is not None:
            return False
        mark = mark or self.current
        self.board[index] = mark
        self.winner = self.winner_of(self.board)
        if self.winner is not None:
            self.win_line = self.win_line_of(self.board)
            self.over = True
        elif self.is_full(self.board):
            self.over = True
        else:
            self.current = self.other(mark)
        return True

    def human_move(self, index: int) -> bool:
        """A move by the person at the keyboard (or two-player mode)."""
        if self.ai_mode != "two" and self.current != self.human:
            return False
        return self.play(index)

    def ai_move(self) -> int | None:
        """Let the computer move; returns the square it picked."""
        if self.over or self.ai_mode == "two" or self.current == self.human:
            return None
        if self.ai_mode == "perfect":
            index = self.best_move_for(self.board, self.ai_mark)
        else:
            options = self.available_in(self.board)
            index = self.rng.choice(options) if options else None
        if index is None:
            return None
        self.play(index, self.ai_mark)
        return index

    # -- state ------------------------------------------------------------- #

    @property
    def available(self) -> list[int]:
        return self.available_in(self.board)

    @property
    def is_draw(self) -> bool:
        return self.over and self.winner is None

    @property
    def status_text(self) -> str:
        if self.winner is not None:
            return f"{self.winner} wins!"
        if self.is_full(self.board):
            return "It's a draw."
        return f"{self.current} to move."

    def copy(self) -> "TicTacToe":
        clone = TicTacToe(self.ai_mode, self.rng, self.human)
        clone.board = list(self.board)
        clone.current = self.current
        clone.winner = self.winner
        clone.win_line = self.win_line
        clone.over = self.over
        return clone


# --------------------------------------------------------------------------- #
# Terminal UI
# --------------------------------------------------------------------------- #

TEXT_HELP = """commands:
  1-9       play that square (1 = top left, 9 = bottom right)
  n         new game        q  quit        ?  help"""


def render_text(game: TicTacToe) -> str:
    cells = [game.board[i] or str(i + 1) for i in range(9)]
    rows = [" {} | {} | {} ".format(*cells[i:i + 3]) for i in (0, 3, 6)]
    return ("\n" + "---+---+---\n").join(rows)


def run_text(game: TicTacToe, seed: int | None) -> int:
    print(f"Tic-Tac-Toe — you are X vs {MODES[game.ai_mode]}\n{TEXT_HELP}\n")
    while True:
        print(render_text(game))
        print(game.status_text)
        if game.over:
            answer = input("play again? [y/N] ").strip().lower()
            if answer == "y":
                game = TicTacToe(game.ai_mode, random.Random(seed), game.human)
                print()
                continue
            return 0
        if game.ai_mode != "two" and game.current == game.ai_mark:
            game.ai_move()
            print()
            continue

        try:
            raw = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not raw:
            continue
        if raw in {"q", "quit", "exit"}:
            return 0
        if raw in {"?", "h", "help"}:
            print(TEXT_HELP)
            continue
        if raw in {"n", "new"}:
            game = TicTacToe(game.ai_mode, random.Random(seed), game.human)
            print()
            continue
        if not raw.isdigit() or not 1 <= int(raw) <= 9:
            print("? enter a number from 1 to 9")
            continue
        if not game.human_move(int(raw) - 1):
            print("? that square is taken")
        print()


# --------------------------------------------------------------------------- #
# Tkinter GUI
# --------------------------------------------------------------------------- #


class TicTacToeGUI:
    BG = "#1c1c1e"
    CELL_BG = "#2c2c2e"
    WIN_BG = "#3f5f3a"
    X_COLOR = "#5ea1ff"
    O_COLOR = "#ff6b6b"
    FG = "#e8e8e8"
    MODE_NAMES = {"perfect": "電腦（無敵）", "easy": "電腦（簡單）", "two": "雙人"}
    AI_DELAY_MS = 180

    def __init__(self, ai_mode: str = "perfect", seed: int | None = None):
        self.seed = seed
        self.ai_mode = ai_mode
        self.game = TicTacToe(ai_mode, random.Random(seed))
        self.tally = {"human": 0, "ai": 0, "draw": 0}
        self.counted = False
        self.pending_ai: str | None = None

        self.root = tk.Tk()
        self.root.configure(bg=self.BG)
        self.root.resizable(False, False)

        self._build_hud()
        self._build_grid()
        self._bind_keys()
        self._refresh()

    # -- widgets ----------------------------------------------------------- #

    def _build_hud(self) -> None:
        hud = tk.Frame(self.root, bg=self.BG, padx=12, pady=10)
        hud.grid(row=0, column=0, sticky="ew")
        self.status_var = tk.StringVar()
        self.score_var = tk.StringVar()
        tk.Label(hud, textvariable=self.status_var, font=("Helvetica", 16, "bold"),
                 bg=self.BG, fg=self.FG).pack(side="left")
        tk.Label(hud, textvariable=self.score_var, font=("Helvetica", 12),
                 bg=self.BG, fg="#8e8e93").pack(side="right")

    def _build_grid(self) -> None:
        frame = tk.Frame(self.root, bg=self.BG, padx=12, pady=0)
        frame.grid(row=1, column=0)
        self.cells: list[tk.Label] = []
        for index in range(9):
            cell = tk.Label(
                frame, text="", width=3, height=1,
                font=("Helvetica", 34, "bold"), bg=self.CELL_BG, fg=self.FG,
            )
            cell.grid(row=index // 3, column=index % 3, padx=2, pady=2)
            cell.bind("<Button-1>", lambda _e, i=index: self.on_cell(i))
            self.cells.append(cell)
        tk.Frame(self.root, bg=self.BG, height=10).grid(row=2, column=0)

    def _bind_keys(self) -> None:
        self.root.bind("<Key>", self._on_key)
        self.root.bind("<F2>", lambda _e: self.new_game())
        self.root.bind("<Return>", lambda _e: self.new_game())

    # -- input ------------------------------------------------------------- #

    def _on_key(self, event) -> None:
        key = event.keysym.lower()
        if key in {"r", "n"}:
            self.new_game()
        elif key == "m":
            modes = list(MODES)
            self.set_mode(modes[(modes.index(self.ai_mode) + 1) % len(modes)])
        elif key == "escape":
            self.root.destroy()
        elif key.isdigit() and key != "0":
            self.on_cell(int(key) - 1)

    def on_cell(self, index: int) -> None:
        if self.game.over or index not in self.game.available:
            return
        if self.game.ai_mode != "two" and self.game.current != self.game.human:
            return  # the computer is about to move
        if not self.game.human_move(index):
            return
        self._refresh()
        if not self.game.over and self.game.ai_mode != "two":
            self._schedule_ai()

    def _schedule_ai(self) -> None:
        self.status_var.set("電腦思考中")
        self.pending_ai = self.root.after(self.AI_DELAY_MS, self._ai_turn)

    def _ai_turn(self) -> None:
        self.pending_ai = None
        if self.game.over:
            return
        self.game.ai_move()
        self._refresh()

    # -- game flow --------------------------------------------------------- #

    def new_game(self) -> None:
        if self.pending_ai is not None:
            self.root.after_cancel(self.pending_ai)
            self.pending_ai = None
        self.game.reset()
        self.counted = False
        self._refresh()

    def set_mode(self, mode: str) -> None:
        self.ai_mode = mode
        self.game = TicTacToe(mode, random.Random(self.seed))
        self.tally = {"human": 0, "ai": 0, "draw": 0}
        self.new_game()

    def _record_result(self) -> None:
        if self.counted or not self.game.over:
            return
        self.counted = True
        if self.game.winner is None:
            self.tally["draw"] += 1
        elif self.game.ai_mode == "two":
            self.tally["human" if self.game.winner == "X" else "ai"] += 1
        elif self.game.winner == self.game.human:
            self.tally["human"] += 1
        else:
            self.tally["ai"] += 1

    # -- drawing ----------------------------------------------------------- #

    def _refresh(self) -> None:
        game = self.game
        for index, cell in enumerate(self.cells):
            mark = game.board[index]
            in_win_line = game.win_line is not None and index in game.win_line
            cell.config(
                text=mark or "",
                fg=self.X_COLOR if mark == "X" else self.O_COLOR,
                bg=self.WIN_BG if in_win_line else self.CELL_BG,
            )
        self._record_result()

        two = game.ai_mode == "two"
        t = self.tally
        left, right = ("X", "O") if two else ("你", "電腦")
        self.score_var.set(f"{left} {t['human']}   {right} {t['ai']}   平 {t['draw']}")
        self.root.title(self.MODE_NAMES[game.ai_mode])

        if game.winner is not None:
            if two:
                self.status_var.set(f"{game.winner} 贏了")
            else:
                self.status_var.set("你贏了" if game.winner == game.human else "你輸了")
        elif game.over:
            self.status_var.set("平手")
        elif two:
            self.status_var.set(f"輪到 {game.current}")
        else:
            self.status_var.set("輪到你")

    def run(self) -> None:
        self.root.mainloop()


# --------------------------------------------------------------------------- #
# Self test
# --------------------------------------------------------------------------- #


def perfect_ai_record(human: str = "X") -> dict[str, int]:
    """Play out *every* human strategy against the perfect AI.

    Returns how the games end. ``human`` must stay 0 — that is what
    "unbeatable" means.
    """
    ai = TicTacToe.other(human)
    tally = {"ai": 0, "draw": 0, "human": 0}

    def recurse(board: list[str | None], turn: str) -> None:
        winner = TicTacToe.winner_of(board)
        if winner is not None:
            tally["ai" if winner == ai else "human"] += 1
            return
        if TicTacToe.is_full(board):
            tally["draw"] += 1
            return
        if turn == human:
            for index in TicTacToe.available_in(board):
                board[index] = human
                recurse(board, ai)
                board[index] = None
        else:
            move = TicTacToe.best_move_for(board, ai)
            assert move is not None
            board[move] = ai
            recurse(board, human)
            board[move] = None

    recurse([None] * 9, human)
    return tally


def selftest(verbose: bool = True) -> int:
    checks = 0

    def check(name: str, condition: bool) -> None:
        nonlocal checks
        checks += 1
        if not condition:
            raise AssertionError(name)
        if verbose:
            print(f"  ok  {name}")

    # win detection on every row, column and diagonal
    for line in LINES:
        board = [None] * 9
        for index in line:
            board[index] = "X"
        check(f"win detected on {line}", TicTacToe.winner_of(board) == "X"
              and TicTacToe.win_line_of(board) == line)

    # a full board with no line is a draw
    draw_board = ["X", "O", "X", "X", "O", "O", "O", "X", "X"]
    check("full board with no line is a draw",
          TicTacToe.winner_of(draw_board) is None and TicTacToe.is_full(draw_board))
    game = TicTacToe()
    for index in (0, 1, 2, 4, 3, 5, 7, 6, 8):
        game.play(index)
    check("nine moves fill the board", game.over and TicTacToe.is_full(game.board))
    check("a drawn game reports a draw",
          game.winner is None and game.is_draw and game.status_text == "It's a draw.")

    # illegal moves are rejected
    game = TicTacToe()
    check("first move is legal", game.human_move(4) is True)
    check("occupied square rejected", game.human_move(4) is False)
    check("out of range rejected", game.play(-1) is False and game.play(9) is False
          and game.play("x") is False)

    # the opponent knows how to take a win and how to block one
    board = ["O", "O", None, "X", None, "X", None, None, None]
    check("takes the immediate winning square", TicTacToe.best_move_for(board, "O") == 2)

    board = ["X", "X", None, None, "O", None, None, None, None]
    check("blocks the only losing square", TicTacToe.best_move_for(board, "O") == 2)

    board = [None] * 9
    check("opens in the centre", TicTacToe.best_move_for(board, "O") == 4)
    check("no move on a finished board",
          TicTacToe.best_move_for(["X", "X", "X", "O", "O", None, None, None, None], "O") is None)

    # the headline claim: no human strategy beats the perfect AI
    record = perfect_ai_record("X")
    check("perfect AI never loses (exhaustive search)", record["human"] == 0)
    check("perfect AI search reached real games",
          record["ai"] + record["draw"] > 0 and record["ai"] > 0 and record["draw"] > 0)
    if verbose:
        print(f"      every human strategy: AI wins {record['ai']}, draws {record['draw']}, "
              f"human wins {record['human']}")

    # ... and a naive "always the first free square" player loses to it
    game = TicTacToe("perfect", random.Random(0))
    while not game.over:
        game.human_move(game.available[0])
        if not game.over:
            game.ai_move()
    check("a naive player loses to the perfect AI", game.winner == "O")

    # easy mode plays legally, two-player mode never auto-moves
    game = TicTacToe("easy", random.Random(5))
    game.human_move(0)
    index = game.ai_move()
    check("easy AI plays a legal square",
          index in range(9) and game.board[index] == "O" and len(game.available) == 7)

    game = TicTacToe("two", random.Random(0))
    game.human_move(0)
    check("two-player mode leaves the turn alone",
          game.current == "O" and game.ai_move() is None)
    game.human_move(4)
    check("two-player mode alternates", game.board[4] == "O" and game.current == "X")

    # the AI never plays for a human who is not on turn
    game = TicTacToe("perfect", random.Random(0))
    check("AI waits for its turn", game.ai_move() is None and game.board == [None] * 9)

    # a copy is independent, which the GUI and tests rely on
    game = TicTacToe()
    game.play(0)
    clone = game.copy()
    clone.play(1)
    check("copies are independent", game.board[1] is None and clone.board[1] == "O")

    # status text
    game = TicTacToe()
    check("status reports whose turn it is", game.status_text == "X to move.")
    game.play(0, "X")
    check("status follows the turn", game.status_text == "O to move.")

    print(f"\n{checks} checks passed.")
    return 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Tic-Tac-Toe (Tkinter GUI by default, --text for the terminal).",
    )
    parser.add_argument("-m", "--mode", choices=MODES, default="perfect",
                        help="opponent (default: perfect)")
    parser.add_argument("--seed", type=int, help="fixed random seed for the easy AI")
    parser.add_argument("--text", action="store_true", help="play in the terminal")
    parser.add_argument("--selftest", action="store_true", help="run the logic tests and exit")
    args = parser.parse_args(argv)

    if args.selftest:
        print("running Tic-Tac-Toe self tests...")
        return selftest()

    game = TicTacToe(args.mode, random.Random(args.seed))
    if args.text or not TK_AVAILABLE:
        if not args.text:
            print("tkinter is unavailable — falling back to terminal mode.", file=sys.stderr)
        return run_text(game, args.seed)

    try:
        TicTacToeGUI(args.mode, args.seed).run()
    except tk.TclError as exc:  # no display
        print(f"cannot open a window ({exc}); use --text instead.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
