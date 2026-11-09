#!/usr/bin/env python3
"""Watch the trained Pong AI play (needs pong_model.npz from pong_train.py).

The AI is the blue paddle on the left, the built-in computer is on the right.
It stops when the match ends. N or a click starts another match.
1 / 2 / 3 change how fast the computer opponent is.
"""

from __future__ import annotations

import argparse
import sys

from pong import DIFFICULTIES, PongGUI
from pong_train import Brain


class AIPong(PongGUI):
    LEFT_WINS = "AI 贏了"
    RIGHT_WINS = "AI 輸了"

    def __init__(self, brain: Brain, ai_speed: float, seed: int | None):
        self.brain = brain  # must exist before the first frame is scheduled
        super().__init__(ai_speed, 5, seed)
        self.root.title("Pong AI")

    def _on_key_press(self, event) -> None:
        if event.keysym.lower() in {"up", "down", "w", "s", "t"}:
            return  # the AI drives; two-player mode is off
        super()._on_key_press(event)

    def _on_motion(self, event) -> None:
        pass  # the mouse does not move the AI's paddle

    def _directions(self) -> tuple[int, int]:
        return self.brain.act(self.game), 0

    def _tick(self) -> None:
        if self.game.serving and not self.game.over and not self.paused:
            self.game.serve()
        super()._tick()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Watch the trained Pong AI.")
    parser.add_argument("-d", "--difficulty", choices=DIFFICULTIES, default="normal")
    parser.add_argument("--seed", type=int, help="fixed serves (same match every time)")
    parser.add_argument("--model", default="pong_model.npz")
    args = parser.parse_args(argv)

    try:
        brain = Brain.load(args.model)
    except FileNotFoundError:
        print(f"{args.model} not found - run pong_train.py first.", file=sys.stderr)
        return 1
    AIPong(brain, DIFFICULTIES[args.difficulty], args.seed).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
