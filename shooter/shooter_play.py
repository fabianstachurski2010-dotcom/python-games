#!/usr/bin/env python3
"""Watch the trained Barrage AI play (needs shooter_model.npz from shooter_train.py).

The network flies the ship, dashes and spends bombs on its own. Movement keys, Shift,
Space and X are ignored while it drives — but P pauses, R starts a new run, 1 / 2 / 3
change difficulty and Esc quits. When a run ends it waits three seconds and starts the
next one, so you can leave it running like an arcade attract mode (`--once` stops after
the first run instead).

    python3 shooter_play.py                  watch it play
    python3 shooter_play.py -d hard          the same network, harder waves
    python3 shooter_play.py --once           one run, then stop
    python3 shooter_play.py --simulate 60    headless, printed — no window
    python3 shooter_play.py --sound          with the synthesized effects
"""

from __future__ import annotations

import argparse
import sys
import time

from shooter import BOMB_START, DIFFICULTIES, DT, ShooterGUI, ShooterGame
from shooter_train import Brain, MODEL_FILE

try:  # the window is optional: --simulate needs no display
    import tkinter as tk
except ImportError:                                          # pragma: no cover
    tk = None

RESTART_DELAY = 3.0          # seconds of game-over screen before the next run


class AIBarrage(ShooterGUI):
    """The same game, with the network on the stick instead of the keyboard."""

    def __init__(self, brain: Brain, difficulty: str, seed: int | None, lives: int,
                 bombs: int, sound: bool, volume: float, backend: str, once: bool):
        self.brain = brain
        self.once = once
        self.over_since: float | None = None      # wall clock, so a slow frame cannot stretch it
        super().__init__(difficulty, seed, lives, score_path=None, sound=sound,
                         bombs=bombs, volume=volume, sound_backend=backend)
        self.root.title("Barrage — AI 觀戰")

    # -- the network drives ------------------------------------------------- #
    def _direction(self) -> tuple[float, float, bool, bool, bool]:
        dx, dy, dash, bomb = self.brain.act(self.game)
        return dx, dy, dash, False, bomb

    def _on_key_press(self, event) -> None:
        key = event.keysym.lower()
        if key in self.KEYS or key in {"shift_l", "shift_r", "space", "x"}:
            return                      # hands off: the AI is flying
        super()._on_key_press(event)

    def _on_motion(self, event) -> None:
        pass                            # and the mouse does not grab the ship either

    def _on_click(self, _event) -> None:
        if self.game.state != "playing":
            self.dash_or_start()

    def _tick(self) -> None:
        game = self.game
        if game.state == "menu":
            game.start()
            self.over_since = None
        elif game.over and not self.once:
            now = time.monotonic()
            self.over_since = self.over_since or now
            if now - self.over_since >= RESTART_DELAY:
                self.over_since = None
                self.new_run()
        super()._tick()


def run_headless(brain: Brain, seconds: float, difficulty: str, seed: int | None,
                 lives: int, bombs: int, verbose: bool = True) -> ShooterGame:
    """The network playing with no window at all."""
    game = ShooterGame(difficulty, seed=seed, lives=lives, score_path=None, bombs=bombs)
    game.start()
    checkpoint = 300
    for frame in range(int(seconds / DT)):
        if game.over:
            break
        game.step(*brain.controls(game))
        if verbose and frame >= checkpoint:
            checkpoint += 300
            print(f"  t={frame / 60:5.1f}s  {game.zone} 第 {game.wave} 波  分數 {game.score:6d}  "
                  f"生命 {game.player.hp}  敵 {len(game.enemies):2d}  敵彈 {len(game.enemy_bullets):3d}")
    return game


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Watch the trained Barrage AI play.")
    parser.add_argument("-d", "--difficulty", choices=list(DIFFICULTIES), default="easy",
                        help="the difficulty it was trained on (easy by default)")
    parser.add_argument("--model", default=MODEL_FILE)
    parser.add_argument("--lives", type=int, default=3)
    parser.add_argument("--bombs", type=int, default=BOMB_START)
    parser.add_argument("--seed", type=int, help="fix the waves (same run every time)")
    parser.add_argument("--once", action="store_true", help="stop after one run")
    parser.add_argument("--simulate", type=float, metavar="SECONDS",
                        help="play headless for this many seconds and print the result")
    parser.add_argument("--sound", action="store_true", help="play synthesized effects")
    parser.add_argument("--volume", type=float, default=0.6)
    parser.add_argument("--sound-backend", choices=("auto", "pygame", "nssound", "command", "none"),
                        default="auto")
    args = parser.parse_args(argv)

    try:
        brain = Brain.load(args.model)
    except FileNotFoundError:
        print(f"{args.model} not found — run shooter_train.py first.", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"{args.model} does not fit this game: {exc}", file=sys.stderr)
        return 1

    if args.simulate is not None:
        print(f"the trained network, {args.difficulty}, {args.simulate:g}s:")
        game = run_headless(brain, args.simulate, args.difficulty, args.seed,
                            args.lives, args.bombs)
        outcome = "survived" if not game.over else "was destroyed"
        print(f"\nthe AI {outcome} {game.frame / 60:.1f}s — {game.zone} wave {game.wave}, "
              f"score {game.score}, kills {game.kills}, grazes {game.grazes}, "
              f"accuracy {game.accuracy * 100:.0f}%, bombs {game.bombs_used}, "
              f"perfect waves {game.perfect_waves}")
        return 0

    if tk is None:
        print("tkinter is unavailable — try --simulate instead.", file=sys.stderr)
        return 1
    try:
        AIBarrage(brain, args.difficulty, args.seed, args.lives, args.bombs,
                  args.sound, args.volume, args.sound_backend, args.once).run()
    except tk.TclError as exc:                                # no display
        print(f"cannot open a window ({exc}); try --simulate instead.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
