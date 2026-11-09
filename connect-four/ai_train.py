#!/usr/bin/env python3
"""Train the learned Connect Four AI (writes ``connect4_model.npz``).

    python3 ai_train.py                 # first run: stage 1 + 2; later runs: more stage-2 rounds
    python3 ai_train.py --fresh         # throw the old model away and start over
    python3 ai_train.py --imitate       # only stage 1
    python3 ai_train.py --improve       # only stage 2 (needs a model), e.g. --rounds 12
    python3 ai_train.py --test          # play the learned AI against easy / normal / hard

What is learned
    The network is a position evaluator: "how good is this position for the
    player to move?"  The learned AI = the alpha-beta search from ``ai.py``
    with this network judging the end of every line (see ``ai_net.py``).

Stage 1 - learn from a deeper search
    Many games are played by search bots (with random slips and random
    openings so the positions are varied).  Every position is shown to a
    depth-5 search, whose opinion (squashed into -1..1) is mixed with how the
    game really ended.  The network learns to predict that mix.

Stage 2 - learn from its own games
    The network now plays itself and the bots using its own evaluation, and
    the positions *it* reaches get labelled the same way and added to the data.
    Each round is benchmarked; the model is only saved when it is at least as
    good as the best one so far, so it never gets worse.

Needs only numpy.  Press Ctrl-C any time: the best model so far is kept.
"""

from __future__ import annotations

import argparse
import math
import multiprocessing as mp
import os
import random
import sys
import time
from typing import Callable, List, Optional, Tuple

import numpy as np

import ai
import ai_net
from ai import H, STRIDE, W, Connect4AI, has_won
from ai_net import FEATURES, ValueNet

Agent = Callable[[int, int, List[int]], int]

SCORE_SCALE = 150.0  # search score -> tanh(score / SCORE_SCALE)
OUTCOME_WEIGHT = 0.4  # target = 0.6 * search opinion + 0.4 * real result


# --------------------------------------------------------------------------- #
# Agents and the fast game loop (bitboards, no UI)
# --------------------------------------------------------------------------- #

def _legal(heights: List[int]) -> List[int]:
    return [c for c in range(W) if heights[c] < H]


class SearchAgent:
    """The hand-written alpha-beta AI at a fixed depth, with optional random slips."""

    def __init__(self, depth: int, eps: float, rng: random.Random) -> None:
        self.ai = Connect4AI("normal", seed=rng.randrange(1 << 30), depth=max(2, depth), budget=None)
        self.eps = eps
        self.rng = rng

    def __call__(self, cur: int, other: int, heights: List[int]) -> int:
        if self.eps and self.rng.random() < self.eps:
            return self.rng.choice(_legal(heights))
        return self.ai.choose_bits(cur, other, heights)


class LearnedAgent:
    """Search with the network as evaluator, fixed depth (fast, used for training/benchmarks)."""

    def __init__(self, net: ValueNet, depth: int, rng: random.Random, eps: float = 0.0) -> None:
        self.ai = Connect4AI("learned", seed=rng.randrange(1 << 30), depth=max(2, depth), budget=None, value_net=net)
        self.eps = eps
        self.rng = rng

    def __call__(self, cur: int, other: int, heights: List[int]) -> int:
        if self.eps and self.rng.random() < self.eps:
            return self.rng.choice(_legal(heights))
        return self.ai.choose_bits(cur, other, heights)


class LevelAgent:
    """One of the real in-game levels (easy / normal / hard / learned) with its real time budget."""

    def __init__(self, level: str, rng: random.Random) -> None:
        self.ai = Connect4AI(level, seed=rng.randrange(1 << 30))

    def __call__(self, cur: int, other: int, heights: List[int]) -> int:
        return self.ai.choose_bits(cur, other, heights)


class TacticalRandom:
    """Wins when it can, blocks when it must, avoids gifts, otherwise random."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng

    def __call__(self, cur: int, other: int, heights: List[int]) -> int:
        legal = _legal(heights)
        for c in legal:
            if has_won(cur | (1 << (heights[c] * STRIDE + c))):
                return c
        for c in legal:
            if has_won(other | (1 << (heights[c] * STRIDE + c))):
                return c
        safe = [
            c
            for c in legal
            if heights[c] + 1 >= H or not has_won(other | (1 << ((heights[c] + 1) * STRIDE + c)))
        ]
        return self.rng.choice(safe or legal)


def play(first: Agent, second: Agent, rng: random.Random, open_random: int = 0) -> Tuple[int, int]:
    """Play one game.  Returns ``(result, moves)``: +1 first wins, -1 second wins, 0 draw."""
    agents = (first, second)
    bits = [0, 0]
    heights = [0] * W
    turn = 0
    moves = 0
    while True:
        legal = _legal(heights)
        if not legal:
            return 0, moves
        if moves < open_random:
            col = rng.choice(legal)
        else:
            col = agents[turn](bits[turn], bits[1 - turn], heights)
        bits[turn] |= 1 << (heights[col] * STRIDE + col)
        heights[col] += 1
        moves += 1
        if has_won(bits[turn]):
            return (1 if turn == 0 else -1), moves
        turn ^= 1


# --------------------------------------------------------------------------- #
# Adam
# --------------------------------------------------------------------------- #

class Adam:
    def __init__(self, net: ValueNet, lr: float) -> None:
        self.net = net
        self.lr = lr
        self.t = 0
        self.m = {k: np.zeros_like(v) for k, v in net.p.items()}
        self.v = {k: np.zeros_like(v) for k, v in net.p.items()}

    def step(self, grads: dict, lr: Optional[float] = None, clip: float = 5.0) -> None:
        lr = self.lr if lr is None else lr
        norm = float(np.sqrt(sum(float((g * g).sum()) for g in grads.values())))
        scale = clip / norm if norm > clip else 1.0
        self.t += 1
        b1, b2, eps = 0.9, 0.999, 1e-8
        for k, g in grads.items():
            g = g * scale
            self.m[k] = b1 * self.m[k] + (1 - b1) * g
            self.v[k] = b2 * self.v[k] + (1 - b2) * g * g
            mh = self.m[k] / (1 - b1 ** self.t)
            vh = self.v[k] / (1 - b2 ** self.t)
            self.net.p[k] -= (lr * mh / (np.sqrt(vh) + eps)).astype(np.float32)


# --------------------------------------------------------------------------- #
# Data: positions labelled by a deeper search + the real result
# --------------------------------------------------------------------------- #

def _gen_worker(job):
    """Play ``n`` varied games, label every position.  Returns (features, targets)."""
    n, seed, params, depth, learned_depth = job
    rng = random.Random(seed)
    teacher = Connect4AI("normal", seed=seed, depth=depth, budget=None)
    net = ValueNet(params) if params else None
    xs: List[np.ndarray] = []
    ts: List[float] = []

    for _ in range(n):
        # Who plays each side?  With a network: mostly the network itself.
        kinds = []
        for _side in range(2):
            r = rng.random()
            if net is not None and r < 0.60:
                kinds.append("learned")
            elif r < 0.90:
                kinds.append("teacher")
            else:
                kinds.append("tactical")
        learner = LearnedAgent(net, learned_depth, rng, eps=0.06) if net is not None else None
        tactical = TacticalRandom(rng)

        bits = [0, 0]
        heights = [0] * W
        turn = 0
        moves = 0
        open_random = rng.choice((0, 1, 2, 3, 4, 5))
        record: List[Tuple[int, int, int, float]] = []  # (cur, other, mover, search value)
        winner = -1
        while True:
            legal = _legal(heights)
            if not legal:
                break
            cur, other = bits[turn], bits[1 - turn]
            best = teacher.choose_bits(cur, other, heights)
            value = math.tanh(teacher.last_score / SCORE_SCALE)
            record.append((cur, other, turn, value))
            kind = kinds[turn]
            if moves < open_random:
                col = rng.choice(legal)
            elif kind == "learned" and learner is not None:
                col = learner(cur, other, heights)
            elif kind == "tactical":
                col = tactical(cur, other, heights)
            else:
                col = rng.choice(legal) if rng.random() < 0.10 else best
            bits[turn] |= 1 << (heights[col] * STRIDE + col)
            heights[col] += 1
            moves += 1
            if has_won(bits[turn]):
                winner = turn
                break
            turn ^= 1

        for cur, other, mover, value in record:
            outcome = 0.0 if winner < 0 else (1.0 if winner == mover else -1.0)
            target = (1.0 - OUTCOME_WEIGHT) * value + OUTCOME_WEIGHT * outcome
            xs.append(ai_net.value_features(cur, other))
            ts.append(target)
            xs.append(ai_net.value_features(ai_net.mirror_bits(cur), ai_net.mirror_bits(other)))
            ts.append(target)

    return np.array(xs, dtype=np.float32).reshape(-1, FEATURES), np.array(ts, dtype=np.float32)


def generate_data(net: Optional[ValueNet], games: int, depth: int, learned_depth: int, workers: int, seed: int, label: str):
    chunk = 8
    params = {k: v.copy() for k, v in net.p.items()} if net is not None else None
    jobs = []
    left = games
    k = 0
    while left > 0:
        n = min(chunk, left)
        jobs.append((n, seed * 1000 + k, params, depth, learned_depth))
        left -= n
        k += 1
    xs, ts = [], []
    done = 0
    started = time.time()
    if workers > 1:
        pool = mp.Pool(workers)
        it = pool.imap_unordered(_gen_worker, jobs)
    else:
        pool = None
        it = map(_gen_worker, jobs)
    try:
        for x, t in it:
            xs.append(x)
            ts.append(t)
            done = min(games, done + chunk)
            rate = done / max(1e-6, time.time() - started)
            eta = (games - done) / max(1e-6, rate)
            print(f"\r    {label} {done}/{games}  ({rate:.1f}/s, ~{eta / 60:.1f} min left)   ", end="", flush=True)
    finally:
        if pool is not None:
            pool.terminate()
    print()
    return np.concatenate(xs), np.concatenate(ts)


def train_value(net: ValueNet, X: np.ndarray, T: np.ndarray, epochs: int, lr: float, seed: int) -> None:
    rng = np.random.default_rng(seed)
    order = rng.permutation(len(X))
    X, T = X[order], T[order]
    n_val = max(500, len(X) // 20)
    Xv, Tv = X[:n_val], T[:n_val]
    Xt, Tt = X[n_val:], T[n_val:]
    opt = Adam(net, lr)
    batch = 256
    steps_total = max(1, epochs * (len(Xt) // batch))
    step = 0

    def stats(xs: np.ndarray, ts: np.ndarray) -> Tuple[float, float]:
        out = np.tanh(net.forward(xs)[:, 0])
        mse = float(((out - ts) ** 2).mean())
        decisive = np.abs(ts) > 0.3
        agree = float((np.sign(out[decisive]) == np.sign(ts[decisive])).mean()) if decisive.any() else 0.0
        return mse, agree

    print(f"    {len(Xt)} training positions, {n_val} held out")
    for epoch in range(1, epochs + 1):
        perm = rng.permutation(len(Xt))
        for i in range(0, len(Xt) - batch + 1, batch):
            idx = perm[i : i + batch]
            xb, tb = Xt[idx], Tt[idx]
            z, acts = net.forward(xb, keep=True)
            out = np.tanh(z[:, 0])
            dz = (2.0 * (out - tb) * (1.0 - out * out) / batch)[:, None].astype(np.float32)
            frac = step / steps_total
            opt.step(net.backward(acts, dz), lr=lr * (0.1 + 0.9 * (1 - frac)))
            step += 1
        tr_mse, _ = stats(Xt[:5000], Tt[:5000])
        va_mse, va_agree = stats(Xv, Tv)
        print(
            f"    epoch {epoch}/{epochs}  train error {tr_mse:.3f}  held-out error {va_mse:.3f}  "
            f"right side of the game {va_agree * 100:.1f}%"
        )


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #

def match(make_a: Callable[[], Agent], make_b: Callable[[], Agent], games: int, rng: random.Random):
    """``games`` games, A and B alternate colours.  Returns (A wins, draws, B wins, avg moves)."""
    aw = dr = bw = 0
    total_moves = 0
    a, b = make_a(), make_b()
    for g in range(games):
        a_first = g % 2 == 0
        res, moves = play(a if a_first else b, b if a_first else a, rng, open_random=rng.choice((2, 2, 3)))
        total_moves += moves
        if res == 0:
            dr += 1
        elif (res == 1) == a_first:
            aw += 1
        else:
            bw += 1
    return aw, dr, bw, total_moves / max(1, games)


def quick_score(net: ValueNet, games: int, rng: random.Random, depth: int = 4) -> Tuple[float, str]:
    """Training-time benchmark: the learned search against fixed heuristic searches."""
    parts = []
    total = 0.0
    for opp_depth in (3, 4):
        aw, dr, bw, _ = match(
            lambda: LearnedAgent(net, depth, rng),
            lambda d=opp_depth: SearchAgent(d, 0.0, rng),
            games,
            rng,
        )
        total += (aw + 0.5 * dr) / games
        parts.append(f"vs depth {opp_depth}: {aw}W/{dr}D/{bw}L")
    return total / 2, "  ".join(parts)


def run_test(games: int, seed: int) -> None:
    net = ai_net.load_model()
    if net is None:
        print("No usable model yet - run  python3 ai_train.py --fresh  first.")
        return
    rng = random.Random(seed)
    print(f"Learned AI vs the search levels ({games} games each, colours alternate)")
    for level in ("easy", "normal", "hard"):
        started = time.time()
        aw, dr, bw, avg = match(lambda: LevelAgent("learned", rng), lambda lv=level: LevelAgent(lv, rng), games, rng)
        print(
            f"  vs {level:<6} {aw:>3}/{games} wins  {dr:>3} draws  {bw:>3} losses   "
            f"avg {avg:.1f} moves   ({time.time() - started:.0f}s)"
        )


# --------------------------------------------------------------------------- #
# Stage 2: learn from its own games
# --------------------------------------------------------------------------- #

def improve(
    net: ValueNet,
    X: Optional[np.ndarray],
    T: Optional[np.ndarray],
    rounds: int,
    games: int,
    epochs: int,
    depth: int,
    learned_depth: int,
    workers: int,
    eval_games: int,
    seed: int,
) -> None:
    rng = random.Random(seed)
    if X is None or T is None:
        base = max(400, games)
        print(f"  (no stage-1 data in memory: making {base} fresh games first)")
        X, T = generate_data(None, base, depth, learned_depth, workers, seed, "games")
    best_score, best_text = quick_score(net, eval_games, rng)
    print(f"  start: score {best_score * 100:.1f}%  ({best_text})   data: {len(X)} positions")
    best_params = {k: v.copy() for k, v in net.p.items()}
    try:
        for rnd in range(1, rounds + 1):
            print(f"  round {rnd}/{rounds}")
            nx, nt = generate_data(net, games, depth, learned_depth, workers, seed * 100 + rnd, "its own games")
            X = np.concatenate([X, nx])
            T = np.concatenate([T, nt])
            print(f"    +{len(nx)} positions (total {len(X)})")
            train_value(net, X, T, epochs, 5e-4, seed + rnd)
            score, text = quick_score(net, eval_games, rng)
            mark = ""
            if score >= best_score:
                best_score, best_text = score, text
                best_params = {k: v.copy() for k, v in net.p.items()}
                net.save(rounds=rnd, score=f"{score:.3f}")
                mark = "  <- saved"
            print(f"    score {score * 100:.1f}%  ({text})  best {best_score * 100:.1f}%{mark}")
    except KeyboardInterrupt:
        print("\n  stopped by Ctrl-C")
    net.p = {k: v.copy() for k, v in best_params.items()}
    net.save(score=f"{best_score:.3f}")
    print(f"  stage 2 done: best score {best_score * 100:.1f}%  ({best_text})")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description="Train the learned Connect Four AI.")
    ap.add_argument("--fresh", action="store_true", help="ignore any saved model and start from scratch")
    ap.add_argument("--imitate", action="store_true", help="run stage 1 only")
    ap.add_argument("--improve", action="store_true", help="run stage 2 only (continue from the saved model)")
    ap.add_argument("--test", action="store_true", help="play the learned AI against easy / normal / hard")
    ap.add_argument("--games", type=int, default=2500, help="stage 1: games (default 2500)")
    ap.add_argument("--depth", type=int, default=5, help="teacher search depth (default 5)")
    ap.add_argument("--epochs", type=int, default=12, help="stage 1: training epochs (default 12)")
    ap.add_argument("--rounds", type=int, default=6, help="stage 2: rounds (default 6)")
    ap.add_argument("--round-games", type=int, default=500, help="stage 2: games per round (default 500)")
    ap.add_argument("--round-epochs", type=int, default=5, help="stage 2: training epochs per round (default 5)")
    ap.add_argument("--learned-depth", type=int, default=3, help="search depth of the network while it plays training games")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2)), help="CPU processes for making data")
    ap.add_argument("--eval-games", type=int, default=24, help="benchmark games per opponent (default 24)")
    ap.add_argument("--test-games", type=int, default=30, help="--test: games per level (default 30)")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    if args.test:
        run_test(args.test_games, args.seed)
        return 0

    net: Optional[ValueNet] = None
    if os.path.exists(ai_net.MODEL_PATH) and not args.fresh:
        try:
            net = ValueNet.load()
            print(f"Loaded {os.path.basename(ai_net.MODEL_PATH)}")
        except Exception as exc:
            print(f"Could not use the old model ({exc}); starting fresh")

    explicit = args.imitate or args.improve
    do_stage1 = args.imitate or net is None
    do_stage2 = args.improve or not explicit
    if args.improve and net is None:
        print("--improve needs a model: run  python3 ai_train.py  first.")
        return 1

    started = time.time()
    X = T = None
    if do_stage1:
        net = ValueNet(seed=args.seed)
        print(f"Stage 1 - learning from a depth-{args.depth} search: {args.games} games, {args.workers} process(es)")
        X, T = generate_data(None, args.games, args.depth, args.learned_depth, args.workers, args.seed, "games")
        print(f"    {len(X)} labelled positions (with mirrored copies)")
        train_value(net, X, T, args.epochs, 1e-3, args.seed)
        net.save(stage1_games=args.games, stage1_depth=args.depth)
        print(f"    saved {os.path.basename(ai_net.MODEL_PATH)}")
    assert net is not None

    if do_stage2:
        print(f"Stage 2 - learning from its own games: {args.rounds} rounds x {args.round_games} games")
        improve(net, X, T, args.rounds, args.round_games, args.round_epochs, args.depth,
                args.learned_depth, args.workers, args.eval_games, args.seed + 2)

    print(f"\nDone in {(time.time() - started) / 60:.1f} min.  Check it:  python3 ai_train.py --test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
