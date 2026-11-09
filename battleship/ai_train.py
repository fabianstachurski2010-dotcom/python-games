#!/usr/bin/env python3
"""Train the learned Battleship AI (numpy only).  Run it on your own machine.

    python3 ai_train.py                 # train (continues from the saved model)
    python3 ai_train.py --fresh         # start from a new random network
    python3 ai_train.py --minutes 120   # stop after about two hours
    python3 ai_train.py --test          # no training: play real games vs easy/normal/hard

Two stages, both automatic:

  1. IMITATE   The network watches the ``hard`` probability-density solver play
               and learns to predict its shot distribution (DAgger style: the
               states come from the student's own games, the answers from the
               teacher).
  2. SELF-PLAY The network plays games on its own, fires by sampling from its
               scores, and is rewarded for sinking the fleet in fewer shots
               (REINFORCE).  A little imitation is mixed in so it does not
               drift away from sensible play.

There is NO legality guard in training: shooting an already-shot cell wastes a
shot, so the network has to learn that by itself.  The progress line shows
``illegal`` (how often its top choice was a used cell) — it should go to ~0.

A third, optional job trains the AI to PLACE its fleet too:

    python3 ai_train.py --place         # ~400 iterations; needs no shooter model

  The placement network drops the five ships one by one (sampling from its own
  probabilities, so the layout changes every game) and is rewarded when the
  opponent shooter - the hard solver, and your trained shooter if you have one -
  needs MORE shots to sink it.  It is saved as battleship_place_model.npz; the
  learned AI uses it automatically when the file exists.

Success metric (same idea as the snake AI): average shots needed to sink the
whole fleet over 200 fixed games, compared with a random baseline and with the
hard solver.  Lower is better.  The best model so far is saved to
``battleship_model.npz`` whenever that number improves; Ctrl-C is always safe.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import engine  # noqa: E402
from ai import HIT_WEIGHT, PLACEMENTS_BY_SIZE  # noqa: E402
from ai_net import (CELLS, MODEL_PATH, N_SHIPS, PLACE_ACTIONS,  # noqa: E402
                    PLACE_MODEL_PATH, PLACE_SIZES, PLACE_TABLES, SHIP_SIZES,
                    SIZE, PolicyNet, build_features, masked_softmax,
                    place_features, place_legal)

STEP_CAP = 400            # safety stop per game (a perfect game needs <= 100)
SHARPEN = 2.0             # teacher target = density ** SHARPEN, normalised
BUFFER_MAX = 150_000      # imitation samples kept (stored compactly)
EVAL_GAMES = 200
EVAL_SEED = 20240601


# --------------------------------------------------------------------------- #
# Vectorised battleship (N games at once, public + hidden state)
# --------------------------------------------------------------------------- #

class VecEnv:
    """N independent solitaire games: sink the hidden fleet in few shots.

    (Turn order does not change how many shots a fleet needs, so one board per
    game is enough to train a shooter.)
    """

    def __init__(self, n: int):
        self.n = n
        self.shots = np.zeros((n, CELLS), dtype=np.int8)       # 0 / 1 miss / 2 hit
        self.sunk = np.zeros((n, CELLS), dtype=bool)
        self.remaining = np.ones((n, N_SHIPS), dtype=np.float32)
        self.ship_id = np.full((n, CELLS), -1, dtype=np.int8)
        self.hits = np.zeros((n, N_SHIPS), dtype=np.int64)
        self.done = np.zeros(n, dtype=bool)
        self.steps = np.zeros(n, dtype=np.int64)
        self.illegal = np.zeros(n, dtype=np.int64)

    def reset(self, rngs: list[random.Random]) -> None:
        self.shots[:] = 0
        self.sunk[:] = False
        self.remaining[:] = 1.0
        self.ship_id[:] = -1
        self.hits[:] = 0
        self.done[:] = False
        self.steps[:] = 0
        self.illegal[:] = 0
        for i, rng in enumerate(rngs):
            for sid, ship in enumerate(engine.random_fleet(rng)):
                for r, c in ship["cells"]:
                    self.ship_id[i, r * SIZE + c] = sid

    def reset_fleets(self, ship_id: np.ndarray) -> None:
        """Start fresh games on given hidden fleets ((n, 100) ids, -1 = water)."""
        self.reset([])
        self.ship_id[:] = ship_id

    def features(self, idx: np.ndarray | None = None) -> np.ndarray:
        sel = slice(None) if idx is None else idx
        shots = self.shots[sel]
        n = shots.shape[0]
        return build_features(shots.reshape(n, SIZE, SIZE),
                              self.sunk[sel].reshape(n, SIZE, SIZE),
                              self.remaining[sel])

    def step(self, actions: np.ndarray) -> None:
        """Fire ``actions[i]`` in every game that is not done yet."""
        idx = np.arange(self.n)
        active = ~self.done
        legal = active & (self.shots[idx, actions] == 0)
        wasted = active & ~legal
        self.illegal[wasted] += 1
        sid = self.ship_id[idx, actions]
        hit = legal & (sid >= 0)
        miss = legal & (sid < 0)
        self.shots[idx[miss], actions[miss]] = 1
        self.shots[idx[hit], actions[hit]] = 2
        hi, hs = idx[hit], sid[hit].astype(np.int64)
        np.add.at(self.hits, (hi, hs), 1)
        newly = self.hits[hi, hs] == SHIP_SIZES[hs]
        for n, s in zip(hi[newly], hs[newly]):
            self.sunk[n, self.ship_id[n] == s] = True
            self.remaining[n, s] = 0.0
        self.steps[active] += 1
        self.done |= self.remaining.sum(axis=1) == 0
        self.done |= self.steps >= STEP_CAP


# --------------------------------------------------------------------------- #
# Teacher: the ``hard`` density solver, vectorised (same maths as ai.py)
# --------------------------------------------------------------------------- #

def _placement_matrices() -> dict[int, np.ndarray]:
    mats = {}
    for size, placements in PLACEMENTS_BY_SIZE.items():
        m = np.zeros((len(placements), CELLS), dtype=np.float32)
        for p, (_mask, cells) in enumerate(placements):
            for r, c in cells:
                m[p, r * SIZE + c] = 1.0
        mats[size] = m
    return mats


MASKS = _placement_matrices()


def teacher_density(shots: np.ndarray, sunk: np.ndarray,
                    remaining: np.ndarray) -> np.ndarray:
    """(N, 100) density of the hard AI (0 on cells that cannot be shot)."""
    blocked = ((shots == 1) | sunk).astype(np.float32)
    hits = ((shots == 2) & ~sunk).astype(np.float32)
    dens = np.zeros((shots.shape[0], CELLS), dtype=np.float32)
    for i, size in enumerate(SHIP_SIZES):
        m = MASKS[int(size)]
        valid = (blocked @ m.T) == 0
        cov = hits @ m.T
        weight = (1.0 + HIT_WEIGHT * cov * cov) * valid * remaining[:, i:i + 1]
        dens += weight @ m
    dens *= (shots == 0)
    return dens


def teacher_target(dens: np.ndarray, shots: np.ndarray) -> np.ndarray:
    """Soft answer distribution over unshot cells (sharpened density)."""
    t = dens ** SHARPEN
    empty = t.sum(axis=1) <= 0
    if empty.any():                      # never happens in practice; stay safe
        t[empty] = (shots[empty] == 0).astype(np.float32)
    return t / t.sum(axis=1, keepdims=True)


# --------------------------------------------------------------------------- #
# Small helpers: softmax, Adam
# --------------------------------------------------------------------------- #

def softmax(z: np.ndarray) -> np.ndarray:
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)


def sample_rows(p: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    cum = p.cumsum(axis=1)
    u = rng.random((p.shape[0], 1)) * cum[:, -1:]
    return np.minimum((cum < u).sum(axis=1), p.shape[1] - 1)


class Adam:
    def __init__(self, net: PolicyNet, lr: float):
        self.lr = lr
        self.m = {k: np.zeros_like(v) for k, v in net.p.items()}
        self.v = {k: np.zeros_like(v) for k, v in net.p.items()}
        self.t = 0

    def apply(self, net: PolicyNet, grads: dict[str, np.ndarray],
              clip: float = 5.0) -> None:
        norm = np.sqrt(sum(float((g * g).sum()) for g in grads.values()))
        scale = min(1.0, clip / (norm + 1e-8))
        self.t += 1
        b1, b2 = 0.9, 0.999
        for k, g in grads.items():
            g = g * scale
            self.m[k] = b1 * self.m[k] + (1 - b1) * g
            self.v[k] = b2 * self.v[k] + (1 - b2) * g * g
            mh = self.m[k] / (1 - b1 ** self.t)
            vh = self.v[k] / (1 - b2 ** self.t)
            net.p[k] -= (self.lr * mh / (np.sqrt(vh) + 1e-8)).astype(np.float32)


# --------------------------------------------------------------------------- #
# Replay buffer of teacher-labelled states (compact: raw state, not features)
# --------------------------------------------------------------------------- #

class Buffer:
    def __init__(self, cap: int):
        self.cap = cap
        self.shots = np.zeros((cap, CELLS), dtype=np.int8)
        self.sunk = np.zeros((cap, CELLS), dtype=bool)
        self.remaining = np.zeros((cap, N_SHIPS), dtype=np.float32)
        self.target = np.zeros((cap, CELLS), dtype=np.float16)
        self.size = 0
        self.pos = 0

    def add(self, shots, sunk, remaining, target) -> None:
        for i in range(shots.shape[0]):
            p = self.pos
            self.shots[p], self.sunk[p] = shots[i], sunk[i]
            self.remaining[p], self.target[p] = remaining[i], target[i]
            self.pos = (p + 1) % self.cap
            self.size = min(self.size + 1, self.cap)

    def sample(self, n: int, rng: np.random.Generator):
        idx = rng.integers(0, self.size, size=n)
        feats = build_features(self.shots[idx].reshape(n, SIZE, SIZE),
                               self.sunk[idx].reshape(n, SIZE, SIZE),
                               self.remaining[idx])
        return feats, self.target[idx].astype(np.float32)


def supervised_grads(net: PolicyNet, x: np.ndarray, target: np.ndarray):
    """Cross-entropy against a soft target; returns (grads, loss)."""
    logits, cache = net.forward(x, cache=True)
    p = softmax(logits)
    loss = float(-(target * np.log(p + 1e-9)).sum(axis=1).mean())
    return net.backward(cache, (p - target) / x.shape[0]), loss


# --------------------------------------------------------------------------- #
# Evaluation (fixed games, greedy play, no guard)
# --------------------------------------------------------------------------- #

def _eval_rngs(n: int) -> list[random.Random]:
    return [random.Random(EVAL_SEED + i) for i in range(n)]


def play_greedy(env: VecEnv, mode: str, net: PolicyNet | None = None,
                rng: np.random.Generator | None = None) -> int:
    """Play every game in *env* to the end; returns the count of illegal picks.

    mode: 'net' | 'teacher' | 'teacher_sample' | 'random'.  For 'net' the top-scoring cell is fired
    as is; if it was already shot that costs one wasted shot (and counts as
    illegal) and the network's best *unshot* cell is fired instead, so a game
    can never loop forever.
    """
    idx = np.arange(env.n)
    illegal_total = 0
    while not env.done.all():
        if mode == "net":
            scores = net.forward(env.features())
            act = scores.argmax(axis=1)
            bad = (env.shots[idx, act] != 0) & ~env.done
            illegal_total += int(bad.sum())
            if bad.any():
                # Wasted shot (+1), then the network's best unshot cell is fired.
                masked = np.where(env.shots != 0, -np.inf, scores)
                act = np.where(bad, masked.argmax(axis=1), act)
                env.step(act)
                env.steps[bad] += 1
                continue
        elif mode == "teacher":
            dens = teacher_density(env.shots, env.sunk, env.remaining)
            act = dens.argmax(axis=1)
        elif mode == "teacher_sample":
            # Same solver but it samples from its own probabilities, so a layout
            # cannot win by hiding in one fixed blind spot of a deterministic AI.
            dens = teacher_density(env.shots, env.sunk, env.remaining)
            act = sample_rows(teacher_target(dens, env.shots), rng)
        else:
            free = (env.shots == 0).astype(np.float32)
            act = sample_rows(free, rng)
        env.step(act)
    return illegal_total


def evaluate(net: PolicyNet | None, mode: str = "net", n: int = EVAL_GAMES):
    """Average shots to sink the fleet (random fleets).  Returns (avg, illegal rate)."""
    env = VecEnv(n)
    env.reset(_eval_rngs(n))
    rng = np.random.default_rng(EVAL_SEED)
    illegal_total = play_greedy(env, mode, net, rng)
    return float(env.steps.mean()), illegal_total / max(1, int(env.steps.sum()))


# --------------------------------------------------------------------------- #
# Stage 1 — imitation (DAgger)
# --------------------------------------------------------------------------- #

def collect_imitation(net: PolicyNet, buf: Buffer, games: int, beta: float,
                      rng: np.random.Generator, pyrng: random.Random) -> int:
    """Play *games* with a teacher/student mix; label every state with the teacher."""
    env = VecEnv(games)
    env.reset([random.Random(pyrng.getrandbits(32)) for _ in range(games)])
    added = 0
    fresh = np.ones(games, dtype=bool)         # state changed since last record
    while not env.done.all():
        live = ~env.done
        rec = live & fresh
        dens = teacher_density(env.shots, env.sunk, env.remaining)
        target = teacher_target(dens, env.shots)
        if rec.any():
            buf.add(env.shots[rec], env.sunk[rec], env.remaining[rec], target[rec])
            added += int(rec.sum())
        use_teacher = rng.random(games) < beta
        probs = softmax(net.forward(env.features()))
        student = sample_rows(probs, rng)
        teach = sample_rows(target, rng)
        act = np.where(use_teacher, teach, student)
        shots_before = env.shots.sum(axis=1)
        env.step(act)
        fresh = env.shots.sum(axis=1) != shots_before
    return added


def stage_imitate(net: PolicyNet, iters: int, state: dict) -> None:
    buf: Buffer = state["buf"]
    rng, pyrng = state["rng"], state["pyrng"]
    opt = Adam(net, lr=1e-3)
    for it in range(1, iters + 1):
        if state["stop"]():
            return
        beta = max(0.15, 1.0 - 0.85 * it / max(1, iters * 0.6))
        added = collect_imitation(net, buf, 96, beta, rng, pyrng)
        losses = []
        for _ in range(max(10, added // 256)):
            x, t = buf.sample(512, rng)
            grads, loss = supervised_grads(net, x, t)
            opt.apply(net, grads)
            losses.append(loss)
        state["samples"] += added
        if it % state["every"] == 0 or it == iters:
            report(state, "imitate", it, iters, loss=float(np.mean(losses)),
                   extra=f"teacher-mix {beta:.2f}")


# --------------------------------------------------------------------------- #
# Stage 2 — self-play (REINFORCE) with a small imitation anchor
# --------------------------------------------------------------------------- #

def stage_selfplay(net: PolicyNet, iters: int, state: dict) -> None:
    buf: Buffer = state["buf"]
    rng, pyrng = state["rng"], state["pyrng"]
    opt = Adam(net, lr=2e-4)
    games = 128
    for it in range(1, iters + 1):
        if state["stop"]():
            return
        env = VecEnv(games)
        env.reset([random.Random(pyrng.getrandbits(32)) for _ in range(games)])
        xs, acts, gids, ts = [], [], [], []
        t = 0
        while not env.done.all():
            live = np.flatnonzero(~env.done)
            feats = env.features(live)
            probs = softmax(net.forward(feats))
            act_live = sample_rows(probs, rng)
            xs.append(feats)
            acts.append(act_live)
            gids.append(live)
            ts.append(np.full(live.size, t))
            full = np.zeros(games, dtype=np.int64)
            full[live] = act_live
            env.step(full)
            t += 1
        total = env.steps.astype(np.float64)
        x = np.concatenate(xs)
        a = np.concatenate(acts)
        g = np.concatenate(gids)
        tt = np.concatenate(ts)
        cost = total[g] - tt                         # shots still to pay from here
        base = np.zeros(t)
        for k in range(t):
            sel = tt == k
            if sel.any():
                base[k] = cost[sel].mean()
        adv = base[tt] - cost                        # positive = better than usual
        adv /= (adv.std() + 1e-6)

        logits, cache = net.forward(x, cache=True)
        p = softmax(logits)
        onehot = np.zeros_like(p)
        onehot[np.arange(a.size), a] = 1.0
        d = (p - onehot) * adv[:, None] / a.size
        grads = net.backward(cache, d)
        if buf.size >= 512:                          # anchor: stay close to the teacher
            bx, bt = buf.sample(512, rng)
            sgrads, _ = supervised_grads(net, bx, bt)
            for k in grads:
                grads[k] = grads[k] + 0.5 * sgrads[k]
        opt.apply(net, grads)
        state["samples"] += int(a.size)
        if it % state["every"] == 0 or it == iters:
            report(state, "self-play", it, iters,
                   extra=f"train-games avg {total.mean():.1f} shots")


# --------------------------------------------------------------------------- #
# Placement training (REINFORCE): hide the fleet so the opponent needs more shots
# --------------------------------------------------------------------------- #

PLACE_EVAL_GAMES = 400
PLACE_EVAL_SEED = 777
MIN_BITS = 4.0        # EVERY ship must keep >= 2**4 = 16 effective positions to be saved
TARGET_BITS = 5.0     # training steers each ship's variety toward this


def sample_fleets(net: PolicyNet, n: int, rng: np.random.Generator,
                  greedy: bool = False):
    """Place *n* fleets with the placement network.

    Returns (ship_id (n, 100), per-ship features, legal masks, actions, entropies
    (5,) in nats, one per ship — 0 means that ship is always put in the same spot).
    """
    occupied = np.zeros((n, CELLS), dtype=np.float32)
    ship_id = np.full((n, CELLS), -1, dtype=np.int8)
    rows = np.arange(n)
    xs, legals, acts = [], [], []
    entropies = np.zeros(len(SHIP_SIZES))
    for index, size in enumerate(SHIP_SIZES):
        size = int(size)
        x = place_features(occupied, index)
        legal = place_legal(occupied, size)
        probs = masked_softmax(net.forward(x), legal)
        entropies[index] = float(-(probs * np.log(probs + 1e-12)).sum(axis=1).mean())
        action = probs.argmax(axis=1) if greedy else sample_rows(probs, rng)
        mat, _ = PLACE_TABLES[size]
        cells = np.argsort(-mat[action], axis=1, kind="stable")[:, :size]
        if not legal[rows, action].all():            # can never happen; never reward a cheat
            raise RuntimeError("placement network picked an illegal placement")
        ship_id[rows[:, None], cells] = index
        occupied += mat[action]
        xs.append(x)
        legals.append(legal)
        acts.append(action)
    return ship_id, xs, legals, acts, entropies


def random_symmetry(ship_id: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Rotate/mirror every game's fleet by a random one of the 8 board symmetries.

    The game does exactly this to the learned layout (PlacementPolicy.place), so
    training and evaluation score the same thing the opponent will face.
    """
    n = ship_id.shape[0]
    grids = ship_id.reshape(n, SIZE, SIZE)
    out = np.empty_like(grids)
    which = rng.integers(0, 8, size=n)
    for t in range(8):
        sel = which == t
        if not sel.any():
            continue
        g = grids[sel]
        if t & 4:
            g = g.transpose(0, 2, 1)
        if t & 1:
            g = g[:, ::-1, :]
        if t & 2:
            g = g[:, :, ::-1]
        out[sel] = g
    return out.reshape(n, CELLS)


def random_ship_ids(n: int, pyrng: random.Random) -> np.ndarray:
    env = VecEnv(n)
    env.reset([random.Random(pyrng.getrandbits(32)) for _ in range(n)])
    return env.ship_id.copy()


def shots_needed(ship_id: np.ndarray, shooter: PolicyNet | None,
                 rng: np.random.Generator) -> np.ndarray:
    """Shots the opponent needs on these fleets.

    The hard solver plays with randomness (it samples its shots).  With a trained
    *shooter* the second half of the games are played by that network instead, so
    the placement cannot memorise one opponent's habits.
    """
    n = ship_id.shape[0]
    out = np.zeros(n)
    halves = [(slice(0, n), "teacher_sample")] if shooter is None else [
        (slice(0, n // 2), "teacher_sample"), (slice(n // 2, n), "net")]
    for sel, mode in halves:
        env = VecEnv(ship_id[sel].shape[0])
        env.reset_fleets(ship_id[sel])
        play_greedy(env, mode, shooter, rng)
        out[sel] = env.steps
    return out


def evaluate_placement(pnet: PolicyNet | None, shooter: PolicyNet | None,
                       n: int = PLACE_EVAL_GAMES):
    """(avg shots vs hard solver, avg vs trained shooter or None, weakest-ship entropy).

    pnet=None means plain random fleets (the baseline).  Higher = harder to find.
    Learned fleets are rotated/mirrored at random, exactly as in a real game.
    """
    rng = np.random.default_rng(PLACE_EVAL_SEED)
    if pnet is None:
        ship_id = random_ship_ids(n, random.Random(PLACE_EVAL_SEED))
        entropy = float("nan")
    else:
        ship_id, _x, _l, _a, entropies = sample_fleets(pnet, n, rng)
        ship_id = random_symmetry(ship_id, rng)
        entropy = float(entropies.min())            # the most predictable ship decides
    env = VecEnv(n)
    env.reset_fleets(ship_id)
    play_greedy(env, "teacher")
    vs_teacher = float(env.steps.mean())
    vs_net = None
    if shooter is not None:
        env = VecEnv(n)
        env.reset_fleets(ship_id)
        play_greedy(env, "net", shooter, rng)
        vs_net = float(env.steps.mean())
    return vs_teacher, vs_net, entropy


def place_score(vs_teacher: float, vs_net: float | None, entropy: float = 99.0) -> float:
    """Higher = better hidden.  If ANY ship is nearly always put in the same spot
    (variety below MIN_BITS) the model scores -inf: predictable, not good."""
    if entropy / np.log(2) < MIN_BITS:
        return float("-inf")
    return vs_teacher if vs_net is None else (vs_teacher + vs_net) / 2


def stage_place(pnet: PolicyNet, shooter: PolicyNet | None, iters: int,
                state: dict) -> None:
    rng, pyrng = state["rng"], state["pyrng"]
    opt = Adam(pnet, lr=5e-4)
    games = 256
    # One entropy bonus per ship, each adapted on its own: a ship that starts to be
    # placed in the same spot (the small ones do first) gets pushed back harder.
    betas = np.full(len(SHIP_SIZES), 0.02)
    for it in range(1, iters + 1):
        if state["stop"]():
            return
        ship_id, xs, legals, acts, entropies = sample_fleets(pnet, games, rng)
        low = entropies / np.log(2) < TARGET_BITS
        betas = np.where(low, np.minimum(betas * 1.15, 3.0), np.maximum(betas * 0.95, 0.02))
        steps = shots_needed(random_symmetry(ship_id, rng), shooter, rng)
        adv = (steps - steps.mean()) / (steps.std() + 1e-6)   # more shots = better
        x = np.concatenate(xs)
        legal = np.concatenate(legals)
        a = np.concatenate(acts)
        adv_all = np.tile(adv, len(xs))
        logits, cache = pnet.forward(x, cache=True)
        p = masked_softmax(logits, legal)
        onehot = np.zeros_like(p)
        onehot[np.arange(a.size), a] = 1.0
        ent = -(p * np.log(p + 1e-12)).sum(axis=1)
        d = (p - onehot) * adv_all[:, None]
        beta_all = np.repeat(betas, games)
        d += beta_all[:, None] * p * (np.log(p + 1e-12) + ent[:, None])
        opt.apply(pnet, pnet.backward(cache, d / a.size))
        if it % state["every"] == 0 or it == iters:
            report_place(state, it, iters, float(steps.mean()))


def report_place(state: dict, it: int, iters: int, train_avg: float) -> None:
    pnet: PolicyNet = state["pnet"]
    shooter = state["shooter"]
    vt, vn, ent = evaluate_placement(pnet, shooter)
    score = place_score(vt, vn, ent)
    mark = ""
    if score > state["best"]:
        state["best"] = score
        pnet.save(PLACE_MODEL_PATH, meta={"vs_teacher": vt})
        mark = "  <- saved best"
    elif score == float("-inf"):
        mark = "  (too predictable - not saved)"
    elapsed = (time.time() - state["t0"]) / 60
    line = (f"[placement {it:4d}/{iters}] {elapsed:5.1f} min  | opponent needs "
            f"{vt:5.1f} shots (hard solver)")
    if vn is not None:
        line += f", {vn:5.1f} (your shooter)"
    line += f"  | weakest-ship variety {ent / np.log(2):.1f} bits (keep >= {MIN_BITS:.0f}){mark}"
    print(line, flush=True)


def train_placement(args, rng: np.random.Generator, pyrng: random.Random) -> int:
    shooter = None
    if os.path.isfile(MODEL_PATH):
        try:
            shooter = PolicyNet.load(MODEL_PATH)
            print("opponents: hard solver + your trained shooter")
        except Exception as exc:  # noqa: BLE001
            print(f"could not read the shooter model ({exc}); using the hard solver only")
    else:
        print("opponents: hard solver only (no trained shooter found yet)")

    base_t, base_n, _ = evaluate_placement(None, shooter)
    base = f"baseline, random fleets: opponent needs {base_t:.1f} shots (hard solver)"
    if base_n is not None:
        base += f", {base_n:.1f} (your shooter)"
    print(base + "   <- the placement network aims to beat this (higher is better)")

    saved_score = float("-inf")
    saved = None
    if os.path.isfile(PLACE_MODEL_PATH):
        try:
            saved = PolicyNet.load(PLACE_MODEL_PATH, PLACE_SIZES[0])
            vt, vn, ent = evaluate_placement(saved, shooter)
            saved_score = place_score(vt, vn, ent)
            if saved_score == float("-inf"):
                print(f"the saved placement model is too predictable (its weakest ship has "
                      f"only {ent / np.log(2):.1f} bits of variety) - it will be replaced")
                saved = None
        except Exception as exc:  # noqa: BLE001
            print(f"could not read the old placement model ({exc}); it will be replaced")
    if saved is not None and not args.fresh:
        pnet = saved.copy()
        print(f"continuing from {os.path.basename(PLACE_MODEL_PATH)}")
    else:
        pnet = PolicyNet.fresh(rng, PLACE_SIZES)
        print("starting from a new random placement network")
        if saved is not None:
            print("the saved placement model is only replaced if the new one beats it")
    print()

    t0 = time.time()
    deadline = t0 + args.minutes * 60 if args.minutes > 0 else None
    state = {"pnet": pnet, "shooter": shooter, "rng": rng, "pyrng": pyrng,
             "t0": t0, "every": max(1, args.every), "best": saved_score,
             "stop": (lambda: deadline is not None and time.time() > deadline)}
    try:
        stage_place(pnet, shooter, args.place, state)
    except KeyboardInterrupt:
        print("\nstopped by you (Ctrl-C)")
    vt, vn, ent = evaluate_placement(pnet, shooter)
    if place_score(vt, vn, ent) > state["best"]:
        state["best"] = place_score(vt, vn, ent)
        pnet.save(PLACE_MODEL_PATH, meta={"vs_teacher": vt})
    if os.path.isfile(PLACE_MODEL_PATH):
        print(f"\nplacement model saved -> {os.path.basename(PLACE_MODEL_PATH)}  "
              "(the learned AI now places its own fleet)")
    else:
        print("\nno placement model saved: nothing beat the starting point")
    print("next: python3 ai_train.py --test")
    return 0


# --------------------------------------------------------------------------- #
# Progress + checkpointing
# --------------------------------------------------------------------------- #

def report(state: dict, stage: str, it: int, iters: int, loss: float | None = None,
           extra: str = "") -> None:
    net: PolicyNet = state["net"]
    elapsed = time.time() - state["t0"]
    line = f"[{stage:9s} {it:4d}/{iters}] {elapsed / 60:5.1f} min"
    if loss is not None:
        line += f"  loss {loss:.3f}"
    avg, illegal = evaluate(net)
    mark = ""
    if avg < state["best"]:
        state["best"] = avg
        net.save(MODEL_PATH, meta={"avg_shots": avg, "illegal": illegal})
        mark = "  <- saved best"
    line += f"  | eval {avg:5.1f} shots  illegal {illegal:5.1%}{mark}"
    if extra:
        line += f"  | {extra}"
    print(line, flush=True)


def baselines() -> None:
    rand, _ = evaluate(None, "random")
    teach, _ = evaluate(None, "teacher")
    print(f"baselines over {EVAL_GAMES} fixed games (avg shots to sink the fleet):")
    print(f"  random firing : {rand:5.1f}")
    print(f"  hard solver   : {teach:5.1f}   <- the teacher; the network aims to match or beat it")


# --------------------------------------------------------------------------- #
# Real-engine test: learned AI vs the built-in bots
# --------------------------------------------------------------------------- #

def head_to_head(games: int = 100) -> int:
    from ai import BattleshipAI
    try:
        BattleshipAI("learned")
    except Exception as exc:  # noqa: BLE001
        print(f"cannot load the model: {exc}")
        return 1
    placing = "its own learned placement" if BattleshipAI("learned").placement_learned \
        else "random placement (no placement model yet)"
    print(f"learned AI vs built-in bots — {games} real games each (alternating who starts)")
    print(f"learned AI fleet: {placing}")
    for bot in ("easy", "normal", "hard"):
        wins = shots = illegal = 0
        for seed in range(games):
            game = engine.Game()
            ais = [BattleshipAI("learned"), BattleshipAI(bot, seed=seed)]
            game.place_fleet(0, ais[0].choose_placement())   # learned placement if trained
            game.place_fleet(1, ais[1].choose_placement())
            starter = seed % 2
            game.start_battle(starter)
            while game.phase == "battle":
                player = game.turn
                r, c = ais[player].choose_attack(game.boards[1 - player])
                game.fire(player, r, c)
            wins += game.winner == 0
            shots += game.shots_fired[0]
            illegal += ais[0]._policy.illegal
        print(f"  vs {bot:<6}: learned wins {wins:3d}/{games}  "
              f"avg shots {shots / games:5.1f}  illegal picks {illegal}")
    if os.path.isfile(PLACE_MODEL_PATH):
        try:
            pnet = PolicyNet.load(PLACE_MODEL_PATH, PLACE_SIZES[0])
            shooter = PolicyNet.load(MODEL_PATH)
            rt, rn, _ = evaluate_placement(None, shooter)
            lt, ln, ent = evaluate_placement(pnet, shooter)
            print("\nplacement (shots the opponent needs to sink the fleet, higher = better hidden):")
            print(f"  random fleets : {rt:5.1f} vs hard solver, {rn:5.1f} vs your shooter")
            print(f"  learned fleets: {lt:5.1f} vs hard solver, {ln:5.1f} vs your shooter"
                  f"   (weakest-ship variety {ent / np.log(2):.1f} bits)")
            if ent / np.log(2) < MIN_BITS:
                print("  WARNING: at least one ship is nearly always put in the same spot "
                      f"(variety below {MIN_BITS:.0f} bits), so those numbers are inflated and an "
                      "opponent could learn it. Retrain: python3 ai_train.py --place --fresh")
        except Exception as exc:  # noqa: BLE001
            print(f"\n(placement check skipped: {exc})")
    else:
        print("\n(no placement model yet - python3 ai_train.py --place trains one)")
    return 0


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Train the learned Battleship AI.")
    ap.add_argument("--imitate", type=int, default=300, metavar="N",
                    help="stage 1 iterations (default 300)")
    ap.add_argument("--selfplay", type=int, default=600, metavar="N",
                    help="stage 2 iterations (default 600; 0 = skip)")
    ap.add_argument("--place", type=int, nargs="?", const=400, default=0, metavar="N",
                    help="train the fleet-PLACEMENT network instead (N iterations, "
                         "default 400)")
    ap.add_argument("--fresh", action="store_true",
                    help="ignore the saved model and start from a random network")
    ap.add_argument("--minutes", type=float, default=0, metavar="M",
                    help="stop after about M minutes (default: run all iterations)")
    ap.add_argument("--every", type=int, default=10, metavar="N",
                    help="print progress + evaluate every N iterations (default 10)")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--test", action="store_true",
                    help="no training: play real games against easy/normal/hard")
    args = ap.parse_args(argv)

    if args.test:
        return head_to_head()

    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(1 << 30)
    rng = np.random.default_rng(seed)
    pyrng = random.Random(seed)

    if args.place:
        return train_placement(args, rng, pyrng)

    saved_avg = float("inf")
    saved = None
    if os.path.isfile(MODEL_PATH):
        try:
            saved = PolicyNet.load(MODEL_PATH)
            saved_avg, _ = evaluate(saved)
        except Exception as exc:  # noqa: BLE001
            print(f"could not read the old model ({exc}); it will be replaced")
    if saved is not None and not args.fresh:
        net = saved.copy()
        print(f"continuing from {os.path.basename(MODEL_PATH)} (scores {saved_avg:.1f} shots)")
    else:
        net = PolicyNet.fresh(rng)
        print("starting from a new random network")
        if saved is not None:
            print(f"the saved model scores {saved_avg:.1f} shots; it is only replaced "
                  "if the new network beats that")

    baselines()
    start_avg, start_illegal = evaluate(net)
    print(f"network right now: {start_avg:.1f} shots, illegal {start_illegal:.1%}\n")

    t0 = time.time()
    deadline = t0 + args.minutes * 60 if args.minutes > 0 else None
    state = {
        "net": net, "buf": Buffer(BUFFER_MAX), "rng": rng, "pyrng": pyrng,
        "t0": t0, "samples": 0, "every": max(1, args.every),
        # an existing saved model is only replaced by a network that scores better
        "best": saved_avg,
        "stop": (lambda: deadline is not None and time.time() > deadline),
    }

    try:
        if args.imitate > 0:
            stage_imitate(net, args.imitate, state)
        if args.selfplay > 0 and not state["stop"]():
            if state["buf"].size == 0:               # resumed model: refill the anchor
                collect_imitation(net, state["buf"], 128, 0.3, rng, pyrng)
            stage_selfplay(net, args.selfplay, state)
    except KeyboardInterrupt:
        print("\nstopped by you (Ctrl-C)")

    final, final_illegal = evaluate(net)
    if final < state["best"]:
        state["best"] = final
        net.save(MODEL_PATH, meta={"avg_shots": final, "illegal": final_illegal})
    print(f"\nbest saved model: {state['best']:.1f} shots on the fixed test games "
          f"-> {os.path.basename(MODEL_PATH)}")
    print("next: python3 ai_train.py --test   (real games vs easy / normal / hard)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
