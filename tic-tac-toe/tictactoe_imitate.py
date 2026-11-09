#!/usr/bin/env python3
"""Teach the Tic-Tac-Toe network by imitation (numpy only, no other installs).

    python3 tictactoe_imitate.py            train, saves tictactoe_model.npz
    python3 tictactoe_imitate.py --test     check the saved model

Evolution got stuck at about 85 mistakes, so this version teaches instead of
mutating: it lists every position that can ever come up (both sides), asks the
minimax player which squares are perfect there, and trains the SAME network
(Brain in tictactoe_train.py) with back-propagation to pick one of those
squares. There is still no guard: the network never sees a rule, it only sees
examples, and "done" means the same strict test as before - zero losses and
zero illegal moves against EVERY possible opponent.

The model file has the same layout as the evolved one, so tictactoe_play.py
works unchanged.
"""

from __future__ import annotations

import argparse
import sys
import time
from functools import lru_cache

import numpy as np

from tic_tac_toe import TicTacToe
from tictactoe_train import MODEL_FILE, N_HID, N_IN, N_OUT, N_PARAMS, Brain, exhaustive, observe, other, report, save



@lru_cache(maxsize=None)
def value(board: tuple, to_move: str) -> int:
    """Perfect-play result for to_move: +1 win, 0 draw, -1 loss."""
    winner = TicTacToe.winner_of(list(board))
    if winner is not None:
        return 1 if winner == to_move else -1
    if TicTacToe.is_full(list(board)):
        return 0
    best = -1
    for i in TicTacToe.available_in(list(board)):
        child = list(board)
        child[i] = to_move
        best = max(best, -value(tuple(child), other(to_move)))
    return best


def perfect_squares(board: tuple, mark: str) -> list[int]:
    scored = []
    for i in TicTacToe.available_in(list(board)):
        child = list(board)
        child[i] = mark
        scored.append((-value(tuple(child), other(mark)), i))
    top = max(s for s, _ in scored)
    return [i for s, i in scored if s == top]


def build_dataset() -> tuple[np.ndarray, np.ndarray]:
    """Every position (either side to move) that can come up, with its perfect squares."""
    seen: dict = {}

    def walk(board: list, turn: str) -> None:
        key = tuple(board)
        if key in seen:
            return
        if TicTacToe.winner_of(board) is not None or TicTacToe.is_full(board):
            return
        seen[key] = turn
        for i in TicTacToe.available_in(board):
            board[i] = turn
            walk(board, other(turn))
            board[i] = None

    walk([None] * 9, "X")
    xs, ts = [], []
    for key, turn in seen.items():
        target = np.zeros(N_OUT)
        target[perfect_squares(key, turn)] = 1.0
        xs.append(observe(list(key), turn))
        ts.append(target / target.sum())
    return np.array(xs), np.array(ts)


def train(args) -> int:
    rng = np.random.default_rng(args.seed)
    x, target = build_dataset()
    print(f"{len(x)} positions to learn from\n")

    params = rng.normal(0, 0.3, N_PARAMS)
    if args.resume:
        params = np.load(MODEL_FILE)["params"].copy()
    m, v = np.zeros(N_PARAMS), np.zeros(N_PARAMS)

    a = N_HID * N_IN
    b = a + N_HID
    c = b + N_OUT * N_HID
    best = params.copy()
    best_flaws = None
    start = time.time()
    try:
        for step in range(1, args.steps + 1):
            w1 = params[:a].reshape(N_HID, N_IN)
            b1 = params[a:b]
            w2 = params[b:c].reshape(N_OUT, N_HID)
            b2 = params[c:c + N_OUT]
            w3 = params[c + N_OUT:].reshape(N_OUT, N_IN)

            hidden = np.tanh(x @ w1.T + b1)
            out = hidden @ w2.T + b2 + x @ w3.T
            out -= out.max(axis=1, keepdims=True)
            prob = np.exp(out)
            prob /= prob.sum(axis=1, keepdims=True)
            loss = -(target * np.log(prob + 1e-12)).sum() / len(x)

            d_out = (prob - target) / len(x)
            d_hidden = (d_out @ w2) * (1 - hidden ** 2)
            grad = np.concatenate([
                (d_hidden.T @ x).ravel(), d_hidden.sum(axis=0),
                (d_out.T @ hidden).ravel(), d_out.sum(axis=0),
                (d_out.T @ x).ravel(),
            ])
            m = 0.9 * m + 0.1 * grad
            v = 0.999 * v + 0.001 * grad ** 2
            params -= args.lr * (m / (1 - 0.9 ** step)) / (np.sqrt(v / (1 - 0.999 ** step)) + 1e-8)

            if step % args.check == 0 or step == args.steps:
                games, losses, illegal = exhaustive(Brain(params).act)
                flaws = losses + illegal
                print(f"step {step:5d}   loss {loss:.4f}   vs every opponent: "
                      f"{losses:3d} losses, {illegal:3d} illegal (of {games})   {time.time() - start:4.1f}s")
                if best_flaws is None or flaws < best_flaws:
                    best, best_flaws = params.copy(), flaws
                if flaws == 0:
                    print("\nnever loses, never cheats against every possible opponent - done.")
                    break
    except KeyboardInterrupt:
        print("\nstopped.")

    save(best)
    print(f"saved {MODEL_FILE}")
    report(best)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Teach the Tic-Tac-Toe network by imitation.")
    parser.add_argument("--steps", type=int, default=6000, help="training steps")
    parser.add_argument("--lr", type=float, default=0.01, help="Adam learning rate")
    parser.add_argument("--check", type=int, default=100, help="steps between strict tests")
    parser.add_argument("--seed", type=int, help="fix the random seed")
    parser.add_argument("--resume", action="store_true", help="continue from the saved model")
    parser.add_argument("--test", action="store_true", help="only test the saved model")
    args = parser.parse_args(argv)

    if args.test:
        report(np.load(MODEL_FILE)["params"])
        return 0
    return train(args)


if __name__ == "__main__":
    sys.exit(main())
