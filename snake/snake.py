#!/usr/bin/env python3
"""霓虹貪食蛇 Neon Serpent — a neon Snake with combos, power-ups and three modes.

GUI controls
    ↑ ↓ ← →  /  WASD    轉向（最多緩衝兩次轉向）
    Space / P / Esc     暫停（再按一次繼續）
    R                   重來
    Q                   回選單
    M                   音效開關
    選單：↑↓ 選擇模式，Enter / 1 / 2 / 3 開始，也可用滑鼠點卡片，Esc 離開

Command line
    python3 snake.py                     # 選單
    python3 snake.py --play              # 跳過選單，直接開始預設模式
    python3 snake.py --play --mode wrap  # 直接開始穿牆模式
    python3 snake.py --selftest          # 無視窗邏輯測試（109 項）
    python3 snake.py --text 2            # 無視窗跑 2 秒，印出 ASCII 盤面
    python3 snake.py --seed 7            # 固定果實順序（可重現）
    python3 snake.py --mute              # 靜音啟動
    python3 snake.py --no-highscore      # 不讀寫 highscore.json

The game is split into three layers:

    SnakeGame   pure grid logic, no pygame, deterministic with --seed.
    Renderer    everything drawn on the surface (board, snake, FX, HUD).
    GameApp     pygame loop, menus, input, sound and state transitions.

The logic layer is a fixed 60 Hz simulation: `SnakeGame.step()` advances exactly
one frame and returns a list of events, so the whole game can be played and
tested without a window.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import tempfile
from collections import deque
from dataclasses import dataclass

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

try:  # the logic layer never needs pygame; only the GUI does
    import pygame
except ImportError:  # pragma: no cover - exercised on machines without pygame
    pygame = None  # type: ignore[assignment]


# --------------------------------------------------------------------------
# Board geometry
# --------------------------------------------------------------------------

APP_NAME = "霓虹貪食蛇 Neon Serpent"
VERSION = "1.0.1"

COLS, ROWS = 30, 20
CELL = 26
BOARD_X, BOARD_Y = 60, 110
BOARD_W, BOARD_H = COLS * CELL, ROWS * CELL
WIN_W, WIN_H = 900, 700

FPS = 60
FIXED_DT = 1.0 / FPS

COMBO_WINDOW = 3.2      # seconds an apple must follow the previous one
COMBO_MAX = 8           # multiplier cap
SPECIAL_EVERY = 4       # apples eaten between special spawns
MAX_SPECIALS = 3
SLOW_TIME = 7.0
DOUBLE_TIME = 8.0
HIGHSCORE_FILE = "highscore.json"

SPECIAL_ORDER = ("golden", "slow", "double", "trim", "poison")


# --------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Mode:
    key: str
    name: str
    en: str
    desc: str
    base: float          # seconds per cell at level 1
    ramp: float          # interval multiplier applied per eaten apple
    floor: float         # fastest interval
    wrap: bool
    duration: float | None


MODES: dict[str, Mode] = {
    "classic": Mode(
        "classic", "經典", "CLASSIC",
        "撞牆就結束，吃得越多蛇越快",
        base=0.140, ramp=0.985, floor=0.060, wrap=False, duration=None,
    ),
    "wrap": Mode(
        "wrap", "穿牆", "WRAP",
        "邊界會穿過去，挑戰極限長度",
        base=0.130, ramp=0.987, floor=0.055, wrap=True, duration=None,
    ),
    "sprint": Mode(
        "sprint", "限時", "SPRINT",
        "90 秒衝分，速度爬升最快",
        base=0.125, ramp=0.982, floor=0.055, wrap=False, duration=90.0,
    ),
}


@dataclass(frozen=True)
class PickupInfo:
    kind: str
    name: str
    base: int
    grow: int
    shrink: int
    ttl: float | None
    weight: int


PICKUPS: dict[str, PickupInfo] = {
    "apple": PickupInfo("apple", "蘋果", 10, 1, 0, None, 0),
    "golden": PickupInfo("golden", "金蘋果", 50, 2, 0, 7.0, 34),
    "slow": PickupInfo("slow", "減速", 15, 1, 0, 8.0, 14),
    "double": PickupInfo("double", "雙倍", 15, 1, 0, 9.0, 14),
    "trim": PickupInfo("trim", "縮短", 20, 0, 5, 8.0, 12),
    "poison": PickupInfo("poison", "毒果", -30, 0, 3, 9.0, 18),
}


@dataclass
class Pickup:
    """A timed special that sits on one board cell."""

    kind: str
    x: int
    y: int
    ttl: float

    @property
    def pos(self) -> tuple[int, int]:
        return (self.x, self.y)


# --------------------------------------------------------------------------
# Pure logic
# --------------------------------------------------------------------------

class SnakeGame:
    """Grid-based snake simulation. One `step()` is exactly one 1/60 s frame."""

    def __init__(self, mode: str = "classic", seed: int | None = None,
                 cols: int = COLS, rows: int = ROWS) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode: {mode!r}")
        if cols < 6 or rows < 6:
            raise ValueError("board must be at least 6x6")
        self.mode = mode
        self.cols = cols
        self.rows = rows
        self.seed = seed
        self.rng = random.Random(seed)
        self.reset()

    # -- setup ---------------------------------------------------------

    def reset(self) -> None:
        self.rng = random.Random(self.seed)
        cx, cy = self.cols // 2, self.rows // 2
        self.body: deque[tuple[int, int]] = deque(
            (cx - i, cy) for i in range(4)
        )
        self.prev_body: list[tuple[int, int]] = list(self.body)
        self.dir: tuple[int, int] = (1, 0)
        self.pending: deque[tuple[int, int]] = deque()

        self.phase = "playing"
        self.score = 0
        self.eaten = 0
        self.chain = 0
        self.max_chain = 0
        self.combo_timer = 0.0
        self.slow_timer = 0.0
        self.double_timer = 0.0

        self.elapsed = 0.0
        self.move_accum = 0.0
        self.grow_pending = 0
        self.shrink_pending = 0
        self.since_special = 0

        self.specials: list[Pickup] = []
        self.apple: tuple[int, int] | None = None
        self.death_reason: str | None = None
        self.final_stats: dict | None = None

        self._spawn_apple()

    # -- read-only state ----------------------------------------------

    @property
    def cfg(self) -> Mode:
        return MODES[self.mode]

    @property
    def multiplier(self) -> int:
        return max(1, self.chain)

    @property
    def level(self) -> int:
        return 1 + self.eaten // 5

    @property
    def elapsed_time(self) -> float:
        return self.elapsed

    @property
    def time_left(self) -> float | None:
        dur = self.cfg.duration
        return None if dur is None else max(0.0, dur - self.elapsed)

    @property
    def interval(self) -> float:
        """Seconds between grid steps (slower when the slow power-up is on)."""
        m = self.cfg
        t = m.base * (m.ramp ** min(self.eaten, 150))
        t = max(m.floor, t)
        if self.slow_timer > 0:
            t /= 0.6
        return t

    def stats(self) -> dict:
        return {
            "mode": self.mode,
            "score": int(self.score),
            "length": len(self.body),
            "eaten": self.eaten,
            "max_chain": self.max_chain,
            "elapsed": round(self.elapsed, 2),
            "time_left": None if self.time_left is None else round(self.time_left, 2),
            "reason": self.death_reason,
        }

    # -- input ---------------------------------------------------------

    def queue_dir(self, d: tuple[int, int]) -> bool:
        """Buffer a turn. Rejects 180° turns and more than two buffered turns."""
        if self.phase != "playing":
            return False
        if d not in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            return False
        if len(self.pending) >= 2:
            return False
        base = self.pending[-1] if self.pending else self.dir
        if d == base or d == (-base[0], -base[1]):
            return False
        self.pending.append(d)
        return True

    # -- simulation ----------------------------------------------------

    def step(self) -> list[dict]:
        """Advance one 1/60 s frame. Returns the events it produced."""
        if self.phase != "playing":
            return []
        ev: list[dict] = []
        dt = FIXED_DT
        self.elapsed += dt

        m = self.cfg
        if m.duration is not None and self.elapsed >= m.duration:
            self.elapsed = m.duration
            return self._die("time")

        # timers -------------------------------------------------------
        if self.combo_timer > 0:
            self.combo_timer = max(0.0, self.combo_timer - dt)
            if self.combo_timer == 0 and self.chain:
                self.chain = 0
                ev.append({"type": "combo_end"})
        if self.slow_timer > 0:
            self.slow_timer = max(0.0, self.slow_timer - dt)
        if self.double_timer > 0:
            self.double_timer = max(0.0, self.double_timer - dt)
        for p in list(self.specials):
            p.ttl -= dt
            if p.ttl <= 0:
                self.specials.remove(p)
                ev.append({"type": "expire", "kind": p.kind, "pos": p.pos})

        # movement ----------------------------------------------------
        self.move_accum += dt
        guard = 0
        while (self.phase == "playing" and self.move_accum >= self.interval
               and guard < 4):
            self.move_accum -= self.interval
            ev.extend(self._tick())
            guard += 1
        return ev

    def _tick(self) -> list[dict]:
        ev: list[dict] = []
        m = self.cfg

        # consume at most one buffered turn per grid step
        while self.pending:
            d = self.pending.popleft()
            if d != (-self.dir[0], -self.dir[1]):
                self.dir = d
                break

        hx, hy = self.body[0]
        nx, ny = hx + self.dir[0], hy + self.dir[1]
        if m.wrap:
            nx %= self.cols
            ny %= self.rows
        elif not (0 <= nx < self.cols and 0 <= ny < self.rows):
            return self._die("wall")

        kind: str | None = None
        if self.apple == (nx, ny):
            kind = "apple"
        else:
            for p in self.specials:
                if p.pos == (nx, ny):
                    kind = p.kind
                    break

        # The tail cell is free unless this move eats (then the body grows).
        occupied = set(self.body)
        if kind is None:
            occupied.discard(self.body[-1])
        if (nx, ny) in occupied:
            return self._die("self")

        self.prev_body = list(self.body)
        self.body.appendleft((nx, ny))

        if kind is not None:
            ev.extend(self._consume(kind, (nx, ny)))

        # tail removal: eating keeps the tail for one more tick
        if self.grow_pending > 0:
            self.grow_pending -= 1
        else:
            self.body.pop()
        for _ in range(self.shrink_pending):
            if len(self.body) > 3:
                self.body.pop()
        self.shrink_pending = 0
        return ev

    def _consume(self, kind: str, pos: tuple[int, int]) -> list[dict]:
        info = PICKUPS[kind]
        ev: list[dict] = []
        was_double = self.double_timer > 0

        if kind in ("apple", "golden"):
            self.eaten += 1
            self.chain = min(COMBO_MAX, self.chain + 1)
            self.combo_timer = COMBO_WINDOW
            self.max_chain = max(self.max_chain, self.chain)
        elif kind == "poison":
            self.chain = 0
            self.combo_timer = 0.0
        elif kind == "slow":
            self.slow_timer = max(self.slow_timer, SLOW_TIME)
        elif kind == "double":
            self.double_timer = max(self.double_timer, DOUBLE_TIME)

        if info.grow:
            self.grow_pending += info.grow
        if info.shrink:
            self.shrink_pending += info.shrink

        mult = self.chain if kind in ("apple", "golden") else 1
        points = info.base * mult
        if was_double and points > 0 and kind != "double":
            points *= 2
        self.score = max(0, int(self.score + points))

        etype = ("eat" if kind in ("apple", "golden")
                 else "poison" if kind == "poison" else "power")
        ev.append({
            "type": etype, "kind": kind, "pos": pos, "points": points,
            "mult": mult, "chain": self.chain, "score": self.score,
        })

        if kind == "apple":
            self.apple = None
            self._spawn_apple()
        else:
            self._remove_special(pos)

        if kind in ("apple", "golden"):
            self.since_special += 1
            self._maybe_spawn_special()
        return ev

    def _die(self, reason: str) -> list[dict]:
        self.phase = "dead"
        self.death_reason = reason
        self.final_stats = self.stats()
        return [{"type": "die", "reason": reason, "stats": self.final_stats.copy()}]

    def _remove_special(self, pos: tuple[int, int]) -> None:
        for i, p in enumerate(self.specials):
            if p.pos == pos:
                self.specials.pop(i)
                return

    # -- spawning ------------------------------------------------------

    def _free_cells(self, head: tuple[int, int], min_dist: int = 0) -> list[tuple[int, int]]:
        occupied = set(self.body)
        occupied.update(p.pos for p in self.specials)
        if self.apple is not None:
            occupied.add(self.apple)
        free = [(x, y) for x in range(self.cols) for y in range(self.rows)
                if (x, y) not in occupied]
        if min_dist > 0 and free:
            far = [c for c in free
                   if abs(c[0] - head[0]) + abs(c[1] - head[1]) >= min_dist]
            return far or free
        return free

    def _spawn_apple(self) -> None:
        self.apple = None
        head = self.body[0]
        cells = self._free_cells(head, min_dist=4)
        self.apple = self.rng.choice(cells) if cells else None

    def _maybe_spawn_special(self) -> Pickup | None:
        if self.since_special < SPECIAL_EVERY:
            return None
        if len(self.specials) >= MAX_SPECIALS:
            return None
        self.since_special = 0

        kinds = list(SPECIAL_ORDER)
        weights = []
        for k in kinds:
            w = PICKUPS[k].weight
            if k == "poison":
                w += min(10, (self.level - 1) * 2)
            weights.append(w)
        kind = self.rng.choices(kinds, weights=weights, k=1)[0]
        info = PICKUPS[kind]
        cells = self._free_cells(self.body[0], min_dist=3)
        if not cells:
            return None
        x, y = self.rng.choice(cells)
        p = Pickup(kind, x, y, info.ttl or 8.0)
        self.specials.append(p)
        return p

    # -- rendering helpers (pure: no pygame here) ----------------------

    def progress(self) -> float:
        """Interpolation factor between the previous and current grid step."""
        return max(0.0, min(1.0, self.move_accum / self.interval))

    def render_body(self, t: float | None = None) -> list[tuple[float, float]]:
        """Smoothed segment centres in cell coordinates, head first.

        Each segment interpolates from where it was one step ago to where it is
        now, which makes the whole snake crawl smoothly.  In wrap mode the
        target is unwrapped past the seam so the head slides off one edge and
        reappears on the other instead of snapping backwards.
        """
        t = self.progress() if t is None else max(0.0, min(1.0, t))
        prev = self.prev_body
        out: list[tuple[float, float]] = []
        wrap = self.cfg.wrap
        for i, (cx, cy) in enumerate(self.body):
            if prev:
                px, py = prev[i] if i < len(prev) else prev[-1]
            else:
                px, py = cx, cy
            fx, fy = float(cx), float(cy)
            if wrap:
                dx = fx - px
                if dx > self.cols / 2:
                    fx -= self.cols
                elif dx < -self.cols / 2:
                    fx += self.cols
                dy = fy - py
                if dy > self.rows / 2:
                    fy -= self.rows
                elif dy < -self.rows / 2:
                    fy += self.rows
            out.append((px + (fx - px) * t, py + (fy - py) * t))
        return out

    def ascii_frame(self) -> str:
        grid = [["." for _ in range(self.cols)] for _ in range(self.rows)]
        for i, (x, y) in enumerate(self.body):
            if 0 <= x < self.cols and 0 <= y < self.rows:
                grid[y][x] = "@" if i == 0 else "o"
        if self.apple is not None:
            x, y = self.apple
            grid[y][x] = "*"
        glyph = {"golden": "G", "slow": "S", "double": "D",
                 "trim": "T", "poison": "P"}
        for p in self.specials:
            if 0 <= p.x < self.cols and 0 <= p.y < self.rows:
                grid[p.y][p.x] = glyph.get(p.kind, "?")
        return "\n".join("".join(row) for row in grid)

    # -- testing / demo helper ----------------------------------------

    def set_state(self, body, direction=(1, 0), apple=None, specials=None) -> None:
        """Replace the board state (used by the tests and the screenshot tool)."""
        self.body = deque((int(x), int(y)) for x, y in body)
        self.prev_body = list(self.body)
        self.dir = direction
        self.pending.clear()
        self.apple = apple
        self.specials = list(specials or [])
        self.move_accum = 0.0
        self.grow_pending = 0
        self.shrink_pending = 0


# --------------------------------------------------------------------------
# High scores
# --------------------------------------------------------------------------

def default_highscore_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), HIGHSCORE_FILE)


def load_highscores(path: str | None = None) -> dict:
    path = path or default_highscore_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_highscore(path: str | None, mode: str, stats: dict) -> bool:
    """Persist a run if it beats the stored score. Returns True for a new best."""
    path = path or default_highscore_path()
    data = load_highscores(path)
    row = data.get(mode)
    prev = int(row.get("score", -1)) if isinstance(row, dict) else -1
    score = int(stats.get("score", 0))
    if score <= prev:
        return False
    data[mode] = {
        "score": score,
        "length": int(stats.get("length", 0)),
        "combo": int(stats.get("max_chain", 0)),
        "elapsed": float(stats.get("elapsed", 0.0)),
    }
    try:
        folder = os.path.dirname(os.path.abspath(path))
        os.makedirs(folder, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------
# Self-test (pure logic, no window)
# --------------------------------------------------------------------------

def selftest(verbose: bool = True) -> int:
    checks = 0
    failures: list[str] = []

    def check(name: str, ok: object) -> None:
        nonlocal checks
        checks += 1
        if ok:
            if verbose:
                print(f"  ok    {name}")
        else:
            failures.append(name)
            print(f"  FAIL  {name}")

    def section(title: str) -> None:
        if verbose:
            print(f"\n[{title}]")

    def approx(a: float, b: float, eps: float = 1e-6) -> bool:
        return abs(a - b) <= eps

    def dist(a: tuple[int, int], b: tuple[int, int]) -> int:
        return abs(a[0] - b[0]) + abs(a[1] - b[1])

    # -- initial board -------------------------------------------------
    section("盤面與初始狀態")
    g = SnakeGame(seed=1)
    check("初始長度為 4", len(g.body) == 4)
    check("蛇頭在棋盤中央", g.body[0] == (COLS // 2, ROWS // 2))
    check("身體每節相連", all(dist(g.body[i], g.body[i + 1]) == 1
                               for i in range(len(g.body) - 1)))
    check("蘋果不在蛇身上", g.apple is not None and g.apple not in set(g.body))
    check("初始方向向右", g.dir == (1, 0))
    check("初始連擊為 0", g.chain == 0 and g.multiplier == 1)
    check("經典模式沒有時限", g.time_left is None)
    check("限時模式有 90 秒倒數",
          SnakeGame("sprint", seed=1).time_left is not None
          and SnakeGame("sprint", seed=1).time_left > 89.9)
    check("三種模式都有設定", set(MODES) == {"classic", "wrap", "sprint"})
    check("限時模式時間為 90 秒", MODES["sprint"].duration == 90.0)

    # -- direction buffer ---------------------------------------------
    section("轉向緩衝")
    g = SnakeGame(seed=2)
    check("可以往上轉", g.queue_dir((0, -1)))
    check("不能直接反轉", not g.queue_dir((0, 1)))
    check("緩衝第二個轉向", g.queue_dir((-1, 0)))
    check("最多緩衝兩個轉向", not g.queue_dir((0, 1)))
    check("不接受零向量", not SnakeGame(seed=2).queue_dir((0, 0)))
    check("不接受斜向", not SnakeGame(seed=2).queue_dir((1, 1)))
    g = SnakeGame(seed=3)
    g.apple = (0, 0)
    g.queue_dir((0, -1))
    g._tick()
    check("緩衝的轉向會在下一格生效",
          g.body[0] == (COLS // 2, ROWS // 2 - 1))

    # -- movement ------------------------------------------------------
    section("移動與時間")
    g = SnakeGame(seed=4)
    head = g.body[0]
    g.step()
    check("不到間隔時間不會移動", g.body[0] == head)
    for _ in range(30):
        g.step()
        if g.body[0] != head:
            break
    check("累積到間隔後前進一格", g.body[0] == (head[0] + 1, head[1]))
    check("普通移動不改變長度", len(g.body) == 4)
    check("移動後蛇身仍然相連",
          all(dist(g.body[i], g.body[i + 1]) == 1 for i in range(3)))
    check("progress 介於 0 與 1", 0.0 <= g.progress() <= 1.0)

    # -- apples & combo ------------------------------------------------
    section("蘋果與連擊")
    g = SnakeGame(seed=5)
    g.set_state([(5, 5), (4, 5), (3, 5), (2, 5)], apple=(6, 5))
    ev = g._tick()
    check("吃到蘋果產生事件", any(e["type"] == "eat" for e in ev))
    check("第一顆蘋果 +10", g.score == 10)
    check("吃到蘋果長 1 節", len(g.body) == 5)
    check("吃掉後立刻重生蘋果", g.apple is not None and g.apple != (6, 5))
    check("新蘋果不在蛇身上", g.apple not in set(g.body))
    check("新蘋果至少離蛇頭 4 格", dist(g.apple, g.body[0]) >= 4)
    check("連擊從 1 開始", g.chain == 1 and g.multiplier == 1)
    check("連擊計時器重設", approx(g.combo_timer, COMBO_WINDOW))
    g.apple = (g.body[0][0] + 1, g.body[0][1])
    g._tick()
    check("第二顆蘋果連擊 ×2", g.chain == 2 and g.multiplier == 2)
    check("第二顆蘋果 +20 分", g.score == 30)
    g.chain = COMBO_MAX
    g.apple = (g.body[0][0] + 1, g.body[0][1])
    g._tick()
    check("連擊上限為 8", g.chain == COMBO_MAX)
    check("上限時單顆 80 分", g.score == 30 + 10 * COMBO_MAX)
    check("最高連擊有記錄", g.max_chain == COMBO_MAX)

    g = SnakeGame(seed=6)
    g.chain = 3
    g.combo_timer = 0.001
    g.apple = (0, 0)
    ev = []
    for _ in range(2):
        ev.extend(g.step())
    check("連擊超時會歸零", g.chain == 0)
    check("超時送出 combo_end 事件", any(e["type"] == "combo_end" for e in ev))

    # -- golden apple --------------------------------------------------
    section("金蘋果")
    g = SnakeGame(seed=7)
    g.set_state([(5, 5), (4, 5), (3, 5), (2, 5)],
                apple=(6, 5), specials=[Pickup("golden", 7, 5, 7.0)])
    g._tick()
    check("金蘋果在第二顆時值 100", g.score == 10)
    ev = g._tick()
    check("金蘋果加倍給分（連擊 ×2）", g.score == 10 + 100)
    check("金蘋果吃後長 1 節", len(g.body) == 6)
    check("金蘋果事件", any(e["type"] == "eat" and e["kind"] == "golden"
                          for e in ev))
    g.apple = (1, 1)
    g._tick()
    check("金蘋果總共長 2 節", len(g.body) == 7)

    # -- power-ups -----------------------------------------------------
    section("特殊果實")
    g = SnakeGame(seed=8)
    g.set_state([(5, 5), (4, 5), (3, 5), (2, 5)],
                specials=[Pickup("slow", 6, 5, 8.0)], apple=(20, 10))
    ev = g._tick()
    check("吃到減速道具啟動效果", approx(g.slow_timer, SLOW_TIME))
    check("道具事件類型正確",
          any(e["type"] == "power" and e["kind"] == "slow" for e in ev))
    base = SnakeGame(seed=8).interval
    check("減速時間隔變長", g.interval > base * 1.5)
    g.slow_timer = 0.0
    check("減速結束後恢復", approx(g.interval, base))

    g = SnakeGame(seed=9)
    g.set_state([(5, 5), (4, 5), (3, 5), (2, 5)],
                apple=(6, 5), specials=[])
    g.double_timer = 5.0
    g._tick()
    check("雙倍時蘋果給 20 分", g.score == 20)
    check("雙倍只影響加分", g.double_timer > 0)

    g = SnakeGame(seed=10)
    g.set_state([(5, 5), (4, 5), (3, 5), (2, 5)],
                apple=(20, 10), specials=[Pickup("double", 6, 5, 8.0)])
    g.double_timer = 5.0
    g._tick()
    check("雙倍道具本身不加倍", g.score == 15)
    check("雙倍道具延長時間", approx(g.double_timer, DOUBLE_TIME))

    g = SnakeGame(seed=11)
    body = [(10, 10), (9, 10), (8, 10), (7, 10), (6, 10),
            (5, 10), (4, 10), (3, 10), (2, 10), (1, 10)]
    g.set_state(body, specials=[Pickup("trim", 11, 10, 5.0)], apple=(20, 10))
    g._tick()
    check("縮短果實剪掉 5 節", len(g.body) == 5)
    check("縮短後長度正確", len(g.body) == len(body) + 1 - 1 - 5)

    # -- poison --------------------------------------------------------
    section("毒果")
    g = SnakeGame(seed=12)
    g.set_state([(5, 5), (4, 5), (3, 5), (2, 5), (1, 5), (0, 5)],
                specials=[Pickup("poison", 6, 5, 5.0)], apple=(20, 10))
    g.score = 100
    g.chain = 3
    g.combo_timer = 2.0
    ev = g._tick()
    check("毒果扣 30 分", g.score == 70)
    check("毒果重置連擊", g.chain == 0)
    check("毒果縮短 3 節", len(g.body) == 3)
    check("毒果事件", any(e["type"] == "poison" for e in ev))
    check("毒果不會讓分數變負", g.score >= 0)
    g = SnakeGame(seed=13)
    g.set_state([(5, 5), (4, 5), (3, 5), (2, 5), (1, 5), (0, 5)],
                specials=[Pickup("poison", 6, 5, 5.0)], apple=(20, 10))
    g.score = 10
    g._tick()
    check("低分吃到毒果停在 0", g.score == 0)
    check("毒果最少保留 3 節", len(g.body) >= 3)

    # -- collisions ----------------------------------------------------
    section("碰撞")
    g = SnakeGame(seed=14)
    g.set_state([(COLS - 1, 10), (COLS - 2, 10), (COLS - 3, 10)], apple=(0, 0))
    ev = g._tick()
    check("經典模式撞牆死亡",
          g.phase == "dead" and g.death_reason == "wall")
    check("死亡送出 die 事件", any(e["type"] == "die" for e in ev))
    check("死亡後不再前進", g.step() == [])
    check("死亡後不接受轉向", not g.queue_dir((0, 1)))

    g = SnakeGame(seed=15)
    g.set_state([(5, 5), (4, 5), (4, 6), (5, 6), (6, 6), (6, 5)],
                direction=(0, 1), apple=(0, 0))
    g._tick()
    check("撞到自己死亡",
          g.phase == "dead" and g.death_reason == "self")

    g = SnakeGame(seed=16)
    g.set_state([(5, 5), (4, 5), (4, 6), (5, 6)],
                direction=(0, 1), apple=(0, 0))
    g._tick()
    check("移進尾巴剛空出的格子是安全的", g.phase == "playing" and len(g.body) == 4)

    # -- wrap mode -----------------------------------------------------
    section("穿牆模式")
    g = SnakeGame("wrap", seed=17)
    g.set_state([(COLS - 1, 10), (COLS - 2, 10), (COLS - 3, 10)], apple=(0, 0))
    g._tick()
    check("右緣穿到左緣", g.body[0] == (0, 10) and g.phase == "playing")
    check("穿牆不改變長度", len(g.body) == 3)
    g = SnakeGame("wrap", seed=18)
    g.set_state([(10, 0), (10, 1), (10, 2)], direction=(0, -1), apple=(20, 10))
    g._tick()
    check("上緣穿到下緣", g.body[0] == (10, ROWS - 1))

    g = SnakeGame("wrap", seed=19)
    g.set_state([(29, 10), (28, 10), (27, 10)], apple=(5, 5))
    g.prev_body = list(g.body)
    g._tick()
    x, _ = g.render_body(0.5)[0]
    check("穿牆的插值往另一側滑出", x >= 29.0)

    # -- timers --------------------------------------------------------
    section("倒數與計時")
    g = SnakeGame("sprint", seed=20)
    g.elapsed = MODES["sprint"].duration - 0.001
    ev = g.step()
    check("限時到點會結束",
          g.phase == "dead" and g.death_reason == "time")
    check("限時到點送出事件", any(e["type"] == "die" for e in ev))
    check("限時剩餘時間為 0", g.time_left == 0.0)

    g = SnakeGame(seed=21)
    g.set_state([(5, 5), (4, 5), (3, 5)], apple=(20, 10))
    g.specials = [Pickup("slow", 1, 1, 0.01)]
    ev = g.step()
    check("特殊果實會過期消失", not g.specials)
    check("過期有 expire 事件", any(e["type"] == "expire" for e in ev))

    # -- spawning ------------------------------------------------------
    section("生成規則")
    g = SnakeGame(seed=22)
    g.set_state([(15, 10), (14, 10), (13, 10), (12, 10)])
    ok = True
    for _ in range(50):
        g._spawn_apple()
        if g.apple in set(g.body) or dist(g.apple, g.body[0]) < 4:
            ok = False
    check("蘋果不會生在蛇身上且保持距離", ok)

    g = SnakeGame(seed=23)
    g.set_state([(15, 10), (14, 10), (13, 10), (12, 10)])
    for _ in range(4):
        g._consume("apple", (16, 10))
    check("每 4 顆蘋果生成一個特殊果實", len(g.specials) == 1)
    check("特殊果實有存活時間", all(p.ttl > 0 for p in g.specials))

    g = SnakeGame(seed=24)
    g.set_state([(15, 10), (14, 10), (13, 10), (12, 10)])
    for _ in range(10):
        g.since_special = SPECIAL_EVERY
        g._maybe_spawn_special()
    check("特殊果實同時最多 3 個", len(g.specials) == MAX_SPECIALS)
    check("特殊果實不會生在蛇身上",
          all(p.pos not in set(g.body) for p in g.specials))

    g = SnakeGame(seed=25)
    g.set_state([(15, 10), (14, 10), (13, 10), (12, 10)])
    occupied = set(g.body)
    bad = 0
    for _ in range(200):
        g._spawn_apple()
        if g.apple in occupied or any(p.pos == g.apple for p in g.specials):
            bad += 1
    check("大量重生蘋果都不會重疊", bad == 0)

    a = SnakeGame(seed=77)
    b = SnakeGame(seed=77)
    seq_a, seq_b = [], []
    for _ in range(20):
        a._spawn_apple()
        b._spawn_apple()
        seq_a.append(a.apple)
        seq_b.append(b.apple)
    check("相同 seed 產生相同食物序列", seq_a == seq_b)
    other = SnakeGame(seed=78)
    other._spawn_apple()
    check("不同 seed 會產生不同蘋果", other.apple != seq_a[0])

    # -- rendering helpers --------------------------------------------
    section("插值繪圖")
    g = SnakeGame(seed=26)
    g.set_state([(5, 5), (4, 5), (3, 5), (2, 5)])
    g.prev_body = [(4, 5), (3, 5), (2, 5), (1, 5)]
    check("t=0 時在上一格", g.render_body(0.0)[0] == (4.0, 5.0))
    check("t=1 時在目前格", g.render_body(1.0)[0] == (5.0, 5.0))
    check("t=0.5 時在中間", g.render_body(0.5)[0] == (4.5, 5.0))
    check("插值輸出長度等於蛇長", len(g.render_body(0.5)) == len(g.body))
    check("插值參數會被夾住",
          g.render_body(-5.0)[0] == g.render_body(0.0)[0]
          and g.render_body(9.0)[0] == g.render_body(1.0)[0])

    # -- ascii ---------------------------------------------------------
    section("ASCII 盤面")
    g = SnakeGame(seed=27)
    lines = g.ascii_frame().splitlines()
    check("ASCII 列數正確", len(lines) == ROWS)
    check("ASCII 每列寬度正確", all(len(line) == COLS for line in lines))
    check("ASCII 只有一個蛇頭", sum(line.count("@") for line in lines) == 1)
    check("ASCII 有蘋果", sum(line.count("*") for line in lines) == 1)

    # -- speed / level -------------------------------------------------
    section("速度與等級")
    g = SnakeGame(seed=28)
    check("初始等級 1", g.level == 1)
    g.eaten = 5
    check("吃 5 顆升到等級 2", g.level == 2)
    g.eaten = 12
    check("吃 12 顆是等級 3", g.level == 3)
    g.eaten = 999
    check("速度有下限", g.interval >= MODES["classic"].floor - 1e-9)
    check("速度不會變負", g.interval > 0)
    slow_interval = g.interval
    g.slow_timer = 1.0
    check("減速讓間隔更長", g.interval > slow_interval)

    # -- high score ----------------------------------------------------
    section("最高分存檔")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "hs.json")
        check("首次存檔即新紀錄",
              save_highscore(path, "classic",
                             {"score": 100, "length": 9, "max_chain": 3,
                              "elapsed": 5.0}))
        data = load_highscores(path)
        check("分數寫入檔案", data.get("classic", {}).get("score") == 100)
        check("較低分不覆蓋",
              not save_highscore(path, "classic", {"score": 99}))
        check("較高分會覆蓋",
              save_highscore(path, "classic", {"score": 150}))
        check("各模式分開儲存",
              save_highscore(path, "wrap", {"score": 80})
              and load_highscores(path).get("wrap", {}).get("score") == 80)
        broken = os.path.join(tmp, "broken.json")
        with open(broken, "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        check("壞掉的存檔不會崩潰", load_highscores(broken) == {})

    # -- stats ---------------------------------------------------------
    section("統計")
    g = SnakeGame(seed=29)
    g.score = 42
    g.eaten = 3
    g.max_chain = 2
    g._die("test")
    st = g.stats()
    check("stats 分數正確", st["score"] == 42)
    check("stats 長度正確", st["length"] == 4)
    check("stats 原因正確", st["reason"] == "test")
    check("死亡後 final_stats 已填好", g.final_stats is not None)

    # -- stress --------------------------------------------------------
    section("隨機操作壓力測試")
    rng = random.Random(1234)
    stress_ok = True
    runs = 0
    for mode in MODES:
        for s in range(3):
            game = SnakeGame(mode, seed=s)
            runs += 1
            for _ in range(1500):
                if rng.random() < 0.07:
                    game.queue_dir(rng.choice(((1, 0), (-1, 0), (0, 1), (0, -1))))
                game.step()
                if game.phase != "playing":
                    if game.death_reason not in ("wall", "self", "time"):
                        stress_ok = False
                    break
                body = list(game.body)
                if len(set(body)) != len(body):
                    stress_ok = False
                if len(body) < 3 or game.score < 0:
                    stress_ok = False
                if game.apple is not None and game.apple in set(body):
                    stress_ok = False
                if any(p.pos in set(body) for p in game.specials):
                    stress_ok = False
    check("九場隨機操作沒有崩潰、蛇身沒有重疊", stress_ok)
    check("壓力測試真的有跑到", runs == 9)

    # -- summary -------------------------------------------------------
    if failures:
        print(f"\n{len(failures)} / {checks} 項失敗：")
        for name in failures:
            print(f"  - {name}")
        return 1
    print(f"\n全部 {checks} 項檢查通過。")
    return 0


# --------------------------------------------------------------------------
# Headless text mode (logic smoke test / debugging)
# --------------------------------------------------------------------------

def run_text(mode: str, seed: int | None, seconds: float) -> int:
    game = SnakeGame(mode=mode, seed=seed)
    for _ in range(max(1, int(seconds * FPS))):
        game.step()
        if game.phase != "playing":
            break
    print(game.ascii_frame())
    st = game.stats()
    print(f"\nmode={st['mode']} score={st['score']} length={st['length']} "
          f"eaten={st['eaten']} elapsed={st['elapsed']:.2f}s "
          f"phase={game.phase} reason={st['reason']}")
    return 0



# --------------------------------------------------------------------------
# Presentation layer (pygame)
# --------------------------------------------------------------------------
#
# Everything below is only imported when the GUI needs it.  The logic layer
# above stays importable (and testable) on machines without pygame.

C_BG_TOP = (5, 8, 16)
C_BG_BOT = (12, 18, 34)
C_PANEL = (12, 20, 35)
C_PANEL_HI = (16, 27, 46)
C_BOARD = (9, 15, 28)
C_GRID = (17, 29, 50)
C_GRID_MAJOR = (24, 40, 66)
C_LINE = (30, 50, 76)
C_DIM = (112, 136, 166)
C_TEXT = (228, 242, 255)
C_GREEN = (90, 255, 170)
C_LIME = (180, 255, 120)
C_CYAN = (90, 215, 255)
C_GOLD = (255, 205, 90)
C_RED = (255, 90, 100)
C_PURPLE = (175, 110, 255)
C_PINK = (255, 110, 190)

MODE_COLORS = {
    "classic": (90, 255, 170),
    "wrap": (90, 215, 255),
    "sprint": (255, 205, 90),
}

PICKUP_COLORS = {
    "apple": (255, 110, 96),
    "golden": (255, 208, 92),
    "slow": (92, 222, 255),
    "double": (255, 190, 96),
    "trim": (126, 244, 176),
    "poison": (186, 110, 255),
}

MODE_KEYS = list(MODES)
CARD_W, CARD_H, CARD_GAP, CARD_Y = 232, 168, 22, 236


def clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v


def mix_color(a: tuple[int, int, int], b: tuple[int, int, int], t: float):
    t = clamp01(t)
    return (int(a[0] + (b[0] - a[0]) * t),
            int(a[1] + (b[1] - a[1]) * t),
            int(a[2] + (b[2] - a[2]) * t))


def shade(c: tuple[int, int, int], f: float):
    return (max(0, min(255, int(c[0] * f))),
            max(0, min(255, int(c[1] * f))),
            max(0, min(255, int(c[2] * f))))


def combo_color(chain: int):
    if chain <= 2:
        return (110, 255, 170)
    if chain <= 4:
        return (255, 232, 120)
    if chain <= 6:
        return (255, 168, 96)
    return (255, 110, 190)


def snake_color(t: float):
    """Head-to-tail neon gradient: lime -> mint -> cyan blue."""
    t = clamp01(t)
    if t < 0.5:
        return mix_color((176, 255, 122), (70, 245, 182), t / 0.5)
    return mix_color((70, 245, 182), (78, 158, 255), (t - 0.5) / 0.5)


def fmt_int(n: int) -> str:
    return f"{int(n):,}"


def fmt_clock(sec: float) -> str:
    sec = max(0.0, sec)
    m = int(sec) // 60
    s = int(sec) % 60
    return f"{m}:{s:02d}"


def star_points(cx: float, cy: float, outer: float, inner: float,
                points: int = 5, rot: float = 0.0):
    out = []
    for i in range(points * 2):
        r = outer if i % 2 == 0 else inner
        a = rot - math.pi / 2 + i * math.pi / points
        out.append((cx + math.cos(a) * r, cy + math.sin(a) * r))
    return out


def wrap_text(font, text: str, max_w: int) -> list[str]:
    lines, cur = [], ""
    for ch in text:
        if ch == " " and not cur:
            continue
        test = cur + ch
        if cur and font.size(test)[0] > max_w:
            lines.append(cur)
            cur = ch
        else:
            cur = test
    if cur:
        lines.append(cur)
    return lines


# --------------------------------------------------------------------------
# Fonts / text cache
# --------------------------------------------------------------------------

class FontBook:
    """Loads a CJK face and a geometric display face, and caches rendered text."""

    CJK_NAMES = ("pingfang", "stheitimedium", "stheitilight", "hiraginosansgb",
                 "notosanscjk", "notosanssc", "sourcehansans", "microsoftyahei",
                 "simhei", "arialunicode")
    DISPLAY_NAMES = ("avenirnext", "avenirnextcondensed", "futura",
                     "helveticaneue", "arial")

    def __init__(self) -> None:
        self._fonts: dict = {}
        self._text: dict = {}
        self.cjk_path = self._match(self.CJK_NAMES)
        self.display_path = self._match(self.DISPLAY_NAMES) or self.cjk_path

    @staticmethod
    def _match(names):
        for name in names:
            try:
                path = pygame.font.match_font(name)
            except Exception:
                path = None
            if path:
                return path
        return None

    def font(self, size: int, bold: bool = False, display: bool = False):
        key = (size, bold, display)
        f = self._fonts.get(key)
        if f is None:
            path = self.display_path if display else self.cjk_path
            try:
                f = pygame.font.Font(path, size)
            except Exception:
                f = pygame.font.Font(None, size)
            f.set_bold(bold)
            self._fonts[key] = f
        return f

    def text(self, txt: str, size: int, color, bold: bool = False,
             display: bool = False, alpha: int = 255):
        key = (txt, size, tuple(color), bold, display, int(alpha))
        surf = self._text.get(key)
        if surf is None:
            if len(self._text) > 700:
                self._text.clear()
            surf = self.font(size, bold, display).render(txt, True, color)
            if alpha < 255:
                surf = surf.copy()
                surf.set_alpha(int(alpha))
            self._text[key] = surf
        return surf


# --------------------------------------------------------------------------
# Synthesised sound effects (numpy -> pygame.mixer)
# --------------------------------------------------------------------------

class SoundBank:
    """Tiny synth: everything is generated at start-up, no asset files."""

    RATE = 44100

    def __init__(self, enabled: bool = True) -> None:
        self.muted = False
        self.sounds: dict = {}
        self.enabled = bool(enabled and pygame is not None
                            and pygame.mixer.get_init())
        if not self.enabled:
            return
        try:
            import numpy as np  # local import: GUI-only dependency
            self._np = np
            self._build()
        except Exception:
            self.enabled = False
            self.sounds = {}

    # -- synth primitives ---------------------------------------------
    def _tone(self, freq: float, dur: float, freq_end: float | None = None,
              wave: str = "sine", vol: float = 0.3, noise: float = 0.0,
              attack: float = 0.004, release: float = 0.015,
              decay: float = 2.0):
        np = self._np
        n = max(1, int(self.RATE * dur))
        t = np.arange(n, dtype=np.float64) / self.RATE
        f0 = float(freq)
        f1 = float(freq if freq_end is None else freq_end)
        freqs = np.linspace(f0, f1, n)
        phase = 2 * np.pi * np.cumsum(freqs) / self.RATE
        if wave == "sine":
            s = np.sin(phase) + 0.28 * np.sin(2 * phase) + 0.12 * np.sin(3 * phase)
        elif wave == "square":
            s = np.sign(np.sin(phase)) * 0.7 + 0.3 * np.sin(2 * phase)
        elif wave == "saw":
            s = 2.0 * ((phase / (2 * np.pi)) % 1.0) - 1.0
        else:
            s = np.sin(phase)
        if noise > 0:
            rng = np.random.default_rng(7)
            s = (1.0 - noise) * s + noise * rng.uniform(-1.0, 1.0, n)
        env = np.ones(n)
        a = max(1, int(attack * self.RATE))
        env[:a] = np.linspace(0.0, 1.0, a)
        r = max(1, int(release * self.RATE))
        env[-r:] *= np.linspace(1.0, 0.0, r)
        env *= np.exp(-t / max(0.01, dur) * decay)
        return np.clip(s * env * vol, -1.0, 1.0)

    def _concat(self, parts, gap: float = 0.0):
        np = self._np
        silence = np.zeros(max(1, int(self.RATE * gap))) if gap > 0 else None
        out = []
        for i, p in enumerate(parts):
            if i and silence is not None:
                out.append(silence)
            out.append(p)
        return np.concatenate(out)

    def _sound(self, arr):
        np = self._np
        data = (np.clip(arr, -1.0, 1.0) * 32767).astype(np.int16)
        stereo = np.column_stack((data, data))
        return pygame.sndarray.make_sound(stereo)

    def _build(self) -> None:
        np = self._np
        # eating: one sound per combo step, pitch climbs with the multiplier
        for i in range(1, COMBO_MAX + 1):
            step = i - 1
            f = 600.0 * (1.0595 ** (step * 2))
            self.sounds[f"eat{i}"] = self._sound(
                self._tone(f, 0.085, f * 1.35, vol=0.26, decay=3.2))
        # golden apple: sparkly arpeggio
        self.sounds["golden"] = self._sound(self._concat([
            self._tone(880, 0.07, 990, vol=0.24, decay=2.0),
            self._tone(1108, 0.07, 1245, vol=0.24, decay=2.0),
            self._tone(1396, 0.16, 1760, vol=0.22, decay=1.6),
        ], gap=0.008))
        # power-up: rising sweep
        self.sounds["power"] = self._sound(self._concat([
            self._tone(520, 0.10, 780, wave="sine", vol=0.22, decay=1.4),
            self._tone(780, 0.16, 1560, wave="sine", vol=0.18, decay=1.2),
        ], gap=0.005))
        # poison: ugly descending buzz
        self.sounds["poison"] = self._sound(self._tone(
            240, 0.32, 65, wave="saw", vol=0.30, noise=0.28, decay=1.2))
        # death: falling tone + noise burst
        self.sounds["die"] = self._sound(self._concat([
            self._tone(330, 0.55, 48, wave="saw", vol=0.28, noise=0.12, decay=1.0),
            self._tone(120, 0.30, 40, wave="sine", vol=0.18, noise=0.25, decay=1.4),
        ], gap=0.01))
        self.sounds["click"] = self._sound(
            self._tone(300, 0.045, 220, wave="sine", vol=0.16, decay=5.0))
        self.sounds["start"] = self._sound(self._concat([
            self._tone(523, 0.09, 523, vol=0.18, decay=2.2),
            self._tone(659, 0.09, 659, vol=0.18, decay=2.2),
            self._tone(784, 0.09, 784, vol=0.18, decay=2.2),
            self._tone(1046, 0.22, 1046, vol=0.20, decay=1.6),
        ], gap=0.01))
        self.sounds["timeup"] = self._sound(self._concat([
            self._tone(880, 0.16, 880, wave="square", vol=0.16, decay=2.0),
            self._tone(660, 0.16, 660, wave="square", vol=0.16, decay=2.0),
            self._tone(440, 0.34, 440, wave="square", vol=0.16, decay=1.6),
        ], gap=0.02))

    # -- playback ------------------------------------------------------
    def play(self, name: str) -> None:
        if not self.enabled or self.muted:
            return
        snd = self.sounds.get(name)
        if snd is not None:
            try:
                snd.play()
            except Exception:
                pass

    def toggle(self) -> bool:
        self.muted = not self.muted
        if not self.muted:
            self.play("click")
        return self.muted


# --------------------------------------------------------------------------
# Particles / floating score labels / screen shake
# --------------------------------------------------------------------------

@dataclass
class Particle:
    x: float
    y: float
    vx: float
    vy: float
    life: float
    max_life: float
    size: float
    color: tuple
    drag: float = 2.4
    gravity: float = 40.0


@dataclass
class Floater:
    text: str
    x: float
    y: float
    vy: float
    life: float
    max_life: float
    color: tuple
    size: int


class FX:
    """All transient juice: particles, score popups, shake and screen flashes."""

    def __init__(self) -> None:
        self.particles: list[Particle] = []
        self.floaters: list[Floater] = []
        self.shake = 0.0
        self.flashes: list[list] = []       # [color, life, max_life, alpha]
        self.rng = random.Random(4242)
        self.t = 0.0

    def clear(self) -> None:
        self.particles.clear()
        self.floaters.clear()
        self.shake = 0.0
        self.flashes.clear()

    def burst(self, x: float, y: float, color, n: int = 10,
              speed: float = 170.0, size: float = 3.4,
              life: float = 0.55, gravity: float = 50.0) -> None:
        rng = self.rng
        for _ in range(n):
            ang = rng.uniform(0.0, math.tau)
            spd = rng.uniform(speed * 0.25, speed)
            self.particles.append(Particle(
                x, y,
                math.cos(ang) * spd, math.sin(ang) * spd,
                life * rng.uniform(0.6, 1.25), life,
                size * rng.uniform(0.6, 1.4),
                mix_color(color, (255, 255, 255), rng.uniform(0.0, 0.25)),
                drag=rng.uniform(1.8, 3.2), gravity=gravity,
            ))
        if len(self.particles) > 420:
            del self.particles[:len(self.particles) - 420]

    def ring(self, x: float, y: float, color, n: int = 14,
             speed: float = 230.0) -> None:
        for i in range(n):
            ang = i / n * math.tau
            self.particles.append(Particle(
                x, y, math.cos(ang) * speed, math.sin(ang) * speed,
                0.4, 0.4, 2.6, color, drag=3.4, gravity=0.0))

    def shake_add(self, amount: float) -> None:
        self.shake = min(16.0, self.shake + amount)

    def flash(self, color, alpha: int = 70, dur: float = 0.22) -> None:
        self.flashes.append([color, dur, dur, alpha])

    def floater(self, text: str, x: float, y: float, color, size: int = 18,
                life: float = 0.95, vy: float = -52.0) -> None:
        self.floaters.append(Floater(text, x, y, vy, life, life, color, size))

    def update(self, dt: float) -> None:
        self.t += dt
        alive = []
        for p in self.particles:
            p.life -= dt
            if p.life <= 0:
                continue
            p.x += p.vx * dt
            p.y += p.vy * dt
            damp = max(0.0, 1.0 - p.drag * dt)
            p.vx *= damp
            p.vy = p.vy * damp + p.gravity * dt
            alive.append(p)
        self.particles = alive

        stays = []
        for f in self.floaters:
            f.life -= dt
            if f.life <= 0:
                continue
            f.y += f.vy * dt
            f.vy *= max(0.0, 1.0 - 1.6 * dt)
            stays.append(f)
        self.floaters = stays

        for fl in self.flashes:
            fl[1] -= dt
        self.flashes = [f for f in self.flashes if f[1] > 0]
        self.shake = max(0.0, self.shake - dt * 42.0)

    def shake_offset(self) -> tuple[int, int]:
        if self.shake < 0.4:
            return (0, 0)
        amp = self.shake * 0.55
        return (int(self.rng.uniform(-amp, amp)),
                int(self.rng.uniform(-amp, amp)))


# --------------------------------------------------------------------------
# Renderer
# --------------------------------------------------------------------------

class Renderer:
    """Draws the menu, the board, the snake, effects and every overlay."""

    def __init__(self, screen, fonts: FontBook) -> None:
        self.screen = screen
        self.fonts = fonts
        self.glow_cache: dict = {}
        self.glow_text_cache: dict = {}
        self.dim_cache: dict = {}
        self.bg = self._build_bg()
        self.board_bg = self._build_board_bg()
        self.dust = self._build_dust()

    # -- cached primitives --------------------------------------------
    def _glow(self, radius: int, color, strength: float = 0.75):
        radius = max(2, int(radius))
        key = (radius, tuple(color))
        surf = self.glow_cache.get(key)
        if surf is not None:
            return surf
        surf = pygame.Surface((radius * 2, radius * 2))
        surf.fill((0, 0, 0))
        for r in range(radius, 0, -1):
            t = 1.0 - r / radius
            f = t * t * strength
            pygame.draw.circle(
                surf,
                (min(255, int(color[0] * f)),
                 min(255, int(color[1] * f)),
                 min(255, int(color[2] * f))),
                (radius, radius), r)
        self.glow_cache[key] = surf
        return surf

    def _glow_text(self, text: str, size: int, color, glow_color,
                   bold: bool = False, display: bool = False):
        key = (text, size, tuple(color), tuple(glow_color), bold, display)
        hit = self.glow_text_cache.get(key)
        if hit is not None:
            return hit
        font = self.fonts.font(size, bold, display)
        core = font.render(text, True, color)
        w, h = core.get_size()
        pad = max(8, size // 4)
        canvas = pygame.Surface((w + pad * 2, h + pad * 2))
        canvas.fill((0, 0, 0))
        canvas.blit(font.render(text, True, glow_color), (pad, pad))
        small = pygame.transform.smoothscale(
            canvas, (max(1, canvas.get_width() // 4),
                     max(1, canvas.get_height() // 4)))
        halo = pygame.transform.smoothscale(small, canvas.get_size())
        result = (halo, core, pad)
        self.glow_text_cache[key] = result
        return result

    def draw_glow_text(self, surface, text: str, size: int, color, glow_color,
                       center, bold: bool = False, display: bool = False,
                       passes: int = 1) -> None:
        halo, core, pad = self._glow_text(text, size, color, glow_color,
                                          bold, display)
        x = int(center[0] - core.get_width() / 2)
        y = int(center[1] - core.get_height() / 2)
        for _ in range(max(1, passes)):
            surface.blit(halo, (x - pad, y - pad),
                         special_flags=pygame.BLEND_ADD)
        surface.blit(core, (x, y))

    def _dim(self, surface, alpha: int) -> None:
        alpha = max(0, min(230, int(alpha)))
        if alpha <= 0:
            return
        key = alpha
        ov = self.dim_cache.get(key)
        if ov is None:
            ov = pygame.Surface((WIN_W, WIN_H))
            ov.fill((0, 0, 0))
            ov.set_alpha(alpha)
            self.dim_cache[key] = ov
        surface.blit(ov, (0, 0))

    def _panel(self, surface, rect, fill=C_PANEL, border=C_LINE,
               radius: int = 16, width: int = 1, alpha: int = 255) -> None:
        rect = pygame.Rect(rect)
        if alpha < 255:
            tmp = pygame.Surface(rect.size, pygame.SRCALPHA)
            pygame.draw.rect(tmp, (*fill[:3], alpha), tmp.get_rect(),
                             border_radius=radius)
            if border is not None:
                pygame.draw.rect(tmp, (*border[:3], min(255, alpha + 60)),
                                 tmp.get_rect(), width, border_radius=radius)
            surface.blit(tmp, rect.topleft)
        else:
            pygame.draw.rect(surface, fill[:3], rect, border_radius=radius)
            if border is not None:
                pygame.draw.rect(surface, border[:3], rect, width,
                                 border_radius=radius)

    # -- static background --------------------------------------------
    def _build_bg(self):
        surf = pygame.Surface((WIN_W, WIN_H))
        for y in range(WIN_H):
            surf.fill(mix_color(C_BG_TOP, C_BG_BOT, y / WIN_H),
                      (0, y, WIN_W, 1))
        surf.blit(self._glow(380, (0, 62, 52), 0.55),
                  (WIN_W // 2 - 380, -220), special_flags=pygame.BLEND_ADD)
        surf.blit(self._glow(300, (12, 40, 70), 0.8),
                  (-220, WIN_H - 320), special_flags=pygame.BLEND_ADD)
        surf.blit(self._glow(260, (40, 18, 62), 0.7),
                  (WIN_W - 280, WIN_H - 260), special_flags=pygame.BLEND_ADD)

        rng = random.Random(11)
        for _ in range(110):
            x, y = rng.randrange(WIN_W), rng.randrange(WIN_H)
            c = mix_color((16, 24, 40), (44, 68, 96), rng.random())
            surf.set_at((x, y), c)

        small = pygame.Surface((96, 96), pygame.SRCALPHA)
        for y in range(96):
            for x in range(96):
                dx = (x - 48) / 48.0
                dy = (y - 48) / 48.0
                d = min(1.0, math.hypot(dx, dy))
                a = int(150 * max(0.0, (d - 0.36) / 0.64) ** 1.6)
                small.set_at((x, y), (0, 0, 0, a))
        surf.blit(pygame.transform.smoothscale(small, (WIN_W, WIN_H)), (0, 0))
        return surf

    def _build_board_bg(self):
        surf = pygame.Surface((BOARD_W, BOARD_H), pygame.SRCALPHA)
        pygame.draw.rect(surf, (*C_BOARD, 255),
                         pygame.Rect(0, 0, BOARD_W, BOARD_H),
                         border_radius=14)
        for x in range(CELL, BOARD_W, CELL):
            col = (*C_GRID_MAJOR, 255) if (x // CELL) % 5 == 0 else (*C_GRID, 255)
            pygame.draw.line(surf, col, (x, 6), (x, BOARD_H - 6))
        for y in range(CELL, BOARD_H, CELL):
            col = (*C_GRID_MAJOR, 255) if (y // CELL) % 5 == 0 else (*C_GRID, 255)
            pygame.draw.line(surf, col, (6, y), (BOARD_W - 6, y))
        # fade the grid near the border
        edge = pygame.Surface((BOARD_W, BOARD_H), pygame.SRCALPHA)
        for i in range(26):
            a = int(160 * (1.0 - i / 26.0) ** 2)
            pygame.draw.rect(edge, (*C_BOARD, a),
                             pygame.Rect(i, i, BOARD_W - 2 * i, BOARD_H - 2 * i),
                             1)
        surf.blit(edge, (0, 0))
        # rounded-corner mask
        mask = pygame.Surface((BOARD_W, BOARD_H), pygame.SRCALPHA)
        pygame.draw.rect(mask, (255, 255, 255, 255),
                         pygame.Rect(0, 0, BOARD_W, BOARD_H), border_radius=14)
        surf.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
        return surf

    def _build_dust(self):
        rng = random.Random(23)
        dust = []
        for _ in range(46):
            dust.append((rng.uniform(0, WIN_W), rng.uniform(0, WIN_H),
                         rng.uniform(6, 22), rng.choice((1, 1, 1, 2)),
                         mix_color((22, 36, 58), (60, 96, 130), rng.random())))
        return dust

    def draw_background(self, surface, t: float) -> None:
        surface.blit(self.bg, (0, 0))
        for x, y0, spd, r, col in self.dust:
            y = (y0 - t * spd) % (WIN_H + 60) - 30
            pygame.draw.circle(surface, col, (int(x), int(y)), r)

    # -- helpers -------------------------------------------------------
    def _cell_px(self, cx: float, cy: float, ox: int = 0, oy: int = 0):
        return (BOARD_X + (cx + 0.5) * CELL + ox,
                BOARD_Y + (cy + 0.5) * CELL + oy)

    def card_rect(self, i: int):
        total = len(MODE_KEYS) * CARD_W + (len(MODE_KEYS) - 1) * CARD_GAP
        x0 = (WIN_W - total) // 2
        return pygame.Rect(x0 + i * (CARD_W + CARD_GAP), CARD_Y, CARD_W, CARD_H)

    def _blit_center(self, surface, img, center):
        surface.blit(img, (int(center[0] - img.get_width() / 2),
                           int(center[1] - img.get_height() / 2)))

    def _blit_right(self, surface, img, right: int, top: int):
        surface.blit(img, (int(right - img.get_width()), top))

    # -- world drawing -------------------------------------------------
    @staticmethod
    def _corner_brackets(surface, rect, color, length: int = 20) -> None:
        for sx, sy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
            x = rect.left if sx > 0 else rect.right
            y = rect.top if sy > 0 else rect.bottom
            pygame.draw.line(surface, color, (x, y), (x + sx * length, y), 2)
            pygame.draw.line(surface, color, (x, y), (x, y + sy * length), 2)

    def _draw_snake(self, surface, game: SnakeGame, t: float,
                    ox: int, oy: int) -> None:
        if not game.body:
            return
        if game.phase == "dead" and game.death_reason != "time":
            return
        body = game.render_body()
        pts = [self._cell_px(x, y, ox, oy) for x, y in body]
        n = len(pts)
        clip = pygame.Rect(BOARD_X + ox + 2, BOARD_Y + oy + 2,
                           BOARD_W - 4, BOARD_H - 4)
        surface.set_clip(clip)

        # bloom along the body (quantised colours keep the cache small)
        step = max(1, n // 34)
        for i in range(0, n, step):
            col = snake_color(round(i / max(1, n - 1) * 7) / 7)
            g = self._glow(20, shade(col, 0.6))
            surface.blit(g, (pts[i][0] - 20, pts[i][1] - 20),
                         special_flags=pygame.BLEND_ADD)

        # split the polyline where the snake wraps across the board edge
        runs: list[list[int]] = []
        cur = [0]
        for i in range(1, n):
            if (abs(pts[i][0] - pts[i - 1][0]) > CELL * 1.55 or
                    abs(pts[i][1] - pts[i - 1][1]) > CELL * 1.55):
                runs.append(cur)
                cur = [i]
            else:
                cur.append(i)
        runs.append(cur)

        width = int(CELL * 0.76)
        dark_w = width + 6
        # dark casing
        for run in runs:
            for a, b in zip(run, run[1:]):
                pygame.draw.line(surface, (10, 26, 26), pts[a], pts[b], dark_w)
            for i in run:
                pygame.draw.circle(surface, (10, 26, 26),
                                   (int(pts[i][0]), int(pts[i][1])), dark_w // 2)
        # gradient body
        for run in runs:
            for a, b in zip(run, run[1:]):
                col = snake_color(((a + b) * 0.5) / max(1, n - 1))
                pygame.draw.line(surface, col, pts[a], pts[b], width)
            for i in run:
                col = snake_color(i / max(1, n - 1))
                pygame.draw.circle(surface, col,
                                   (int(pts[i][0]), int(pts[i][1])), width // 2)
        # glossy highlight
        for run in runs:
            for a, b in zip(run, run[1:]):
                col = mix_color(snake_color(a / max(1, n - 1)),
                                (255, 255, 255), 0.42)
                pygame.draw.line(surface, col, pts[a], pts[b], 5)
            for i in run[::3]:
                col = mix_color(snake_color(i / max(1, n - 1)),
                                (255, 255, 255), 0.55)
                pygame.draw.circle(surface, col,
                                   (int(pts[i][0]), int(pts[i][1])), 2)

        # head: two overlapping discs read as a rounded snout
        hx, hy = pts[0]
        dx, dy = game.dir
        px, py = -dy, dx
        head_col = snake_color(0.0)
        fx0, fy0 = hx + dx * 5.0, hy + dy * 5.0
        g = self._glow(32, shade(head_col, 0.42))
        surface.blit(g, (hx - 32, hy - 32), special_flags=pygame.BLEND_ADD)
        for (cx0, cy0, r0) in ((hx, hy, 16), (fx0, fy0, 12)):
            pygame.draw.circle(surface, (10, 30, 26),
                               (int(cx0), int(cy0)), r0)
        pygame.draw.circle(surface, head_col, (int(hx), int(hy)), 14)
        pygame.draw.circle(surface, head_col, (int(fx0), int(fy0)), 10)
        pygame.draw.circle(surface, mix_color(head_col, (255, 255, 255), 0.45),
                           (int(hx), int(hy)), 14, 2)
        for side in (-1, 1):
            ex = hx + dx * 6.0 + px * 5.5 * side
            ey = hy + dy * 6.0 + py * 5.5 * side
            pygame.draw.circle(surface, (246, 255, 255), (int(ex), int(ey)), 4)
            pygame.draw.circle(surface, (10, 22, 30),
                               (int(ex + dx * 1.8), int(ey + dy * 1.8)), 2.2)
        if int(t * 2.6) % 4 == 0 and game.phase == "playing":
            pygame.draw.line(surface, (255, 90, 120),
                             (int(hx + dx * 15), int(hy + dy * 15)),
                             (int(hx + dx * 21), int(hy + dy * 21)), 2)
            pygame.draw.line(surface, (255, 120, 150),
                             (int(hx + dx * 21), int(hy + dy * 21)),
                             (int(hx + dx * 24 - py * 3),
                              int(hy + dy * 24 + px * 3)), 2)
        surface.set_clip(None)

    def _draw_item(self, surface, kind: str, cell, t: float,
                   ox: int, oy: int, ttl_frac: float | None) -> None:
        cx0, cy0 = cell
        bob = math.sin(t * 3.2 + cx0 * 0.7 + cy0 * 0.3) * 1.6
        cx, cy = self._cell_px(cx0, cy0, ox, oy)
        cx = int(cx)
        cy = int(cy + bob)
        color = PICKUP_COLORS.get(kind, (255, 255, 255))
        pulse = 0.55 + 0.45 * math.sin(t * 5.0 + cx0)
        gr = 28 if kind != "golden" else 34
        g = self._glow(gr, shade(color, 0.35 + 0.25 * pulse))
        surface.blit(g, (cx - gr, cy - gr), special_flags=pygame.BLEND_ADD)

        if kind == "apple":
            r = 9.0 + math.sin(t * 6.0) * 0.8
            pygame.draw.circle(surface, (120, 26, 36), (cx, cy), int(r) + 2)
            pygame.draw.circle(surface, (255, 108, 96), (cx, cy), int(r))
            pygame.draw.circle(surface, (255, 206, 178), (cx - 3, cy - 3), 2.5)
            pygame.draw.line(surface, (150, 232, 150),
                             (cx, cy - 9), (cx + 2, cy - 15), 2)
            pygame.draw.ellipse(surface, (118, 240, 150),
                                (cx + 2, cy - 19, 8, 5))
        elif kind == "golden":
            rot = t * 1.3
            pygame.draw.polygon(surface, (140, 90, 20),
                                star_points(cx, cy, 14, 6.5, 5, rot))
            pygame.draw.polygon(surface, (255, 202, 80),
                                star_points(cx, cy, 12.5, 5.2, 5, rot))
            pygame.draw.polygon(surface, (255, 244, 190),
                                star_points(cx, cy, 6.5, 2.8, 5, rot))
        elif kind == "poison":
            pygame.draw.circle(surface, (60, 26, 110), (cx, cy), 11)
            pygame.draw.circle(surface, (96, 52, 160), (cx, cy), 9)
            pygame.draw.circle(surface, (206, 150, 255), (cx, cy), 10, 2)
            for k in range(3):
                ph = (t * 0.7 + k * 0.33) % 1.0
                bx = cx - 4 + k * 4
                by = cy + 7 - int(ph * 15)
                pygame.draw.circle(surface, (214, 168, 255), (bx, by), 1.5)
            pygame.draw.circle(surface, (242, 232, 255), (cx - 4, cy - 2), 2)
            pygame.draw.circle(surface, (242, 232, 255), (cx + 4, cy - 2), 2)
            pygame.draw.line(surface, (242, 232, 255),
                             (cx - 3, cy + 4), (cx + 3, cy + 4), 1)
        elif kind == "slow":
            pygame.draw.circle(surface, (44, 74, 110), (cx, cy), 11)
            pygame.draw.circle(surface, (92, 222, 255), (cx, cy), 10, 2)
            for k in range(4):
                a = t * 0.9 + k * math.pi / 4
                ex, ey = cx + math.cos(a) * 10, cy + math.sin(a) * 10
                pygame.draw.line(surface, (176, 244, 255),
                                 (cx, cy), (int(ex), int(ey)), 2)
            pygame.draw.circle(surface, (222, 252, 255), (cx, cy), 2)
        elif kind == "double":
            pygame.draw.rect(surface, (58, 38, 12),
                             (cx - 11, cy - 11, 22, 22), border_radius=6)
            pygame.draw.rect(surface, (255, 196, 96),
                             (cx - 11, cy - 11, 22, 22), 2, border_radius=6)
            self._blit_center(
                surface,
                self.fonts.text("×2", 12, (255, 228, 160), bold=True,
                                display=True),
                (cx, cy + 1))
        elif kind == "trim":
            pygame.draw.circle(surface, (28, 60, 44), (cx, cy), 11)
            pygame.draw.circle(surface, (126, 244, 176), (cx, cy), 10, 2)
            for k in (-1, 0):
                yy = cy - 3 + k * 6
                pygame.draw.lines(surface, (206, 255, 222), False,
                                  [(cx - 5, yy), (cx, yy + 4), (cx + 5, yy)], 2)

        if ttl_frac is not None:
            frac = clamp01(ttl_frac)
            rect = pygame.Rect(cx - 17, cy - 17, 34, 34)
            try:
                pygame.draw.arc(surface, shade(color, 0.6), rect,
                                -math.pi / 2, -math.pi / 2 + math.tau * frac, 2)
            except Exception:
                pass

    def _draw_food(self, surface, game: SnakeGame, t: float,
                   ox: int, oy: int) -> None:
        clip = pygame.Rect(BOARD_X + ox + 2, BOARD_Y + oy + 2,
                           BOARD_W - 4, BOARD_H - 4)
        surface.set_clip(clip)
        if game.apple is not None:
            self._draw_item(surface, "apple", game.apple, t, ox, oy, None)
        for p in game.specials:
            ttl = PICKUPS[p.kind].ttl or 8.0
            self._draw_item(surface, p.kind, (p.x, p.y), t, ox, oy,
                            p.ttl / ttl)
        surface.set_clip(None)

    def _draw_particles(self, surface, fx: FX, ox: int, oy: int) -> None:
        clip = pygame.Rect(BOARD_X + ox + 2, BOARD_Y + oy + 2,
                           BOARD_W - 4, BOARD_H - 4)
        surface.set_clip(clip)
        for p in fx.particles:
            a = clamp01(p.life / p.max_life)
            if a <= 0.02:
                continue
            x, y = int(p.x + ox), int(p.y + oy)
            r = max(1, int(p.size * a + 0.5))
            pygame.draw.circle(surface, shade(p.color, 0.30 * a), (x, y), r * 2)
            pygame.draw.circle(surface, shade(p.color, a), (x, y), r)
        surface.set_clip(None)

    def _draw_floaters(self, surface, fx: FX) -> None:
        clip = pygame.Rect(BOARD_X - 10, BOARD_Y - 26,
                           BOARD_W + 20, BOARD_H + 46)
        surface.set_clip(clip)
        for f in fx.floaters:
            a = clamp01(f.life / f.max_life)
            img = self.fonts.font(f.size, True, display=False).render(
                f.text, True, f.color)
            img = img.copy()
            img.set_alpha(int(255 * a))
            surface.blit(img, (int(f.x - img.get_width() / 2),
                               int(f.y - img.get_height() / 2)))
        surface.set_clip(None)

    def draw_flash(self, surface, fx: FX) -> None:
        for color, life, max_life, alpha in fx.flashes:
            a = int(alpha * clamp01(life / max_life))
            if a <= 0:
                continue
            ov = pygame.Surface((WIN_W, WIN_H), pygame.SRCALPHA)
            ov.fill((*color[:3], a))
            surface.blit(ov, (0, 0))

    # -- HUD -----------------------------------------------------------
    def draw_hud(self, app, surface) -> None:
        game = app.game
        if game is None:
            return

        # score (left)
        surface.blit(self.fonts.text("分數", 10, C_DIM), (60, 12))
        size = 30 + int(10 * app.punch)
        score_img = self.fonts.text(fmt_int(game.score), size, C_TEXT,
                                    bold=True, display=True)
        surface.blit(score_img, (60, 26))

        # personal best (right)
        best = app.best_for(game.mode)
        label = self.fonts.text("最高", 10, C_DIM)
        self._blit_right(surface, label, 840, 12)
        best_img = self.fonts.text(fmt_int(best), 20,
                                   C_GOLD if best > 0 else C_DIM,
                                   bold=True, display=True)
        self._blit_right(surface, best_img, 840, 24)

        # combo bar
        if game.chain >= 2:
            col = combo_color(game.chain)
            txt = self.fonts.text(f"×{game.chain} 連擊", 14, col, bold=True)
            surface.blit(txt, (60, 68))
            frac = clamp01(game.combo_timer / COMBO_WINDOW)
            pygame.draw.rect(surface, (20, 32, 50), (60, 92, 170, 5),
                             border_radius=3)
            pygame.draw.rect(surface, col, (60, 92, int(170 * frac), 5),
                             border_radius=3)

        # mode pill
        mode = game.cfg
        col = MODE_COLORS.get(game.mode, C_GREEN)
        label = self.fonts.text(f"{mode.name}  {mode.en}", 11, col)
        pw, ph = label.get_width() + 28, 24
        pill = pygame.Rect(WIN_W // 2 - pw // 2, 10, pw, ph)
        self._panel(surface, pill, fill=(12, 22, 38),
                    border=shade(col, 0.40), radius=12, alpha=235)
        self._blit_center(surface, label, pill.center)

        # timer / speed
        if mode.duration is not None:
            left = game.time_left or 0.0
            tcol = C_RED if left <= 15 else C_TEXT
            alpha = 255
            if left <= 10:
                alpha = int(120 + 120 * (0.5 + 0.5 * math.sin(app.anim_t * 8)))
            img = self.fonts.text(fmt_clock(left), 30, tcol, bold=True,
                                  display=True, alpha=alpha)
            self._blit_center(surface, img, (WIN_W // 2, 56))
        else:
            img = self.fonts.text(f"速度 LV {game.level}", 11, C_DIM)
            self._blit_center(surface, img, (WIN_W // 2, 52))

        # footer: stats (left)
        stats = self.fonts.text(
            f"長度 {len(game.body)}　·　速度 LV {game.level}　·　已吃 {game.eaten}",
            12, C_DIM)
        surface.blit(stats, (60, 654))

        # footer: power-up chips (right)
        chips = []
        if game.slow_timer > 0:
            chips.append(("減速", game.slow_timer, PICKUP_COLORS["slow"]))
        if game.double_timer > 0:
            chips.append(("雙倍", game.double_timer, PICKUP_COLORS["double"]))
        x = 840
        for name, left, col in chips:
            img = self.fonts.text(f"{name} {left:.1f}s", 11, col, bold=True)
            w = img.get_width() + 20
            rect = pygame.Rect(x - w, 648, w, 22)
            self._panel(surface, rect, fill=(14, 24, 40),
                        border=shade(col, 0.45), radius=10, alpha=230)
            self._blit_center(surface, img, rect.center)
            x -= w + 8

        # controls hint (center bottom)
        hint = self.fonts.text(
            "↑↓←→ 移動　·　Space 暫停　·　R 重來　·　M 音效", 11, C_DIM)
        self._blit_center(surface, hint, (WIN_W // 2, 684))

    # -- menu ----------------------------------------------------------
    def draw_menu(self, app, surface) -> None:
        t = app.anim_t
        surface.blit(self._glow(250, (0, 60, 44), 0.5),
                     (WIN_W // 2 - 250, 104 - 250),
                     special_flags=pygame.BLEND_ADD)
        self.draw_glow_text(surface, "霓虹貪食蛇", 56, (232, 255, 246),
                            (0, 120, 80), (WIN_W // 2, 104), bold=True,
                            passes=2)
        sub = self.fonts.text("N E O N   S E R P E N T", 13, C_CYAN,
                              display=True)
        self._blit_center(surface, sub, (WIN_W // 2, 156))
        tag = self.fonts.text("吃蘋果 · 累積連擊 · 收集特殊果實", 13, C_DIM)
        self._blit_center(surface, tag, (WIN_W // 2, 184))

        # decorative rules beside the title
        pygame.draw.line(surface, (24, 74, 60), (110, 104), (300, 104), 1)
        pygame.draw.line(surface, (24, 74, 60), (600, 104), (790, 104), 1)
        pygame.draw.circle(surface, (60, 200, 140), (306, 104), 2)
        pygame.draw.circle(surface, (60, 200, 140), (594, 104), 2)

        # mode cards
        for i, key in enumerate(MODE_KEYS):
            self._draw_card(app, surface, i, key, t)
        sel = MODE_KEYS[app.menu_idx]
        caret_y = CARD_Y - 16 + int(math.sin(t * 4) * 2)
        cx = self.card_rect(app.menu_idx).centerx
        pygame.draw.polygon(surface, MODE_COLORS[sel],
                            [(cx - 6, caret_y - 7), (cx + 6, caret_y - 7),
                             (cx, caret_y + 1)])

        # hints
        hint1 = self.fonts.text(
            "↑ ↓ 選擇模式　·　Enter 開始　·　1 / 2 / 3 快速開始　·　M 音效　·　Esc 離開",
            13, C_TEXT)
        self._blit_center(surface, hint1, (WIN_W // 2, 442))
        hint2 = self.fonts.text(
            "移動 ← ↑ ↓ → / WASD　·　暫停 Space　·　重來 R", 12, C_DIM)
        self._blit_center(surface, hint2, (WIN_W // 2, 472))
        hint3 = self.fonts.text(
            "蘋果 +10　·　金蘋果 +50　·　毒果 −30　·　道具：減速 / 雙倍 / 縮短",
            12, C_DIM)
        self._blit_center(surface, hint3, (WIN_W // 2, 500))

        # rules
        rules = [
            "吃到蘋果會變長；撞牆或咬到自己就結束。",
            f"在 {COMBO_WINDOW:g} 秒內連續吃蘋果會累積連擊，最高 ×{COMBO_MAX}。",
            f"每吃 {SPECIAL_EVERY} 顆蘋果出現一顆特殊果實，場上最多 {MAX_SPECIALS} 顆。",
        ]
        for k, line in enumerate(rules):
            img = self.fonts.text(line, 12, (96, 118, 146))
            self._blit_center(surface, img, (WIN_W // 2, 552 + k * 26))

        # footer
        foot = self.fonts.text("最高分會自動存在 highscore.json", 10, (74, 94, 120))
        surface.blit(foot, (60, 672))
        sound_off = (not app.sounds.enabled) or app.sounds.muted
        right = self.fonts.text(
            f"v{VERSION}　·　音效{'關' if sound_off else '開'}", 10,
            C_RED if sound_off else (82, 102, 128))
        self._blit_right(surface, right, 840, 672)

    def _draw_card(self, app, surface, i: int, key: str, t: float) -> None:
        mode = MODES[key]
        col = MODE_COLORS[key]
        rect = self.card_rect(i)
        selected = (i == app.menu_idx)
        hover = app.mouse is not None and rect.collidepoint(app.mouse)
        draw = rect.move(0, -5 if selected else (-2 if hover else 0))

        if selected or hover:
            g = self._glow(130, shade(col, 0.45), 0.7)
            surface.blit(g, (draw.centerx - 130, draw.centery - 130),
                         special_flags=pygame.BLEND_ADD)
        fill = C_PANEL_HI if selected else C_PANEL
        border = col if selected else (shade(col, 0.62) if hover else (34, 52, 78))
        self._panel(surface, draw, fill=fill, border=border, radius=16,
                    width=2 if selected else 1, alpha=255)
        # top accent
        pygame.draw.line(surface, col,
                         (draw.left + 18, draw.top + 1),
                         (draw.right - 18, draw.top + 1), 3)

        name = self.fonts.text(mode.name, 26, C_TEXT if selected else (190, 205, 225),
                               bold=True)
        self._blit_center(surface, name, (draw.centerx, draw.top + 36))
        en = self.fonts.text(mode.en, 10, col if selected else (96, 116, 142),
                             display=True)
        self._blit_center(surface, en, (draw.centerx, draw.top + 66))

        font = self.fonts.font(12, display=False)
        lines = wrap_text(font, mode.desc, CARD_W - 34)
        for k, line in enumerate(lines[:2]):
            img = self.fonts.text(line, 12, (128, 148, 176))
            self._blit_center(surface, img, (draw.centerx, draw.top + 94 + k * 20))

        best = app.best_for(key)
        if best > 0:
            bimg = self.fonts.text(f"最佳 {fmt_int(best)}", 12, C_GOLD)
        else:
            bimg = self.fonts.text("尚未挑戰", 12, (86, 104, 130))
        self._blit_center(surface, bimg, (draw.centerx, draw.top + 140))

    # -- pause / game over ---------------------------------------------
    def draw_pause(self, app, surface) -> None:
        self._dim(surface, 132)
        rect = pygame.Rect(0, 0, 440, 210)
        rect.center = (WIN_W // 2, 360)
        self._panel(surface, rect, fill=(9, 15, 27), border=(44, 82, 114),
                    radius=18, width=2, alpha=252)
        self._blit_center(surface, self.fonts.text("暫停", 36, C_TEXT, bold=True),
                          (WIN_W // 2, rect.top + 52))
        self._blit_center(surface,
                          self.fonts.text("P A U S E D", 10, C_CYAN, display=True),
                          (WIN_W // 2, rect.top + 84))
        if app.game is not None:
            self._blit_center(
                surface,
                self.fonts.text(f"目前分數 {fmt_int(app.game.score)}", 14, C_DIM),
                (WIN_W // 2, rect.top + 120))
        self._blit_center(
            surface,
            self.fonts.text("Esc / Space 繼續　·　R 重新開始　·　Q 回選單",
                            12, C_DIM),
            (WIN_W // 2, rect.top + 164))

    def draw_dead(self, app, surface) -> None:
        a = clamp01(app.result_t / 0.45)
        self._dim(surface, int(150 * a))
        stats = app.last_stats or {}
        reason = stats.get("reason") or "end"
        reason_text = {"wall": "撞牆了", "self": "咬到自己",
                       "time": "時間到"}.get(reason, "結束")
        rcol = C_GOLD if reason == "time" else (255, 120, 120)
        rect = pygame.Rect(0, 0, 500, 340)
        rect.center = (WIN_W // 2, 360 + int((1 - a) * 22))
        border = C_GOLD if app.new_best else (44, 82, 114)
        if app.new_best:
            g = self._glow(220, (80, 60, 10), 0.9)
            surface.blit(g, (rect.centerx - 220, rect.centery - 220),
                         special_flags=pygame.BLEND_ADD)
        self._panel(surface, rect, fill=C_PANEL, border=border, radius=20,
                    width=2, alpha=int(248 * a))

        self._blit_center(surface,
                          self.fonts.text("R U N   O V E R", 9, C_DIM, display=True),
                          (rect.centerx, rect.top + 30))
        self._blit_center(surface,
                          self.fonts.text(reason_text, 30, rcol, bold=True),
                          (rect.centerx, rect.top + 64))
        self._blit_center(surface,
                          self.fonts.text("分數", 11, C_DIM),
                          (rect.centerx, rect.top + 104))
        self._blit_center(
            surface,
            self.fonts.text(fmt_int(stats.get("score", 0)), 46, C_TEXT,
                            bold=True, display=True),
            (rect.centerx, rect.top + 136))
        if app.new_best:
            pulse = int(160 + 95 * (0.5 + 0.5 * math.sin(app.anim_t * 5)))
            img = self.fonts.text("★ 新紀錄 ★", 13, C_GOLD, bold=True,
                                  alpha=pulse)
            self._blit_center(surface, img, (rect.centerx, rect.top + 174))

        cols = [
            ("長度", str(stats.get("length", 0))),
            ("最高連擊", f"×{stats.get('max_chain', 0)}"),
            ("存活時間", f"{int(stats.get('elapsed', 0))}s"),
        ]
        for k, (label, value) in enumerate(cols):
            cx = rect.centerx + (k - 1) * 150
            self._blit_center(surface, self.fonts.text(label, 10, C_DIM),
                              (cx, rect.top + 214))
            self._blit_center(surface,
                              self.fonts.text(value, 20, C_TEXT, bold=True,
                                              display=True),
                              (cx, rect.top + 236))

        pygame.draw.line(surface, (36, 56, 84),
                         (rect.left + 40, rect.top + 268),
                         (rect.right - 40, rect.top + 268), 1)
        self._blit_center(
            surface,
            self.fonts.text("Enter 再來一次　·　Esc 回選單", 12, C_DIM),
            (rect.centerx, rect.top + 292))

    # -- frame ---------------------------------------------------------
    def draw(self, surface, app) -> None:
        self.draw_background(surface, app.anim_t)
        if app.state == "menu" or app.game is None:
            self.draw_menu(app, surface)
        else:
            self.draw_game(app, surface)
            if app.state == "pause":
                self.draw_pause(app, surface)
            elif app.state == "dead":
                self.draw_dead(app, surface)
        self.draw_flash(surface, app.fx)

    def draw_game(self, app, surface) -> None:
        game = app.game
        ox, oy = app.fx.shake_offset()
        panel = pygame.Rect(BOARD_X + ox - 3, BOARD_Y + oy - 3,
                            BOARD_W + 6, BOARD_H + 6)
        pygame.draw.rect(surface, (9, 36, 48), panel.inflate(10, 10), 3,
                         border_radius=20)
        surface.blit(self.board_bg, (BOARD_X + ox, BOARD_Y + oy))
        pygame.draw.rect(surface, (26, 62, 88), panel, 2, border_radius=15)
        self._corner_brackets(surface, panel, (44, 120, 150), 22)
        self._draw_food(surface, game, app.anim_t, ox, oy)
        self._draw_snake(surface, game, app.anim_t, ox, oy)
        self._draw_particles(surface, app.fx, ox, oy)
        self._draw_floaters(surface, app.fx)
        self.draw_hud(app, surface)


# --------------------------------------------------------------------------
# Application: window, input, state machine, game feel
# --------------------------------------------------------------------------

class GameApp:
    """Owns the pygame loop and wires the logic layer to the presentation layer."""

    def __init__(self, seed: int | None = None, no_highscore: bool = False,
                 mute: bool = False, start_mode: str | None = None,
                 play: bool = False) -> None:
        if pygame is None:
            raise RuntimeError("pygame is required for the GUI")
        pygame.init()
        try:
            pygame.mixer.init(frequency=SoundBank.RATE, size=-16,
                              channels=2, buffer=512)
        except pygame.error:
            pass
        self.screen = pygame.display.set_mode((WIN_W, WIN_H))
        # Draw into an opaque 24-bit buffer, then present it.  On macOS the
        # SDL display surface carries an alpha channel; drawing straight onto
        # it lets transparent source pixels leak their RGB (glyph boxes turn
        # into solid bars), so the window buffer is kept alpha-free.
        self.frame = pygame.Surface((WIN_W, WIN_H), 0, 24)
        self.frame.fill(C_BG_TOP)
        pygame.display.set_caption(APP_NAME)
        self.clock = pygame.time.Clock()
        self.fonts = FontBook()
        self.sounds = SoundBank(enabled=not mute)
        self.renderer = Renderer(self.screen, self.fonts)
        self.fx = FX()

        self.anim_t = 0.0
        self.state = "menu"              # menu | play | pause | dead
        self.game: SnakeGame | None = None
        self.menu_idx = MODE_KEYS.index(start_mode) if start_mode in MODES else 0
        self.punch = 0.0
        self.acc = 0.0
        self.death_t = 0.0
        self.result_t = 0.0
        self.new_best = False
        self.last_stats: dict | None = None
        self.mouse: tuple[int, int] | None = None
        self.running = True

        self.seed = seed
        self.no_highscore = no_highscore
        self.highscores = {} if no_highscore else load_highscores()

        if play:
            self.start_game()

    # -- helpers -------------------------------------------------------
    def best_for(self, mode: str) -> int:
        entry = self.highscores.get(mode) or {}
        best = int(entry.get("score", 0) or 0) if isinstance(entry, dict) else 0
        if self.game is not None and self.game.mode == mode:
            best = max(best, int(self.game.score))
        return best

    @staticmethod
    def _cell_px(cx: float, cy: float) -> tuple[float, float]:
        return (BOARD_X + (cx + 0.5) * CELL, BOARD_Y + (cy + 0.5) * CELL)

    # -- state transitions ---------------------------------------------
    def start_game(self, mode: str | None = None) -> None:
        mode = mode or MODE_KEYS[self.menu_idx]
        self.menu_idx = MODE_KEYS.index(mode)
        self.game = SnakeGame(mode=mode, seed=self.seed)
        self.state = "play"
        self.acc = 0.0
        self.death_t = 0.0
        self.result_t = 0.0
        self.new_best = False
        self.last_stats = None
        self.punch = 0.0
        self.fx.clear()
        self.sounds.play("start")
        self.fx.floater("GO!", WIN_W // 2, BOARD_Y + BOARD_H // 2 - 24,
                        (150, 255, 180), 40, life=0.85, vy=-26)
        self.fx.flash((18, 90, 60), 45, 0.3)

    def quit_to_menu(self) -> None:
        self.state = "menu"
        self.game = None
        self.fx.clear()
        self.last_stats = None
        self.new_best = False
        self.sounds.play("click")

    def toggle_mute(self) -> None:
        self.sounds.toggle()

    # -- input ---------------------------------------------------------
    def _events(self) -> None:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_m:
                    self.toggle_mute()
                    continue
                self._key(event.key)
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                self._click(event.pos)

    def _key(self, key: int) -> None:
        if self.state == "menu":
            self._key_menu(key)
        elif self.state == "play":
            self._key_play(key)
        elif self.state == "pause":
            self._key_pause(key)
        elif self.state == "dead":
            self._key_dead(key)

    def _key_menu(self, key: int) -> None:
        if key in (pygame.K_UP, pygame.K_w, pygame.K_LEFT, pygame.K_a):
            self.menu_idx = (self.menu_idx - 1) % len(MODE_KEYS)
            self.sounds.play("click")
        elif key in (pygame.K_DOWN, pygame.K_s, pygame.K_RIGHT, pygame.K_d):
            self.menu_idx = (self.menu_idx + 1) % len(MODE_KEYS)
            self.sounds.play("click")
        elif key in (pygame.K_1, pygame.K_2, pygame.K_3):
            self.menu_idx = key - pygame.K_1
            self.start_game()
        elif key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE):
            self.start_game()
        elif key == pygame.K_ESCAPE:
            self.running = False

    def _key_play(self, key: int) -> None:
        turns = {
            pygame.K_LEFT: (-1, 0), pygame.K_a: (-1, 0),
            pygame.K_RIGHT: (1, 0), pygame.K_d: (1, 0),
            pygame.K_UP: (0, -1), pygame.K_w: (0, -1),
            pygame.K_DOWN: (0, 1), pygame.K_s: (0, 1),
        }
        if key in turns:
            if self.game is not None:
                self.game.queue_dir(turns[key])
        elif key in (pygame.K_SPACE, pygame.K_p, pygame.K_ESCAPE):
            self.state = "pause"
            self.sounds.play("click")
        elif key == pygame.K_r:
            self.start_game()
        elif key == pygame.K_q:
            self.quit_to_menu()

    def _key_pause(self, key: int) -> None:
        if key in (pygame.K_ESCAPE, pygame.K_SPACE, pygame.K_p):
            self.state = "play"
            self.sounds.play("click")
        elif key == pygame.K_r:
            self.start_game()
        elif key == pygame.K_q:
            self.quit_to_menu()

    def _key_dead(self, key: int) -> None:
        if self.result_t < 0.28:
            return
        if key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE,
                   pygame.K_r):
            self.start_game()
        elif key in (pygame.K_ESCAPE, pygame.K_q):
            self.quit_to_menu()

    def _click(self, pos) -> None:
        if self.state != "menu":
            return
        for i in range(len(MODE_KEYS)):
            if self.renderer.card_rect(i).collidepoint(pos):
                self.menu_idx = i
                self.start_game()
                return

    # -- simulation + game feel ---------------------------------------
    def _update(self, dt: float) -> None:
        self.fx.update(dt)
        self.punch = max(0.0, self.punch - dt * 2.0)

        if self.state == "play" and self.game is not None:
            self.acc = min(self.acc + dt, 0.25)
            guard = 0
            while (self.state == "play" and self.game is not None
                   and self.acc >= FIXED_DT and guard < 12):
                events = self.game.step()
                self._process(events)
                self.acc -= FIXED_DT
                guard += 1
        elif self.state == "dead":
            self.death_t += dt
            self.result_t += dt

    def _process(self, events: list[dict]) -> None:
        for ev in events:
            kind = ev["type"]
            if kind == "eat":
                pos = ev["pos"]
                k = ev["kind"]
                x, y = self._cell_px(*pos)
                col = PICKUP_COLORS[k]
                chain = int(ev.get("chain", 1) or 1)
                n = 16 if k == "golden" else 9
                self.fx.burst(x, y, col, n=n, speed=195.0, size=3.6, life=0.62)
                self.fx.ring(x, y, col, n=10, speed=210.0)
                self.fx.floater(f"+{int(ev['points'])}", x, y - 18,
                                mix_color(col, (255, 255, 255), 0.3), 20)
                if chain >= 2:
                    self.fx.floater(f"×{chain}", x, y - 44,
                                    combo_color(chain), 13, life=0.7, vy=-36)
                self.fx.shake_add(2.0 + min(4.0, chain * 0.45))
                self.punch = min(1.0, self.punch + 0.55)
                self.sounds.play("golden" if k == "golden"
                                 else f"eat{min(COMBO_MAX, chain)}")
                if k == "golden":
                    self.fx.flash((120, 96, 30), 36, 0.22)
            elif kind == "power":
                pos = ev["pos"]
                k = ev["kind"]
                x, y = self._cell_px(*pos)
                col = PICKUP_COLORS[k]
                self.fx.burst(x, y, col, n=12, speed=180.0, size=3.4, life=0.55)
                self.fx.ring(x, y, col, n=8, speed=170.0)
                self.fx.floater(f"{PICKUPS[k].name} +{int(ev['points'])}",
                                x, y - 18, col, 18)
                self.fx.shake_add(1.8)
                self.sounds.play("power")
                self.fx.flash(shade(col, 0.55), 30, 0.18)
            elif kind == "poison":
                pos = ev["pos"]
                x, y = self._cell_px(*pos)
                col = PICKUP_COLORS["poison"]
                self.fx.burst(x, y, col, n=16, speed=225.0, size=4.0, life=0.7)
                self.fx.ring(x, y, (255, 110, 170), n=10, speed=250.0)
                self.fx.floater(f"{int(ev['points'])}", x, y - 18,
                                (255, 130, 150), 20)
                self.fx.shake_add(9.0)
                self.fx.flash((120, 16, 60), 55, 0.3)
                self.sounds.play("poison")
            elif kind == "die":
                self._on_death(ev.get("stats") or {}, ev.get("reason"))

    def _on_death(self, stats: dict, reason: str | None) -> None:
        if self.state != "play" or self.game is None:
            return
        reason = reason or stats.get("reason")
        self.state = "dead"
        self.death_t = 0.0
        self.result_t = 0.0
        self.last_stats = dict(stats) if stats else self.game.stats()
        self.last_stats["reason"] = reason
        self.sounds.play("timeup" if reason == "time" else "die")
        self.fx.shake_add(6.0 if reason == "time" else 14.0)

        if reason == "time":
            self.fx.flash((255, 200, 90), 80, 0.5)
            self.fx.burst(WIN_W // 2, BOARD_Y + BOARD_H // 2, C_GOLD,
                          n=26, speed=260.0, size=3.6, life=0.9)
        else:
            self.fx.flash((255, 255, 255), 95, 0.3)
            body = self.game.render_body(1.0)
            total = max(1, len(body) - 1)
            for i, (cx, cy) in enumerate(body):
                if i % 2 and i > 2:
                    continue
                x, y = self._cell_px(cx, cy)
                col = snake_color(min(1.0, i / total))
                self.fx.burst(x, y, col, n=3, speed=205.0, size=3.2, life=0.7)
            hx, hy = self._cell_px(*self.game.body[0])
            self.fx.burst(hx, hy, (255, 255, 255), n=12, speed=240.0,
                          size=3.4, life=0.6)

        self.new_best = False
        if not self.no_highscore and self.last_stats.get("score", 0) > 0:
            try:
                self.new_best = save_highscore(default_highscore_path(),
                                               self.game.mode, self.last_stats)
            except Exception:
                self.new_best = False
            if self.new_best:
                self.highscores = load_highscores()
                self.fx.floater("★ 新紀錄 ★", WIN_W // 2,
                                BOARD_Y + BOARD_H // 2, C_GOLD, 30, life=1.8,
                                vy=-18)
                self.fx.burst(WIN_W // 2, BOARD_Y + BOARD_H // 2 - 40,
                              C_GOLD, n=18, speed=190.0, size=3.0, life=1.0)

    # -- frame ---------------------------------------------------------
    def draw_frame(self, surface=None) -> None:
        target = surface or self.frame
        self.renderer.draw(target, self)
        if surface is None:
            self.screen.fill((0, 0, 0, 255))
            self.screen.blit(self.frame, (0, 0))

    def run(self) -> None:
        try:
            while self.running:
                dt = self.clock.tick(FPS) / 1000.0
                dt = min(dt, 0.05)
                self.anim_t += dt
                self.mouse = pygame.mouse.get_pos()
                self._events()
                self._update(dt)
                self.draw_frame()
                pygame.display.flip()
        finally:
            pygame.quit()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--selftest", action="store_true", help="run the logic test-suite")
    ap.add_argument("--text", type=float, metavar="SECONDS",
                    help="headless: simulate, then print the board as ASCII")
    ap.add_argument("--mode", choices=tuple(MODES), default="classic",
                    help="preselected mode")
    ap.add_argument("--play", action="store_true",
                    help="skip the menu and start --mode immediately")
    ap.add_argument("--seed", type=int, help="deterministic food sequence")
    ap.add_argument("--mute", action="store_true", help="start with sound off")
    ap.add_argument("--no-highscore", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    if args.text is not None:
        return run_text(args.mode, args.seed, args.text)

    if pygame is None:
        print("需要 pygame：python3 -m pip install pygame", file=sys.stderr)
        return 1
    try:
        app = GameApp(seed=args.seed, no_highscore=args.no_highscore,
                      mute=args.mute, start_mode=args.mode, play=args.play)
    except pygame.error as exc:
        print(f"無法開啟視窗：{exc}", file=sys.stderr)
        return 1
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
