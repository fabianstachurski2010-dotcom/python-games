#!/usr/bin/env python3
"""Learned Battleship policy network (numpy) — features, forward pass, model I/O.

This file is the single source of truth for what the network sees, so the
trainer (ai_train.py) and the game (ai.py) can never disagree about features.

What the network sees (public information only — never the hidden fleet):
    miss map          cells already shot that were water
    hit map           hits that do NOT belong to an announced-sunk ship yet
    sunk map          cells of ships already announced as sunk
    horizontal span   length of the free run (no miss / no sunk) through the cell
    vertical span     same, vertically
    remaining ships   5 flags: carrier, battleship, cruiser, submarine, destroyer
  -> 5 * 100 + 5 = 505 numbers

What it outputs: 100 scores, one per cell.  There is no legality guard inside
the network: it has to learn by itself not to shoot a cell twice.  (Only
``choose_attack`` has a last-resort fallback so the game never crashes; it
counts how often that happens in ``Policy.illegal``.)
"""

from __future__ import annotations

import os

import numpy as np

import engine

SIZE = engine.SIZE
CELLS = SIZE * SIZE
N_SHIPS = len(engine.FLEET)
SHIP_SIZES = np.array([size for _, size in engine.FLEET], dtype=np.int64)
N_FEATURES = 5 * CELLS + N_SHIPS            # 505
HIDDEN = (256, 128)

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(HERE, "battleship_model.npz")
PLACE_MODEL_PATH = os.path.join(HERE, "battleship_place_model.npz")

# Placement network: ships are placed one by one (carrier first).  It sees where
# the earlier ships are + which ship is next, and scores 200 placements
# (horizontal/vertical x 100 anchor cells).  Illegal placements (off the board or
# overlapping) are masked out — a ship cannot be un-placed, so unlike shooting
# there is nothing to learn from an illegal try.
PLACE_FEATURES = CELLS + N_SHIPS            # 105
PLACE_ACTIONS = 2 * CELLS                   # 200
PLACE_SIZES = (PLACE_FEATURES, 128, 128, PLACE_ACTIONS)


# --------------------------------------------------------------------------- #
# Features (vectorised: N boards at once)
# --------------------------------------------------------------------------- #

def _span(free: np.ndarray, axis: int) -> np.ndarray:
    """Length of the contiguous free run through every free cell (0 elsewhere).

    *free* is ``(N, 10, 10)`` bool; *axis* 2 = along a row, 1 = along a column.
    """
    work = free if axis == 2 else free.transpose(0, 2, 1)
    n = work.shape[0]
    left = np.zeros((n, SIZE, SIZE), dtype=np.int16)
    right = np.zeros((n, SIZE, SIZE), dtype=np.int16)
    for c in range(SIZE):
        prev = left[:, :, c - 1] if c > 0 else 0
        left[:, :, c] = np.where(work[:, :, c], prev + 1, 0)
    for c in range(SIZE - 1, -1, -1):
        nxt = right[:, :, c + 1] if c < SIZE - 1 else 0
        right[:, :, c] = np.where(work[:, :, c], nxt + 1, 0)
    span = np.where(work, left + right - 1, 0)
    return span if axis == 2 else span.transpose(0, 2, 1)


def build_features(shots: np.ndarray, sunk: np.ndarray,
                   remaining: np.ndarray) -> np.ndarray:
    """Turn public board state into network input.

    shots      (N, 10, 10) int8   0 = unshot, 1 = miss, 2 = hit
    sunk       (N, 10, 10) bool   cell belongs to an announced-sunk ship
    remaining  (N, 5)      float  1.0 if that ship is still afloat
    returns    (N, 505)    float32
    """
    n = shots.shape[0]
    miss = shots == 1
    hit_open = (shots == 2) & ~sunk
    free = ~(miss | sunk)
    hspan = _span(free, 2).astype(np.float32) / SIZE
    vspan = _span(free, 1).astype(np.float32) / SIZE
    parts = (
        miss.reshape(n, CELLS).astype(np.float32),
        hit_open.reshape(n, CELLS).astype(np.float32),
        sunk.reshape(n, CELLS).astype(np.float32),
        hspan.reshape(n, CELLS),
        vspan.reshape(n, CELLS),
        np.asarray(remaining, dtype=np.float32).reshape(n, N_SHIPS),
    )
    return np.concatenate(parts, axis=1)


def board_state(board) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Public state of an ``engine.Board`` as ``(shots, sunk, remaining)`` (N=1)."""
    shots = np.zeros((1, SIZE, SIZE), dtype=np.int8)
    for (r, c), result in board.shots.items():
        shots[0, r, c] = 2 if result == "hit" else 1
    sunk = np.zeros((1, SIZE, SIZE), dtype=bool)
    remaining = np.ones((1, N_SHIPS), dtype=np.float32)
    for ship in board.sunk_ships:
        for r, c in ship.cells:
            sunk[0, r, c] = True
        remaining[0, engine.SHIP_NAMES.index(ship.name)] = 0.0
    return shots, sunk, remaining


# --------------------------------------------------------------------------- #
# The network: 505 -> 256 -> 128 -> 100, ReLU
# --------------------------------------------------------------------------- #

class PolicyNet:
    PARAM_NAMES = ("W1", "b1", "W2", "b2", "W3", "b3")

    def __init__(self, params: dict[str, np.ndarray]):
        self.p = {k: np.asarray(params[k], dtype=np.float32) for k in self.PARAM_NAMES}

    @classmethod
    def fresh(cls, rng: np.random.Generator,
              sizes: tuple[int, ...] | None = None) -> "PolicyNet":
        sizes = sizes or (N_FEATURES, *HIDDEN, CELLS)
        params: dict[str, np.ndarray] = {}
        for i in range(3):
            fan_in, fan_out = sizes[i], sizes[i + 1]
            scale = np.sqrt(2.0 / fan_in) * (0.1 if i == 2 else 1.0)
            params[f"W{i + 1}"] = rng.normal(0.0, scale, (fan_in, fan_out))
            params[f"b{i + 1}"] = np.zeros(fan_out)
        return cls(params)

    def copy(self) -> "PolicyNet":
        return PolicyNet({k: v.copy() for k, v in self.p.items()})

    def forward(self, x: np.ndarray, cache: bool = False):
        p = self.p
        z1 = x @ p["W1"] + p["b1"]
        a1 = np.maximum(z1, 0.0)
        z2 = a1 @ p["W2"] + p["b2"]
        a2 = np.maximum(z2, 0.0)
        logits = a2 @ p["W3"] + p["b3"]
        if cache:
            return logits, (x, z1, a1, z2, a2)
        return logits

    def backward(self, cache, dlogits: np.ndarray) -> dict[str, np.ndarray]:
        """Gradients of a loss whose gradient w.r.t. the logits is *dlogits*."""
        x, z1, a1, z2, a2 = cache
        p = self.p
        g = {}
        g["W3"] = a2.T @ dlogits
        g["b3"] = dlogits.sum(axis=0)
        da2 = dlogits @ p["W3"].T
        dz2 = da2 * (z2 > 0)
        g["W2"] = a1.T @ dz2
        g["b2"] = dz2.sum(axis=0)
        da1 = dz2 @ p["W2"].T
        dz1 = da1 * (z1 > 0)
        g["W1"] = x.T @ dz1
        g["b1"] = dz1.sum(axis=0)
        return g

    # -- I/O ---------------------------------------------------------------- #
    def save(self, path: str = MODEL_PATH, meta: dict | None = None) -> None:
        tmp = path + ".tmp.npz"
        extra = {f"meta_{k}": np.asarray(v) for k, v in (meta or {}).items()}
        np.savez(tmp, features=np.asarray(self.p["W1"].shape[0]), **self.p, **extra)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str = MODEL_PATH, features: int = N_FEATURES) -> "PolicyNet":
        with np.load(path) as data:
            if int(data["features"]) != features:
                raise ValueError(
                    f"{os.path.basename(path)} was trained with a different "
                    f"feature layout ({int(data['features'])} != {features}); "
                    "retrain with ai_train.py --fresh"
                )
            return cls({k: data[k] for k in cls.PARAM_NAMES})


# --------------------------------------------------------------------------- #
# Play-time policy
# --------------------------------------------------------------------------- #

_CACHE: dict[str, tuple[float, PolicyNet]] = {}


def model_exists(path: str = MODEL_PATH) -> bool:
    return os.path.isfile(path)


def place_model_exists(path: str = PLACE_MODEL_PATH) -> bool:
    return os.path.isfile(path)


def load_model(path: str = MODEL_PATH, features: int = N_FEATURES,
               hint: str = "python3 ai_train.py") -> PolicyNet:
    """Load (and cache by mtime) a trained network."""
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"{os.path.basename(path)} not found — train it first: {hint}"
        )
    mtime = os.path.getmtime(path)
    hit = _CACHE.get(path)
    if hit is not None and hit[0] == mtime:
        return hit[1]
    net = PolicyNet.load(path, features)
    _CACHE[path] = (mtime, net)
    return net


def load_place_model(path: str = PLACE_MODEL_PATH) -> PolicyNet:
    return load_model(path, PLACE_FEATURES, "python3 ai_train.py --place")


# --------------------------------------------------------------------------- #
# Placement helpers (shared by the trainer and the game)
# --------------------------------------------------------------------------- #

def _placement_tables() -> dict[int, tuple[np.ndarray, np.ndarray]]:
    """size -> (200 x 100 cell matrix, 200 in-bounds flags)."""
    tables = {}
    for size in sorted(set(int(s) for s in SHIP_SIZES)):
        mat = np.zeros((PLACE_ACTIONS, CELLS), dtype=np.float32)
        inside = np.zeros(PLACE_ACTIONS, dtype=bool)
        for action in range(PLACE_ACTIONS):
            vertical, cell = divmod(action, CELLS)
            r, c = divmod(cell, SIZE)
            cells = [(r + (i if vertical else 0), c + (0 if vertical else i))
                     for i in range(size)]
            if all(rr < SIZE and cc < SIZE for rr, cc in cells):
                inside[action] = True
                for rr, cc in cells:
                    mat[action, rr * SIZE + cc] = 1.0
        tables[size] = (mat, inside)
    return tables


PLACE_TABLES = _placement_tables()


def transform_cell(r: int, c: int, t: int) -> tuple[int, int]:
    """One of the 8 board symmetries (t = 0..7): optional transpose, then flips."""
    if t & 4:
        r, c = c, r
    if t & 1:
        r = SIZE - 1 - r
    if t & 2:
        c = SIZE - 1 - c
    return r, c


def place_features(occupied: np.ndarray, ship_index: int) -> np.ndarray:
    """(N, 100) occupancy + which ship is next -> (N, 105)."""
    n = occupied.shape[0]
    feats = np.zeros((n, PLACE_FEATURES), dtype=np.float32)
    feats[:, :CELLS] = occupied
    feats[:, CELLS + ship_index] = 1.0
    return feats


def place_legal(occupied: np.ndarray, size: int) -> np.ndarray:
    """(N, 200) bool: placements that stay on the board and overlap nothing."""
    mat, inside = PLACE_TABLES[size]
    return inside[None, :] & ((occupied @ mat.T) == 0)


def masked_softmax(logits: np.ndarray, legal: np.ndarray) -> np.ndarray:
    z = np.where(legal, logits, -1e9)
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z) * legal
    return e / (e.sum(axis=1, keepdims=True) + 1e-12)


class PlacementPolicy:
    """Places a whole fleet with the trained placement network.

    Ships are *sampled* from the network's probabilities (not argmax), and the
    finished layout is then rotated/mirrored at random (one of the 8 board
    symmetries), so it never sits in the same corner every game.
    """

    def __init__(self, net: PolicyNet | None = None, path: str = PLACE_MODEL_PATH):
        self.net = net if net is not None else load_place_model(path)

    def place(self, rng) -> list[dict]:
        """*rng* is a ``random.Random``.  Returns an engine fleet."""
        occupied = np.zeros((1, CELLS), dtype=np.float32)
        ships: list[dict] = []
        for index, (name, size) in enumerate(engine.FLEET):
            legal = place_legal(occupied, size)
            if not legal.any():
                raise engine.PlacementError("generation_failed", "no legal spot left")
            probs = masked_softmax(self.net.forward(place_features(occupied, index)),
                                   legal)[0]
            pick = int(np.searchsorted(np.cumsum(probs), rng.random() * probs.sum()))
            pick = min(pick, PLACE_ACTIONS - 1)
            if not legal[0, pick]:                       # float edge case
                pick = int(np.flatnonzero(legal[0])[0])
            mat, _ = PLACE_TABLES[size]
            cells = np.flatnonzero(mat[pick])
            ships.append({"name": name,
                          "cells": [[int(x // SIZE), int(x % SIZE)] for x in cells]})
            occupied += mat[pick][None, :]
        t = min(int(rng.random() * 8), 7)               # random rotation / mirror
        if t:
            for ship in ships:
                ship["cells"] = [list(transform_cell(r, c, t)) for r, c in ship["cells"]]
        return ships


class Policy:
    """Wraps a PolicyNet for real ``engine.Board`` objects."""

    def __init__(self, net: PolicyNet | None = None, path: str = MODEL_PATH):
        self.net = net if net is not None else load_model(path)
        self.illegal = 0     # times the raw top choice was an already-shot cell
        self.moves = 0

    def scores(self, board) -> np.ndarray:
        shots, sunk, remaining = board_state(board)
        return self.net.forward(build_features(shots, sunk, remaining))[0]

    def choose(self, board) -> tuple[int, int]:
        """Best cell by network score.  Raises ShotError when nothing is left."""
        if len(board.shots) >= CELLS:
            raise engine.ShotError("no_moves", "no available cells left to fire at")
        scores = self.scores(board)
        self.moves += 1
        top = int(np.argmax(scores))
        r, c = divmod(top, SIZE)
        if (r, c) in board.shots:
            # The network picked a cell that was already shot.  Count it, then
            # fall back to its best *legal* cell so the game keeps going.
            self.illegal += 1
            masked = scores.copy()
            for (sr, sc) in board.shots:
                masked[sr * SIZE + sc] = -np.inf
            top = int(np.argmax(masked))
            r, c = divmod(top, SIZE)
        return r, c
