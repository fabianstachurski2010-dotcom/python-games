#!/usr/bin/env python3
"""Train a Pong paddle by evolution (numpy only, no other installs).

    python3 pong_train.py            train, saves pong_model.npz
    python3 pong_train.py --test     score the saved model against a random player

A small neural network sees the ball and the paddles and picks every frame:
up, stay or down. It plays the left paddle against the built-in computer
player. The best networks are kept and mutated to make the next generation.
Ctrl+C stops and keeps the best.

The network gets raw numbers only. The tracking helper in pong.py is not used,
so whatever it learns, it learns by itself.
"""

from __future__ import annotations

import argparse
import random
import sys
import time

import numpy as np

from pong import DIFFICULTIES, PongGame

N_IN, N_HID, N_OUT = 7, 10, 3
N_PARAMS = N_HID * N_IN + N_HID + N_OUT * N_HID + N_OUT
MODEL_FILE = "pong_model.npz"


# --------------------------------------------------------------------------- #
# The brain
# --------------------------------------------------------------------------- #


def observe(g: PongGame) -> np.ndarray:
    """What the network sees: ball position and speed, both paddles."""
    mine = g.left_y + g.PADDLE_H / 2
    theirs = g.right_y + g.PADDLE_H / 2
    return np.array([
        g.ball_x / g.WIDTH * 2 - 1,
        g.ball_y / g.HEIGHT * 2 - 1,
        g.ball_vx / g.MAX_BALL_VX,
        g.ball_vy / g.MAX_BALL_VY,
        mine / g.HEIGHT * 2 - 1,
        (g.ball_y - mine) / g.HEIGHT * 2,
        theirs / g.HEIGHT * 2 - 1,
    ])


class Brain:
    """7 inputs -> 10 tanh units -> 3 outputs (up, stay, down)."""

    def __init__(self, params: np.ndarray):
        a = N_HID * N_IN
        self.w1 = params[:a].reshape(N_HID, N_IN)
        self.b1 = params[a:a + N_HID]
        b = a + N_HID
        self.w2 = params[b:b + N_OUT * N_HID].reshape(N_OUT, N_HID)
        self.b2 = params[-N_OUT:]

    def act(self, g: PongGame) -> int:
        """-1 = up, 0 = stay, +1 = down."""
        hidden = np.tanh(self.w1 @ observe(g) + self.b1)
        return int(np.argmax(self.w2 @ hidden + self.b2)) - 1

    @staticmethod
    def load(path: str = MODEL_FILE) -> "Brain":
        return Brain(np.load(path)["params"])


# --------------------------------------------------------------------------- #
# Playing
# --------------------------------------------------------------------------- #


def run_match(act, ai_speed: float, seed: int, max_frames: int = 4000):
    """Play one headless match. Returns (points for us, points against, paddle hits, won)."""
    g = PongGame(random.Random(seed), ai_speed=ai_speed)
    hits = frames = 0
    while not g.over and frames < max_frames:
        if g.serving:
            g.serve()
        hits += g.step(act(g)).count("paddle:left")
        frames += 1
    return g.score_left, g.score_right, hits, g.winner == "left"


def random_player(seed: int):
    rng = random.Random(seed)
    return lambda _g: rng.choice((-1, 0, 1))


# --------------------------------------------------------------------------- #
# Evolution
# --------------------------------------------------------------------------- #


def evaluate(params: np.ndarray, seeds, ai_speed: float):
    """Fitness plus a few numbers to print. Hits give a hint before any point is scored."""
    brain = Brain(params)
    fitness = wins = mine = theirs = 0.0
    for seed in seeds:
        sl, sr, hits, won = run_match(brain.act, ai_speed, seed)
        fitness += 3 * sl - 2 * sr + 0.2 * hits + (5 if won else 0)
        wins += won
        mine += sl
        theirs += sr
    n = len(seeds)
    return fitness / n, wins / n, mine / n, theirs / n


def save(params: np.ndarray) -> None:
    np.savez(MODEL_FILE, params=params)


def report(params: np.ndarray, ai_speed: float) -> None:
    seeds = range(10_000, 10_030)  # matches the network has never seen
    brain = Brain(params)
    for name, act_for in (("AI    ", lambda s: brain.act), ("random", random_player)):
        results = [run_match(act_for(s), ai_speed, s) for s in seeds]
        wins = sum(r[3] for r in results)
        mine = sum(r[0] for r in results) / len(results)
        theirs = sum(r[1] for r in results) / len(results)
        print(f"  {name}  won {wins:2d}/{len(results)}   avg points {mine:.1f} : {theirs:.1f}")


def train(args) -> int:
    ai_speed = DIFFICULTIES[args.difficulty]
    rng = np.random.default_rng(args.seed)

    pop = rng.normal(0, 1, (args.pop, N_PARAMS))
    if args.resume:
        champion = np.load(MODEL_FILE)["params"]
        pop[0] = champion
        for i in range(1, args.pop):
            pop[i] = champion + rng.normal(0, 0.1, N_PARAMS)

    print(f"training against the '{args.difficulty}' computer player (speed {ai_speed:g}) "
          f"- population {args.pop}, Ctrl+C to stop\n")
    best = pop[0]
    try:
        for gen in range(1, args.generations + 1):
            start = time.time()
            seeds = [int(rng.integers(1 << 30)) for _ in range(args.episodes)]
            results = [evaluate(p, seeds, ai_speed) for p in pop]
            order = np.argsort([-r[0] for r in results])
            best = pop[order[0]].copy()
            fit, win_rate, mine, theirs = results[order[0]]
            avg = sum(r[0] for r in results) / len(results)
            save(best)
            print(f"gen {gen:3d}   best won {win_rate * 100:3.0f}%  points {mine:3.1f} : {theirs:3.1f}"
                  f"   fitness {fit:5.1f}  (avg {avg:5.1f})   {time.time() - start:4.1f}s")

            if win_rate >= 1.0:
                # looks perfect - double-check on 20 matches it has never seen
                fresh = [int(rng.integers(1 << 30)) for _ in range(20)]
                if evaluate(best, fresh, ai_speed)[1] >= 0.9:
                    print("\nwon the training matches and 90%+ of 20 fresh ones - done.")
                    break

            elites = pop[order[:max(2, args.pop // 10)]]
            top_half = pop[order[:args.pop // 2]]
            children = []
            for _ in range(args.pop - len(elites)):
                parent = top_half[rng.integers(len(top_half))]
                mask = rng.random(N_PARAMS) < 0.3
                children.append(parent + rng.normal(0, 0.2, N_PARAMS) * mask)
            pop = np.vstack([elites, children])
    except KeyboardInterrupt:
        print("\nstopped.")

    save(best)
    print(f"saved {MODEL_FILE}\n\ntest on 30 new matches vs the '{args.difficulty}' computer player")
    report(best, ai_speed)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evolve a Pong-playing neural network.")
    parser.add_argument("-d", "--difficulty", choices=DIFFICULTIES, default="normal",
                        help="the computer opponent to train against")
    parser.add_argument("--generations", type=int, default=150)
    parser.add_argument("--pop", type=int, default=50, help="networks per generation")
    parser.add_argument("--episodes", type=int, default=4, help="matches per network per generation")
    parser.add_argument("--seed", type=int, help="fix the random seed")
    parser.add_argument("--resume", action="store_true", help="continue from the saved model")
    parser.add_argument("--test", action="store_true", help="only test the saved model")
    args = parser.parse_args(argv)

    if args.test:
        print(f"test on 30 new matches vs the '{args.difficulty}' computer player")
        report(np.load(MODEL_FILE)["params"], DIFFICULTIES[args.difficulty])
        return 0
    return train(args)


if __name__ == "__main__":
    sys.exit(main())
