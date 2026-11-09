#!/usr/bin/env python3
"""The learned Connect Four player: a learned *position evaluator* (numpy only).

Why an evaluator and not "just pick a column"?
    A network that picks a column directly has to see every tactic in one
    glance; it plateaus well below a search.  Here the network answers a simpler
    question - "how good is this position for the player to move?" - and the
    alpha-beta search in ``ai.py`` asks it at the end of every line it looks at.
    The search provides the lookahead, the network provides the judgement,
    and only the judgement is learned (``ai_train.py``).

The network sees 169 numbers, always from the point of view of the player to move:
    42  my discs            (row 0 = bottom)
    42  opponent discs
    42  empty cells where I would complete four in a row   ("my threats")
    42  empty cells where the opponent would complete four ("their threats")
     1  how full the board is (who moves next matters for threats)

Output: one number in (-1, 1)  (+1 = I am winning, -1 = I am losing).

The trained weights live in ``connect4_model.npz`` next to this file.
"""

from __future__ import annotations

import os
import threading
from typing import List, Optional, Sequence

import numpy as np

from ai import BOARD_BITS, H, STRIDE, W

CELLS = H * W
FEATURES = 4 * CELLS + 1  # 169
SIZES = (FEATURES, 192, 96, 1)

MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "connect4_model.npz")

_SHIFTS = [r * STRIDE + c for r in range(H) for c in range(W)]
_SH = np.array(_SHIFTS, dtype=np.uint64)
_ONE = np.uint64(1)


# --------------------------------------------------------------------------- #
# Bitboard helpers
# --------------------------------------------------------------------------- #

def winning_cells(pos: int, mask: int) -> int:
    """Empty cells where ``pos`` would complete four in a row (bitboard)."""
    r = (pos << STRIDE) & (pos << (2 * STRIDE)) & (pos << (3 * STRIDE))  # three stacked
    for s in (1, STRIDE - 1, STRIDE + 1):
        p = (pos << s) & (pos << (2 * s))
        r |= p & (pos << (3 * s))
        r |= p & (pos >> s)
        p = (pos >> s) & (pos >> (2 * s))
        r |= p & (pos << s)
        r |= p & (pos >> (3 * s))
    return r & BOARD_BITS & ~mask


def mirror_bits(pos: int) -> int:
    """Left/right mirror of a bitboard (the game is symmetric)."""
    out = 0
    for r in range(H):
        for c in range(W):
            if pos & (1 << (r * STRIDE + c)):
                out |= 1 << (r * STRIDE + (W - 1 - c))
    return out


def value_features(cur: int, other: int) -> np.ndarray:
    """169 features for the player holding ``cur`` (the one about to move)."""
    mask = cur | other
    x = np.empty(FEATURES, dtype=np.float32)
    x[0:CELLS] = (np.uint64(cur) >> _SH) & _ONE
    x[CELLS : 2 * CELLS] = (np.uint64(other) >> _SH) & _ONE
    x[2 * CELLS : 3 * CELLS] = (np.uint64(winning_cells(cur, mask)) >> _SH) & _ONE
    x[3 * CELLS : 4 * CELLS] = (np.uint64(winning_cells(other, mask)) >> _SH) & _ONE
    x[4 * CELLS] = bin(mask).count("1") / CELLS
    return x


# --------------------------------------------------------------------------- #
# Network
# --------------------------------------------------------------------------- #

class ValueNet:
    """MLP ``169 -> 192 -> 96 -> 1`` with ReLU inside and tanh on the output."""

    def __init__(self, params: Optional[dict] = None, seed: Optional[int] = None) -> None:
        if params is None:
            rng = np.random.default_rng(seed)
            params = {}
            for i in range(len(SIZES) - 1):
                fan_in, fan_out = SIZES[i], SIZES[i + 1]
                params[f"W{i}"] = (rng.standard_normal((fan_in, fan_out)) * np.sqrt(2.0 / fan_in)).astype(np.float32)
                params[f"b{i}"] = np.zeros(fan_out, dtype=np.float32)
        self.p = params
        self.layers = len(SIZES) - 1
        self.meta: dict = {}

    def forward(self, x: np.ndarray, keep: bool = False):
        """Pre-tanh output ``z`` for a batch ``x`` of shape ``(n, FEATURES)``."""
        acts = [x]
        h = x
        for i in range(self.layers):
            h = h @ self.p[f"W{i}"] + self.p[f"b{i}"]
            if i < self.layers - 1:
                h = np.maximum(h, 0.0)
            acts.append(h)
        return (h, acts) if keep else h

    def backward(self, acts: list, dz: np.ndarray) -> dict:
        grads = {}
        delta = dz
        for i in range(self.layers - 1, -1, -1):
            grads[f"W{i}"] = acts[i].T @ delta
            grads[f"b{i}"] = delta.sum(axis=0)
            if i > 0:
                delta = (delta @ self.p[f"W{i}"].T) * (acts[i] > 0)
        return grads

    def value(self, cur: int, other: int) -> float:
        """Evaluation in (-1, 1) from the point of view of the player holding ``cur``."""
        x = value_features(cur, other)
        h = np.maximum(x @ self.p["W0"] + self.p["b0"], 0.0)
        h = np.maximum(h @ self.p["W1"] + self.p["b1"], 0.0)
        z = h @ self.p["W2"] + self.p["b2"]
        return float(np.tanh(z[0]))

    def copy(self) -> "ValueNet":
        net = ValueNet({k: v.copy() for k, v in self.p.items()})
        net.meta = dict(self.meta)
        return net

    def save(self, path: str = MODEL_PATH, **meta) -> None:
        self.meta.update(meta)
        data = dict(self.p)
        data["meta_keys"] = np.array(list(self.meta.keys()))
        data["meta_vals"] = np.array([str(v) for v in self.meta.values()])
        tmp = path + ".tmp.npz"
        np.savez(tmp, **data)
        os.replace(tmp, path)
        _invalidate_cache()

    @staticmethod
    def load(path: str = MODEL_PATH) -> "ValueNet":
        with np.load(path, allow_pickle=False) as data:
            params = {k: data[k].astype(np.float32) for k in data.files if k[0] in "Wb" and k[1:].isdigit()}
            meta = {}
            if "meta_keys" in data.files:
                meta = dict(zip(map(str, data["meta_keys"]), map(str, data["meta_vals"])))
        for i in range(len(SIZES) - 1):
            w = params.get(f"W{i}")
            if w is None or w.shape != (SIZES[i], SIZES[i + 1]):
                raise ValueError("model file is from an older version - retrain with: python3 ai_train.py --fresh")
        net = ValueNet(params)
        net.meta = meta
        return net


# --------------------------------------------------------------------------- #
# Cached loading (used by ai.py)
# --------------------------------------------------------------------------- #

_cache_lock = threading.Lock()
_cache: dict = {"path": None, "mtime": None, "net": None}


def _invalidate_cache() -> None:
    with _cache_lock:
        _cache.update(path=None, mtime=None, net=None)


def load_model(path: str = MODEL_PATH) -> Optional[ValueNet]:
    """Cached loader; reloads automatically when the file changes. ``None`` if unusable."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    with _cache_lock:
        if _cache["path"] == path and _cache["mtime"] == mtime and _cache["net"] is not None:
            return _cache["net"]
    try:
        net = ValueNet.load(path)
    except Exception:
        return None
    with _cache_lock:
        _cache.update(path=path, mtime=mtime, net=net)
    return net
