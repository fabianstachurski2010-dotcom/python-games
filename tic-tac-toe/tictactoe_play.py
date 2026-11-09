#!/usr/bin/env python3
"""Play against the trained Tic-Tac-Toe AI (needs tictactoe_model.npz).

You are X and move first; the AI is O. Click a square or press 1-9.
If the AI ever picks a taken square it forfeits - there is no guard.
R / Enter / F2: new game.
"""

from __future__ import annotations

import sys

from tic_tac_toe import TicTacToe, TicTacToeGUI
from tictactoe_train import Brain


class NetGame(TicTacToe):
    """The normal game, but the computer's moves come from the network."""

    def __init__(self, brain: Brain):
        super().__init__("perfect")
        self.brain = brain

    def ai_move(self) -> int | None:
        if self.over or self.current == self.human:
            return None
        index = self.brain.act(self.board, self.ai_mark)
        if self.board[index] is not None:  # no guard: a taken square forfeits the game
            self.winner, self.win_line, self.over = self.human, None, True
            return index
        self.play(index, self.ai_mark)
        return index


class AITicTacToe(TicTacToeGUI):
    def __init__(self, brain: Brain):
        super().__init__("perfect")
        self.game = NetGame(brain)
        self.new_game()

    def _on_key(self, event) -> None:
        if event.keysym.lower() != "m":  # no opponent switching here
            super()._on_key(event)

    def _refresh(self) -> None:
        super()._refresh()
        self.root.title("Tic-Tac-Toe AI")


def main() -> int:
    try:
        brain = Brain.load()
    except FileNotFoundError:
        print("tictactoe_model.npz not found - run tictactoe_train.py first.", file=sys.stderr)
        return 1
    AITicTacToe(brain).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
