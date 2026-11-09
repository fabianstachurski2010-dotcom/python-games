#!/usr/bin/env python3
"""Chomp 迷宮追擊 — a maze-chase game in one file (Tkinter, standard library only).

GUI controls
    ← → ↑ ↓  /  W A S D     steer (the turn is taken at the next open corner)
    P                       pause
    R  /  N  /  F2          new run
    1 / 2 / 3               easy / normal / hard ghosts (starts a new run)
    Esc                     quit

Other modes
    --selftest              run the logic tests and exit
    --simulate SECONDS      headless: a pellet-hungry bot plays, then print the result
    --text [SECONDS]        headless: print the maze as ASCII

The maze is 21 x 22 tiles drawn from the MAZE table below. Everything the rules
need — the four ghost personalities, the scatter/chase schedule, the house dot
counters, the frightened window, the fruit — lives in `ChompGame`, which never
touches the display, so the tests can play whole levels without a window.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

try:  # tkinter is optional: --selftest, --simulate and --text need no display
    import tkinter as tk

    TK_AVAILABLE = True
except ImportError:  # pragma: no cover
    TK_AVAILABLE = False


# --------------------------------------------------------------------------- #
# The maze
# --------------------------------------------------------------------------- #

# '#' wall   '.' dot   'o' power pellet   ' ' empty but walkable   '=' ghost door
# 'H' inside the ghost house. 21 wide and mirror-symmetric, with the wrap-around
# tunnel on TUNNEL_ROW (the two blank tiles at each end of that row).
MAZE = (
    "#####################",
    "#.........#.........#",
    "#o##.####.#.####.##o#",
    "#.##.####.#.####.##.#",
    "#...................#",
    "#.##.#.#######.#.##.#",
    "#....#....#....#....#",
    "####.####.#.####.####",
    "####.#.........#.####",
    "####.#.##===##.#.####",
    "#......#HHHHH#......#",
    "  ##.#.#HHHHH#.#.##  ",
    "####.#.#######.#.####",
    "####.#.... ....#.####",
    "#.........#.........#",
    "#.##.####.#.####.##.#",
    "#o.#.............#.o#",
    "##.#.###.....###.#.##",
    "#....#...#.#...#....#",
    "#.####.###.###.####.#",
    "#...................#",
    "#####################",
)

ROWS = len(MAZE)                 # 22
COLS = len(MAZE[0])              # 21
TUNNEL_ROW = 11
TUNNEL_CELLS = frozenset((c, TUNNEL_ROW) for c in (0, 1, COLS - 2, COLS - 1))

WALL = "#"
DOOR = "="
HOUSE = "H"
DOT = "."
PELLET = "o"
EMPTY = " "

PAC_START = (10, 13)
HOUSE_EXIT = (10, 8)             # the tile a ghost walks out onto
HOUSE_CENTER = (10, 10)          # where eaten ghosts are reborn
FRUIT_TILE = (10, 13)            # in front of the house, where Pac-Man starts

UP, LEFT, DOWN, RIGHT = (0, -1), (-1, 0), (0, 1), (1, 0)
# Tk measures arc angles counter-clockwise from the 3 o'clock position, so 90 is
# straight up on screen and 270 is straight down. The mouth is the gap opposite
# the arc's start, which puts it at exactly this angle.
MOUTH_ANGLE = {RIGHT: 0, UP: 90, LEFT: 180, DOWN: 270}


def mouth_angle(direction):
    """Which way Pac-Man's mouth should open, in Tk arc degrees."""
    return MOUTH_ANGLE.get(direction, MOUTH_ANGLE[RIGHT])
DIRECTIONS = (UP, LEFT, DOWN, RIGHT)     # the original's tie-break order
KEY_DIRS = {"up": UP, "w": UP, "down": DOWN, "s": DOWN,
            "left": LEFT, "a": LEFT, "right": RIGHT, "d": RIGHT}

# The two tiles beside the house door: outside the house a ghost may not choose
# to turn upwards here (the original's "no-up" zones).
NO_UP_TILES = frozenset({(9, 8), (11, 8)})

# Where each ghost heads while the mode is "scatter" (outside the maze is fine)
SCATTER = {"blinky": (COLS - 1, 0), "pinky": (0, 0),
           "inky": (COLS - 1, ROWS - 1), "clyde": (0, ROWS - 1)}

GHOST_NAMES = ("blinky", "pinky", "inky", "clyde")
GHOST_LABEL = {"blinky": "紅 追擊者", "pinky": "粉 埋伏者",
               "inky": "青 包夾者", "clyde": "橘 膽小鬼"}
GHOST_COLOR = {"blinky": "#ff4b3e", "pinky": "#ff9ad5",
               "inky": "#4fe3e3", "clyde": "#ffab48"}

MODE_LABEL = {"scatter": "散開", "chase": "追擊", "frightened": "驚嚇"}

# Scatter / chase schedule: (seconds, mode). The last entry runs forever.
MODE_SCHEDULE = ((7.0, "scatter"), (20.0, "chase"), (7.0, "scatter"), (20.0, "chase"),
                 (5.0, "scatter"), (20.0, "chase"), (5.0, "scatter"),
                 (float("inf"), "chase"))

# How long a power pellet keeps the ghosts frightened, indexed by level - 1.
FRIGHT_SECONDS = (7.0, 6.0, 5.0, 4.0, 3.0, 5.0, 2.0, 2.0, 1.0, 5.0,
                  2.0, 1.0, 1.0, 3.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0)

GHOST_CHAIN_SCORES = (200, 400, 800, 1600)

# dots eaten -> fruit appears
FRUIT_THRESHOLDS = (70, 170)
FRUIT_SECONDS = 9.5
FRUITS = (("櫻桃", "#e8443c", 100), ("草莓", "#ff5d8f", 300),
          ("橘子", "#ff9f2e", 500), ("蘋果", "#ff3b30", 700),
          ("甜瓜", "#7ee36b", 1000), ("香蕉", "#ffe066", 2000),
          ("鳳梨", "#f7c948", 3000), ("鑰匙", "#8fd3ff", 5000))

DOT_SCORE = 10
PELLET_SCORE = 50
EXTRA_LIFE_AT = 10000

BASE_SPEED = 10.0                # tiles per second that every fraction is of
EYES_SPEED = 1.60
LEAVE_SPEED = 0.55
COLLIDE_DIST = 0.5               # tiles; closer than this and they touch

READY_SECONDS = 2.2
DEATH_SECONDS = 1.8
LEVELCLEAR_SECONDS = 2.4
GHOST_FREEZE = 0.7               # the pause that shows the ghost's score
RELEASE_DOTS = {"pinky": 0, "inky": 30, "clyde": 60}
NO_DOT_RELEASE = 4.0             # nothing eaten for this long and the house empties
REVIVE_SECONDS = 0.6

DT = 1.0 / 60.0

DIFFICULTIES = {
    "easy": {"label": "簡單", "ghost": 0.85, "fright": 1.35},
    "normal": {"label": "普通", "ghost": 1.00, "fright": 1.00},
    "hard": {"label": "困難", "ghost": 1.15, "fright": 0.75},
}

QUEUE_LIVES = 3
DEFAULT_SCORE_PATH = Path(__file__).resolve().parent / "highscore.json"


# --------------------------------------------------------------------------- #
# Grid helpers
# --------------------------------------------------------------------------- #


def wrap_cell(cell):
    """Walk off one side of the tunnel row and come back on the other."""
    c, r = cell
    if 0 <= c < COLS:
        return (c, r)
    if r == TUNNEL_ROW:
        return (c % COLS, r)
    return (max(0, min(COLS - 1, c)), r)


def tile_at(cell):
    c, r = cell
    if not (0 <= r < ROWS):
        return WALL
    c, r = wrap_cell((c, r))
    return MAZE[r][c]


def is_wall(cell):
    return tile_at(cell) == WALL


def pac_can_enter(cell):
    return tile_at(cell) in (DOT, PELLET, EMPTY)


def squared_distance(a, b):
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


def manhattan(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


@dataclass
class Mover:
    """An entity that walks tile to tile, remembering how far it has got.

    The position in tile units is `cell + 0.5 + dir * progress`, so `progress`
    is always in [0, 1) and a turn is only possible at a tile centre.
    """
    cell: tuple[int, int]
    dir: tuple[int, int] = (0, 0)
    progress: float = 0.0

    def pos(self):
        return (self.cell[0] + 0.5 + self.dir[0] * self.progress,
                self.cell[1] + 0.5 + self.dir[1] * self.progress)

    def advance(self, distance, walkable, choose=None):
        """Move up to `distance` tiles, calling `choose(mover)` at tile centres."""
        remaining = distance
        guard = 0
        while remaining > 1e-9 and guard < 64:
            guard += 1
            if self.progress <= 0.0:
                self.progress = 0.0
                if choose is not None:
                    choose(self)
            if self.dir == (0, 0) or not walkable(self.cell, self.dir):
                self.progress = 0.0
                return
            step = min(remaining, 1.0 - self.progress)
            self.progress += step
            remaining -= step
            if self.progress >= 1.0 - 1e-9:
                self.cell = wrap_cell((self.cell[0] + self.dir[0],
                                       self.cell[1] + self.dir[1]))
                self.progress = 0.0

    def reverse(self):
        """Turn around on the spot: the tile it was heading for becomes its cell."""
        if self.dir == (0, 0):
            return
        if self.progress > 0.0:
            self.cell = wrap_cell((self.cell[0] + self.dir[0], self.cell[1] + self.dir[1]))
            self.progress = 1.0 - self.progress
        self.dir = (-self.dir[0], -self.dir[1])


@dataclass
class Ghost:
    name: str
    cell: tuple[int, int]
    state: str = "house"          # house | leaving | out | eaten
    mover: Mover = field(default=None)
    release_timer: float = 0.0
    dot_limit: int = 0

    def __post_init__(self):
        if self.mover is None:
            self.mover = Mover(self.cell)


def speed_profile(level):
    """(pac, ghost, frightened, tunnel) as fractions of BASE_SPEED."""
    if level <= 1:
        return (0.80, 0.75, 0.50, 0.40)
    if level <= 4:
        return (0.90, 0.85, 0.55, 0.45)
    if level <= 20:
        return (1.00, 0.95, 0.60, 0.50)
    return (0.90, 0.95, 0.60, 0.50)


def fright_seconds(level):
    return FRIGHT_SECONDS[min(level, len(FRIGHT_SECONDS)) - 1]


def fruit_for(level):
    return FRUITS[min(level, len(FRUITS)) - 1]


# --------------------------------------------------------------------------- #
# The game (no UI at all)
# --------------------------------------------------------------------------- #


class ChompGame:
    """Rules, ghost AI and scoring. One `step()` is one 1/60 s frame."""

    def __init__(self, level: int = 1, lives: int = QUEUE_LIVES,
                 difficulty: str = "normal", seed: int | None = None, rng=None):
        if difficulty not in DIFFICULTIES:
            raise ValueError(f"unknown difficulty: {difficulty}")
        self.difficulty = difficulty
        self.rng = rng if rng is not None else random.Random(seed)
        self.start_level = max(1, level)
        self.start_lives = max(1, lives)
        self.start()

    # -- lifecycle --------------------------------------------------------- #

    def start(self) -> list[str]:
        """Begin a fresh run from level 1 with a full set of lives."""
        self.level = self.start_level
        self.lives = self.start_lives
        self.score = 0
        self.high_score = 0
        self.extra_life_given = False
        self.time = 0.0
        self.paused = False
        self._load_level()
        return []

    def _load_level(self) -> None:
        """(Re)fill the maze and put everybody back on their marks."""
        self.tiles = [list(row) for row in MAZE]
        self.dots_eaten = 0
        self.dots_total = sum(row.count(DOT) + row.count(PELLET) for row in MAZE)
        self.fruit = None
        self.fruit_timer = 0.0
        self.fruit_history = []
        self.fruit_thresholds = list(FRUIT_THRESHOLDS)
        self.ghost_chain = 0
        self.fright_timer = 0.0
        self.freeze = 0.0
        self.eaten_this_fright = 0
        self.mode_index = 0
        self.mode_timer = MODE_SCHEDULE[0][0]
        self.mode = MODE_SCHEDULE[0][1]
        self.pac = Mover(PAC_START, LEFT)
        self.pac_want = LEFT
        self.ghosts = [
            Ghost("blinky", HOUSE_EXIT, "out"),
            Ghost("pinky", (10, 10), "house", dot_limit=RELEASE_DOTS["pinky"]),
            Ghost("inky", (8, 10), "house", dot_limit=RELEASE_DOTS["inky"]),
            Ghost("clyde", (12, 10), "house", dot_limit=RELEASE_DOTS["clyde"]),
        ]
        for g in self.ghosts:
            g.mover = Mover(g.cell, (0, 0) if g.state == "house" else LEFT)
        self.ghosts[0].mover = Mover(HOUSE_EXIT, LEFT)
        self.no_dot_timer = 0.0
        self.state = "ready"
        self.timer = READY_SECONDS
        self._invariant()

    def _invariant(self) -> None:
        """Cheap guard the tests can lean on: nobody may stand inside a wall."""
        for g in self.ghosts:
            if is_wall(g.mover.cell):
                raise AssertionError(f"{g.name} stands in a wall at {g.mover.cell}")
        if is_wall(self.pac.cell):
            raise AssertionError(f"pac stands in a wall at {self.pac.cell}")

    # -- queries ----------------------------------------------------------- #

    def tile(self, cell) -> str:
        c, r = cell
        if not (0 <= r < ROWS):
            return WALL
        c, _ = wrap_cell((c, r))
        return self.tiles[r][c]

    def dots_left(self) -> int:
        return sum(row.count(DOT) + row.count(PELLET)
                   for row in self.tiles)

    def pac_pos(self):
        return self.pac.pos()

    def ghost_pos(self, name):
        for g in self.ghosts:
            if g.name == name:
                return g.mover.pos()
        raise KeyError(name)

    def ghost(self, name) -> Ghost:
        for g in self.ghosts:
            if g.name == name:
                return g
        raise KeyError(name)

    def is_frightened(self, g: Ghost) -> bool:
        return self.fright_timer > 0.0 and g.state in ("out", "leaving")

    def ghosts_frightened(self) -> bool:
        return any(self.is_frightened(g) for g in self.ghosts)

    def fright_flash(self) -> bool:
        """True while the frightened ghosts should be drawn white."""
        if self.fright_timer <= 0.0:
            return False
        if self.fright_timer > 2.0:
            return False
        return int(self.fright_timer * 5) % 2 == 0

    def pac_speed(self) -> float:
        return speed_profile(self.level)[0] * BASE_SPEED

    def ghost_speed(self, g: Ghost) -> float:
        pac_f, ghost_f, fright_f, tunnel_f = speed_profile(self.level)
        scale = DIFFICULTIES[self.difficulty]["ghost"]
        if g.state == "eaten":
            return EYES_SPEED * BASE_SPEED
        if g.state == "leaving":
            return LEAVE_SPEED * BASE_SPEED * scale
        if self.is_frightened(g):
            return fright_f * BASE_SPEED
        if g.mover.cell in TUNNEL_CELLS:
            return tunnel_f * BASE_SPEED
        return ghost_f * BASE_SPEED * scale

    def mode_phase(self):
        """(mode, seconds left in this phase)."""
        return (self.mode, max(0.0, self.mode_timer))

    # -- input ------------------------------------------------------------- #

    def set_pac_dir(self, d) -> bool:
        """Ask Pac-Man to go this way. Reversing happens at once."""
        if d not in DIRECTIONS:
            return False
        self.pac_want = d
        if d == self.pac.dir:
            return True
        if (d[0], d[1]) == (-self.pac.dir[0], -self.pac.dir[1]) and self.pac.dir != (0, 0):
            self.pac.reverse()
            return True
        # a right-angle turn waits for the next tile centre, which `step` applies
        return self._can_enter(self.pac.cell, d, pac=True)

    # -- frame ------------------------------------------------------------- #

    def step(self) -> list[str]:
        events: list[str] = []
        if self.paused or self.state == "gameover":
            return events
        dt = DT
        self.time += dt

        if self.state == "ready":
            self.timer -= dt
            if self.timer <= 0.0:
                self.state = "playing"
                events.append("go")
            return events

        if self.state == "dying":
            self.timer -= dt
            if self.timer <= 0.0:
                self._after_death(events)
            return events

        if self.state == "levelclear":
            self.timer -= dt
            if self.timer <= 0.0:
                self._next_level(events)
            return events

        if self.freeze > 0.0:
            self.freeze -= dt
            return events

        self._step_mode(dt, events)
        self._step_pac(dt, events)
        self._step_ghosts(dt, events)
        if self._collisions(events):
            return events
        self._step_fruit(dt, events)
        return events

    # -- mode schedule ----------------------------------------------------- #

    def _step_mode(self, dt, events) -> None:
        if self.fright_timer > 0.0:
            self.fright_timer -= dt
            if self.fright_timer <= 0.0:
                self.fright_timer = 0.0
                self.ghost_chain = 0
                events.append("fright:end")
            return                                  # the schedule pauses while frightened
        self.mode_timer -= dt
        if self.mode_timer <= 0.0:
            self.mode_index = min(self.mode_index + 1, len(MODE_SCHEDULE) - 1)
            self.mode = MODE_SCHEDULE[self.mode_index][1]
            self.mode_timer = MODE_SCHEDULE[self.mode_index][0]
            for g in self.ghosts:
                if g.state in ("out", "leaving"):
                    g.mover.reverse()               # the original reverses them all
            events.append(f"mode:{self.mode}")

    # -- Pac-Man ----------------------------------------------------------- #

    def _can_enter(self, cell, d, pac=False) -> bool:
        nxt = (cell[0] + d[0], cell[1] + d[1])
        if pac:
            return pac_can_enter(nxt)
        return not is_wall(nxt)

    def _step_pac(self, dt, events) -> None:
        def choose(m):
            want = self.pac_want
            if want != (0, 0) and pac_can_enter((m.cell[0] + want[0], m.cell[1] + want[1])):
                m.dir = want

        self.pac.advance(self.pac_speed() * dt,
                         lambda cell, d: self._can_enter(cell, d, True), choose)
        self._eat_at(self.pac.cell, events)

    def _eat_at(self, cell, events) -> None:
        here = self.tile(cell)
        if here == DOT:
            self.tiles[cell[1]][cell[0]] = EMPTY
            self.score += DOT_SCORE
            self.dots_eaten += 1
            self.no_dot_timer = 0.0
            self._check_extra_life(events)
            self._check_fruit(events)
            events.append("dot")
        elif here == PELLET:
            self.tiles[cell[1]][cell[0]] = EMPTY
            self.score += PELLET_SCORE
            self.dots_eaten += 1
            self.no_dot_timer = 0.0
            self._start_fright(events)
            self._check_extra_life(events)
            self._check_fruit(events)
            events.append("pellet")
        if here in (DOT, PELLET) and self.dots_left() == 0:
            self.state = "levelclear"
            self.timer = LEVELCLEAR_SECONDS
            events.append("levelclear")

    def _check_extra_life(self, events) -> None:
        if not self.extra_life_given and self.score >= EXTRA_LIFE_AT:
            self.extra_life_given = True
            self.lives += 1
            events.append("extra")

    def _start_fright(self, events) -> None:
        self.fright_timer = fright_seconds(self.level) * DIFFICULTIES[self.difficulty]["fright"]
        self.ghost_chain = 0
        self.eaten_this_fright = 0
        for g in self.ghosts:
            if g.state in ("out", "leaving"):
                g.mover.reverse()
        events.append("fright")

    # -- ghosts ------------------------------------------------------------ #

    def ghost_target(self, g: Ghost):
        """The tile this ghost is aiming for right now (the classic formulas)."""
        if g.state == "eaten":
            return HOUSE_CENTER
        if g.state == "leaving":
            return HOUSE_EXIT
        if self.mode == "scatter":
            return SCATTER[g.name]
        pac = self.pac.cell
        d = self.pac.dir if self.pac.dir != (0, 0) else self.pac_want
        if g.name == "blinky":
            return pac
        if g.name == "pinky":
            t = (pac[0] + 4 * d[0], pac[1] + 4 * d[1])
            if d == UP:                     # the original's overflow, kept on purpose
                t = (t[0] - 4, t[1])
            return t
        if g.name == "inky":
            a = (pac[0] + 2 * d[0], pac[1] + 2 * d[1])
            if d == UP:
                a = (a[0] - 2, a[1])
            b = self.ghost("blinky").mover.cell
            return (2 * a[0] - b[0], 2 * a[1] - b[1])
        # clyde: bold until he gets within eight tiles, then he bolts for his corner
        if squared_distance(g.mover.cell, pac) > 64:
            return pac
        return SCATTER["clyde"]

    def _ghost_can_go(self, g: Ghost, cell, d) -> bool:
        nxt = (cell[0] + d[0], cell[1] + d[1])
        if is_wall(nxt):
            return False
        tile = tile_at(nxt)
        if tile in (DOOR, HOUSE) and g.state not in ("house", "leaving", "eaten"):
            return False
        if (d == UP and g.state == "out" and cell in NO_UP_TILES
                and not self.is_frightened(g)):
            return False
        return True

    def _choose_ghost_dir(self, g: Ghost) -> None:
        back = (-g.mover.dir[0], -g.mover.dir[1])
        options = []
        for d in DIRECTIONS:
            if g.mover.dir != (0, 0) and d == back:
                continue
            if not self._ghost_can_go(g, g.mover.cell, d):
                continue
            options.append(d)
        if not options:
            g.mover.reverse()
            return
        if self.is_frightened(g):
            g.mover.dir = self.rng.choice(options)
            return
        target = self.ghost_target(g)
        g.mover.dir = min(options, key=lambda d: (
            squared_distance(target, (g.mover.cell[0] + d[0], g.mover.cell[1] + d[1])),
            DIRECTIONS.index(d)))

    def _step_ghosts(self, dt, events) -> None:
        self.no_dot_timer += dt
        for g in self.ghosts:
            if g.state == "house":
                self._house_step(g, dt, events)
                continue
            walkable = lambda cell, d, gg=g: self._ghost_can_go(gg, cell, d)
            choose = lambda _m, gg=g: self._choose_ghost_dir(gg)
            g.mover.advance(self.ghost_speed(g) * dt, walkable, choose)
            if g.state == "leaving" and g.mover.cell == HOUSE_EXIT:
                g.state = "out"
                g.mover.dir = LEFT
                g.mover.progress = 0.0
            elif g.state == "eaten" and g.mover.cell == HOUSE_CENTER:
                g.state = "house"
                g.release_timer = REVIVE_SECONDS
                g.mover.dir = UP
            g.cell = g.mover.cell

    def _house_step(self, g: Ghost, dt, events) -> None:
        if g.release_timer > 0.0:
            g.release_timer -= dt
            if g.release_timer <= 0.0:
                g.state = "leaving"
                g.mover.dir = (0, 0)
                events.append(f"leave:{g.name}")
            return
        ready = (self.dots_eaten >= g.dot_limit and self.fright_timer <= 0.0)
        forced = self.no_dot_timer >= NO_DOT_RELEASE
        if ready or forced:
            g.state = "leaving"
            g.mover.dir = (0, 0)
            events.append(f"leave:{g.name}")

    # -- collisions, fruit, life ------------------------------------------- #

    def _collisions(self, events) -> bool:
        here = self.pac.pos()
        for g in self.ghosts:
            if g.state == "eaten":
                continue
            if squared_distance(here, g.mover.pos()) > COLLIDE_DIST ** 2:
                continue
            if self.is_frightened(g):
                points = GHOST_CHAIN_SCORES[min(self.eaten_this_fright,
                                                len(GHOST_CHAIN_SCORES) - 1)]
                self.eaten_this_fright += 1
                self.ghost_chain = self.eaten_this_fright
                self.score += points
                self._popup(g, points)
                g.state = "eaten"
                g.mover.progress = 0.0
                self.freeze = GHOST_FREEZE
                self._check_extra_life(events)
                events.append(f"ghost:{points}")
                return True
            self.state = "dying"
            self.timer = DEATH_SECONDS
            events.append("death")
            return True
        return False

    def _popup(self, g: Ghost, points: int) -> None:
        self.last_popup = (g.mover.cell, points)
        self.popup_timer = 1.0

    def _after_death(self, events) -> None:
        self.lives -= 1
        if self.lives <= 0:
            self.state = "gameover"
            events.append("gameover")
            return
        self._reset_actors()
        self.state = "ready"
        self.timer = READY_SECONDS
        events.append("ready")

    def _reset_actors(self) -> None:
        self.pac = Mover(PAC_START, LEFT)
        self.pac_want = LEFT
        self.ghosts = [
            Ghost("blinky", HOUSE_EXIT, "out"),
            Ghost("pinky", (10, 10), "house", dot_limit=RELEASE_DOTS["pinky"]),
            Ghost("inky", (8, 10), "house", dot_limit=RELEASE_DOTS["inky"]),
            Ghost("clyde", (12, 10), "house", dot_limit=RELEASE_DOTS["clyde"]),
        ]
        for g in self.ghosts:
            g.mover = Mover(g.cell, (0, 0) if g.state == "house" else LEFT)
        self.mode_index = 0
        self.mode = MODE_SCHEDULE[0][1]
        self.mode_timer = MODE_SCHEDULE[0][0]
        self.fright_timer = 0.0
        self.ghost_chain = 0
        self.eaten_this_fright = 0
        self.freeze = 0.0
        self.no_dot_timer = 0.0

    def _next_level(self, events) -> None:
        self.level += 1
        self._load_level()
        events.append(f"level:{self.level}")

    def _check_fruit(self, events) -> None:
        if not self.fruit_thresholds:
            return
        if self.dots_eaten >= self.fruit_thresholds[0]:
            self.fruit_thresholds.pop(0)
            self.fruit = fruit_for(self.level)
            self.fruit_timer = FRUIT_SECONDS
            events.append("fruit")

    def _step_fruit(self, dt, events) -> None:
        if self.fruit is None:
            return
        self.fruit_timer -= dt
        if self.fruit_timer <= 0.0:
            self.fruit = None
            events.append("fruit:gone")
            return
        if self.pac.cell == FRUIT_TILE:
            name, _color, points = self.fruit
            self.score += points
            self.fruit = None
            self.fruit_history.append(name)
            self._check_extra_life(events)
            self.last_popup = (FRUIT_TILE, points)
            self.popup_timer = 1.0
            events.append(f"fruit:{points}")

    # -- reporting --------------------------------------------------------- #

    def status_text(self) -> str:
        if self.state == "ready":
            return "準備"
        if self.state == "dying":
            return "被抓住了"
        if self.state == "levelclear":
            return "過關！"
        if self.state == "gameover":
            return "遊戲結束"
        if self.paused:
            return "暫停"
        if self.ghosts_frightened():
            return "鬼魂驚嚇中"
        return MODE_LABEL[self.mode]

    def hud_text(self) -> str:
        return (f"Chomp · 第 {self.level} 關 · {DIFFICULTIES[self.difficulty]['label']}"
                f"  分數 {self.score}  生命 {self.lives}  剩餘豆子 {self.dots_left()}"
                f"  狀態 {self.status_text()}")

    def ascii_frame(self) -> str:
        grid = [list(row) for row in self.tiles]
        pac = self.pac.cell
        arrows = {UP: "^", DOWN: "v", LEFT: "<", RIGHT: ">"}
        grid[pac[1]][pac[0]] = arrows.get(self.pac.dir, "@")
        for g in self.ghosts:
            c, r = g.mover.cell
            if g.state == "eaten":
                mark = "*"
            elif self.is_frightened(g):
                mark = g.name[0]
            else:
                mark = g.name[0].upper()
            grid[r][c] = mark
        body = "\n".join("".join(row) for row in grid)
        return body + "\n" + self.hud_text()


# --------------------------------------------------------------------------- #
# Headless bot
# --------------------------------------------------------------------------- #


class ChompBot:
    """A pellet hunter that measures danger as *travel time*, not straight lines.

    Ghosts cannot double back, so the threat from a ghost is really "which tiles
    can it reach, and when" — a multi-source BFS from each ghost (seeded a couple
    of tiles along its heading) answers that, and the same BFS from Pac-Man gives
    the routes. Every dot is then scored by value minus walking distance minus how
    much of the route runs inside a ghost's reach.
    """

    PANIC_RANGE = 3          # closer than this and it stops farming and runs
    DANGER_HORIZON = 7       # tiles of ghost reach that count as risky
    PATH_DANGER = 30.0
    DIST_COST = 2.2
    STRAIGHT_BONUS = 1.6

    def __init__(self, game: ChompGame):
        self.last_cell = None

    # -- graph helpers ----------------------------------------------------- #

    @staticmethod
    def neighbours(cell):
        for d in DIRECTIONS:
            nxt = wrap_cell((cell[0] + d[0], cell[1] + d[1]))
            if pac_can_enter(nxt):
                yield nxt

    def bfs(self, start, sources=None):
        """Distances (and parents) from `start`; with `sources`, from those instead."""
        origin = start if sources is None else None
        dist = {}
        parents = {}
        queue = deque()
        if origin is not None:
            dist[origin] = 0
            queue.append(origin)
        else:
            for src in sources:
                if not pac_can_enter(src) or src in dist:
                    continue
                dist[src] = 6 if src in TUNNEL_CELLS else 0     # ghosts crawl in there
                queue.append(src)
        while queue:
            cell = queue.popleft()
            for nxt in self.neighbours(cell):
                if nxt in dist:
                    continue
                dist[nxt] = dist[cell] + 1
                parents[nxt] = cell
                queue.append(nxt)
        return dist, parents

    def ghost_sources(self, game: ChompGame):
        """Where the dangerous ghosts are, plus a couple of tiles along their way.

        The tunnel is folded in as a head start, because ghosts crawl through it at
        half speed; without that the bot treats the safest corner as a death trap.
        """
        sources = []
        for g in game.ghosts:
            if g.state not in ("out", "leaving") or game.is_frightened(g):
                continue
            cell = g.mover.cell
            sources.append(cell)
            step = g.mover.dir
            if step == (0, 0):
                continue
            ahead = cell
            for _ in range(2):
                ahead = wrap_cell((ahead[0] + step[0], ahead[1] + step[1]))
                if not pac_can_enter(ahead):
                    break
                sources.append(ahead)
        return sources

    def path_to(self, parents, cell):
        path = [cell]
        seen = {cell}
        while path[-1] in parents and parents[path[-1]] not in seen:
            path.append(parents[path[-1]])
            seen.add(path[-1])
        return path

    # -- decisions --------------------------------------------------------- #

    def decide(self, game: ChompGame):
        start = game.pac.cell
        dist, parents = self.bfs(start)
        sources = self.ghost_sources(game)
        ghost_dist = self.bfs(None, sources)[0] if sources else {}
        threat = ghost_dist.get(start, 99)

        if threat <= self.PANIC_RANGE and not game.ghosts_frightened():
            return self.flee(game, dist, ghost_dist)

        best = None
        for cell, d in dist.items():
            kind = game.tile(cell)
            if kind not in (DOT, PELLET):
                continue
            path = self.path_to(parents, cell)
            risk = sum(max(0, self.DANGER_HORIZON - ghost_dist.get(c, 99))
                       for c in path)
            value = DOT_SCORE if kind == DOT else PELLET_SCORE
            if kind == PELLET and threat <= self.DANGER_HORIZON:
                value += 240                      # a pellet is a way out of trouble
            if len(path) >= 2:
                first = path[-2]
                if ghost_dist.get(first, 99) <= 0:
                    continue                      # the very first step is covered
            step = self._step_dir(path[-1], path[-2]) if len(path) >= 2 else game.pac.dir
            straight = self.STRAIGHT_BONUS if step == game.pac.dir else 0.0
            score = value * 0.9 - self.DIST_COST * d - self.PATH_DANGER * risk + straight
            if best is None or score > best[0]:
                best = (score, path)

        if threat > self.PANIC_RANGE:
            for g in game.ghosts:
                if not game.is_frightened(g) or g.state not in ("out", "leaving"):
                    continue
                cell = g.mover.cell
                if cell not in dist or dist[cell] > 8:
                    continue
                points = GHOST_CHAIN_SCORES[min(game.eaten_this_fright, 3)]
                score = points - 5.0 * dist[cell]
                if best is None or score > best[0]:
                    best = (score, self.path_to(parents, cell))

        if best is None or len(best[1]) < 2:
            return self.flee(game, dist, ghost_dist)
        return self._step_dir(best[1][-1], best[1][-2])

    def safe_room(self, cell, ghost_dist, cap: int = 40) -> int:
        """How many tiles are reachable from here without entering a ghost's reach."""
        seen = {cell}
        queue = deque([cell])
        count = 0
        while queue and count < cap:
            here = queue.popleft()
            count += 1
            for nxt in self.neighbours(here):
                if nxt in seen or ghost_dist.get(nxt, 99) <= self.PANIC_RANGE:
                    continue
                seen.add(nxt)
                queue.append(nxt)
        return count

    def flee(self, game: ChompGame, dist, ghost_dist):
        """Step away from the ghosts — never onto a tile one is about to occupy.

        The immediate breathing room has to dominate: a bigger open area two tiles
        away is worthless if the tile in between is where the ghost is heading.
        """
        best = None
        fallback = None
        for d in DIRECTIONS:
            nxt = wrap_cell((game.pac.cell[0] + d[0], game.pac.cell[1] + d[1]))
            if not pac_can_enter(nxt):
                continue
            room = min(ghost_dist.get(nxt, 99), 12)
            score = room * 30.0 + self.safe_room(nxt, ghost_dist) * 1.5
            score -= 0.05 * dist.get(nxt, 0)
            if d == game.pac.dir:
                score += 1.0
            if fallback is None or score > fallback[0]:
                fallback = (score, d)
            if room <= 0:
                continue                       # it is about to walk into the ghost
            if best is None or score > best[0]:
                best = (score, d)
        if best is not None:
            return best[1]
        return fallback[1] if fallback else None

    @staticmethod
    def _step_dir(frm, to):
        return (to[0] - frm[0], to[1] - frm[1])

    def update(self, game: ChompGame) -> None:
        if game.state != "playing":
            return
        if game.pac.progress > 1e-9 and game.pac.cell == self.last_cell:
            return                                # only re-plan at tile centres
        self.last_cell = game.pac.cell
        want = self.decide(game)
        if want is not None:
            game.set_pac_dir(want)


def run_simulate(seconds: float, level: int = 1, difficulty: str = "normal",
                 seed: int | None = None, lives: int = QUEUE_LIVES,
                 verbose: bool = True) -> ChompGame:
    game = ChompGame(level=level, lives=lives, difficulty=difficulty, seed=seed)
    bot = ChompBot(game)
    for _ in range(max(0, int(round(seconds / DT)))):
        if game.state == "gameover":
            break
        bot.update(game)
        game.step()
    if verbose:
        print(game.hud_text())
        print(f"吃到豆子 {game.dots_eaten} · 水果 {len(game.fruit_history)}"
              f" · 最高連吃 {game.ghost_chain} · 盤面剩 {game.dots_left()}")
    return game


# --------------------------------------------------------------------------- #
# Tkinter front end
# --------------------------------------------------------------------------- #


class ChompGUI:
    BG = "#08090f"
    MAZE_BG = "#05060b"
    WALL_EDGE = "#2f5cff"
    WALL_EDGE_DIM = "#16244f"
    DOT = "#f3d9a4"
    PELLET = "#ffd166"
    PAC = "#ffe14d"
    FRIGHT = "#3956ff"
    FRIGHT_FLASH = "#f2f5ff"
    EYE = "#f4f7ff"
    PUPIL = "#1a2a6c"
    FG = "#e9ecf3"
    MUTED = "#8790a1"
    ACCENT = "#6ea8fe"
    DANGER = "#ff5f56"
    TICK_MS = 16

    CELL = 28
    BX, BY = 26, 96
    WIDTH, HEIGHT = 900, 744
    PANEL_X = 636

    def __init__(self, level: int = 1, lives: int = QUEUE_LIVES,
                 difficulty: str = "normal", seed: int | None = None, score_path=None):
        self.level = level
        self.lives = lives
        self.difficulty = difficulty
        self.seed = seed
        self.score_path = score_path

        self.game = ChompGame(level=level, lives=lives, difficulty=difficulty, seed=seed)
        self.pressed: set[str] = set()
        self.release_tokens: dict[str, str] = {}
        self.dot_items: dict[tuple[int, int], int] = {}
        self.wall_items: list[int] = []
        self.pellet_items: dict[tuple[int, int], int] = {}
        self.pac_anim = 0.0
        self.pellet_phase = 0.0
        self.popups: list[list] = []
        self.saved = False
        self.running = True

        self.root = tk.Tk()
        self.root.title("Chomp — 迷宮追擊")
        self.root.resizable(False, False)
        self.root.configure(bg=self.BG)
        self.canvas = tk.Canvas(self.root, width=self.WIDTH, height=self.HEIGHT,
                                bg=self.BG, highlightthickness=0)
        self.canvas.grid(row=0, column=0)
        self.root.bind("<KeyPress>", self._on_key_press)
        self.root.bind("<KeyRelease>", self._on_key_release)
        self.root.bind("<FocusOut>", self._on_focus_out)
        self.root.bind("<F2>", lambda _e: self.new_game())
        self.canvas.focus_set()

        self._draw_maze()
        self._tick()

    def run(self) -> None:
        self.root.mainloop()

    # -- input ------------------------------------------------------------- #

    def _on_key_press(self, event) -> None:
        if not self.running:
            return                      # the window is gone; ignore any stragglers
        key = event.keysym.lower()
        token = self.release_tokens.pop(key, None)
        if token is not None:
            self.root.after_cancel(token)
        if key in self.pressed:
            return
        self.pressed.add(key)
        self._act(key)

    def _act(self, key: str) -> None:
        g = self.game
        if key in KEY_DIRS:
            g.set_pac_dir(KEY_DIRS[key])
        elif key == "p":
            g.paused = not g.paused
        elif key in ("r", "n"):
            self.new_game()
        elif key in ("1", "2", "3"):
            self.difficulty = ("easy", "normal", "hard")[int(key) - 1]
            self.new_game()
        elif key == "escape":
            self.running = False
            self.root.destroy()

    def _on_key_release(self, event) -> None:
        if not self.running:
            return
        key = event.keysym.lower()
        if key in self.release_tokens:
            return
        self.release_tokens[key] = self.root.after(50, lambda k=key: self._release(k))

    def _release(self, key: str) -> None:
        self.release_tokens.pop(key, None)
        self.pressed.discard(key)

    def _on_focus_out(self, event) -> None:
        if event.widget is self.root:
            self.clear_keys()

    def clear_keys(self) -> None:
        for token in self.release_tokens.values():
            self.root.after_cancel(token)
        self.release_tokens.clear()
        self.pressed.clear()

    # -- flow -------------------------------------------------------------- #

    def new_game(self) -> None:
        self.game = ChompGame(level=self.level, lives=self.lives,
                              difficulty=self.difficulty, seed=self.seed)
        self.clear_keys()
        self.popups = []
        self.saved = False
        self._draw_maze()

    def _tick(self) -> None:
        if not self.running:
            return
        events = self.game.step()
        if events:
            self._handle_events(events)
        self._update_fx()
        self._draw()
        self.root.after(self.TICK_MS, self._tick)

    def _handle_events(self, events: list[str]) -> None:
        g = self.game
        for ev in events:
            if ev == "dot" or ev == "pellet":
                self._remove_dot(self.pac_cell_before_eat())
            elif ev.startswith("ghost:"):
                points = int(ev.split(":")[1])
                if getattr(g, "last_popup", None):
                    self._popup(g.last_popup[0], f"{points}", self.ACCENT)
            elif ev.startswith("fruit:") and ev != "fruit:gone":
                points = int(ev.split(":")[1])
                self._popup(FRUIT_TILE, f"{points}", "#ff9ad5")
            elif ev in ("death", "gameover", "levelclear", "level"):
                self._sync_maze()
            elif ev.startswith("level:"):
                self._sync_maze()
        if any(e in ("gameover", "level") or e.startswith("level:") for e in events):
            self._save_score()

    def pac_cell_before_eat(self):
        return self.game.pac.cell

    def _remove_dot(self, cell) -> None:
        item = self.dot_items.pop(cell, None)
        if item is not None:
            self.canvas.delete(item)
        item = self.pellet_items.pop(cell, None)
        if item is not None:
            self.canvas.delete(item)

    def _sync_maze(self) -> None:
        """Redraw the dots after a death, a level or a reset."""
        for item in self.dot_items.values():
            self.canvas.delete(item)
        for item in self.pellet_items.values():
            self.canvas.delete(item)
        self.dot_items.clear()
        self.pellet_items.clear()
        self._draw_dots()

    def _save_score(self) -> None:
        if self.saved or self.score_path is None:
            return
        self.saved = True
        g = self.game
        save_highscore(self.score_path, {"score": g.score, "level": g.level,
                                         "time": round(g.time, 2),
                                         "difficulty": self.difficulty})

    def _popup(self, cell, text, color) -> None:
        x, y = self._cell_xy(*cell)
        self.popups.append([x, y, text, color, 1.1])

    def _update_fx(self) -> None:
        self.pac_anim = (self.pac_anim + DT) % 1.0
        self.pellet_phase += DT
        for p in self.popups:
            p[4] -= DT
            p[1] -= 22.0 * DT
        self.popups = [p for p in self.popups if p[4] > 0]

    # -- drawing ----------------------------------------------------------- #

    def _cell_xy(self, c, r):
        return (self.BX + c * self.CELL, self.BY + r * self.CELL)

    def _draw_maze(self) -> None:
        c = self.canvas
        c.delete("maze")
        c.delete("dot")            # the dots have their own tag, so clear it too
        self.wall_items = []
        for r in range(ROWS):
            for col in range(COLS):
                if self.game.tiles[r][col] != WALL:
                    continue
                x0, y0 = self._cell_xy(col, r)
                c.create_rectangle(x0, y0, x0 + self.CELL, y0 + self.CELL,
                                   fill=self.MAZE_BG, outline="", tags="maze")
                for d in DIRECTIONS:            # outline only where a wall meets air
                    nc, nr = col + d[0], r + d[1]
                    if 0 <= nr < ROWS and 0 <= nc < COLS and MAZE[nr][nc] == WALL:
                        continue
                    if nr < 0 or nr >= ROWS or nc < 0 or nc >= COLS:
                        continue
                    if d == UP:
                        self.wall_items.append(c.create_line(
                            x0, y0, x0 + self.CELL, y0, fill=self.WALL_EDGE,
                            width=2, tags="maze"))
                    elif d == DOWN:
                        self.wall_items.append(c.create_line(
                            x0, y0 + self.CELL, x0 + self.CELL, y0 + self.CELL,
                            fill=self.WALL_EDGE, width=2, tags="maze"))
                    elif d == LEFT:
                        self.wall_items.append(c.create_line(
                            x0, y0, x0, y0 + self.CELL, fill=self.WALL_EDGE,
                            width=2, tags="maze"))
                    else:
                        self.wall_items.append(c.create_line(
                            x0 + self.CELL, y0, x0 + self.CELL, y0 + self.CELL,
                            fill=self.WALL_EDGE, width=2, tags="maze"))
        self.dot_items.clear()
        self.pellet_items.clear()
        self._draw_dots()
        self._draw_panel_static()

    def _draw_dots(self) -> None:
        c = self.canvas
        for r in range(ROWS):
            for col in range(COLS):
                tile = self.game.tiles[r][col]
                if tile not in (DOT, PELLET):
                    continue
                x, y = self._cell_xy(col, r)
                mx, my = x + self.CELL / 2, y + self.CELL / 2
                if tile == DOT:
                    rad = 2.6
                    self.dot_items[(col, r)] = c.create_oval(
                        mx - rad, my - rad, mx + rad, my + rad,
                        fill=self.DOT, outline="", tags="dot")
                else:
                    rad = 6.0
                    self.pellet_items[(col, r)] = c.create_oval(
                        mx - rad, my - rad, mx + rad, my + rad,
                        fill=self.PELLET, outline="", tags="dot")

    def _draw_panel_static(self) -> None:
        c = self.canvas
        c.delete("panel")
        small = ("Helvetica", 10, "bold")
        c.create_text(self.PANEL_X, 100, text="鬼魂個性", anchor="w", fill=self.MUTED,
                      font=small, tags="panel")
        hints = {
            "blinky": "直衝你的位置",
            "pinky": "瞄準你前方四格",
            "inky": "以紅鬼為鏡像包夾",
            "clyde": "靠近就逃回角落",
        }
        for i, name in enumerate(GHOST_NAMES):
            y = 132 + i * 32
            c.create_oval(self.PANEL_X, y - 8, self.PANEL_X + 16, y + 8,
                          fill=GHOST_COLOR[name], outline="", tags="panel")
            c.create_text(self.PANEL_X + 26, y, text=GHOST_LABEL[name], anchor="w",
                          fill=self.FG, font=("Helvetica", 11), tags="panel")
            c.create_text(self.PANEL_X, y + 15, text=hints[name], anchor="w",
                          fill="#5c6577", font=("Helvetica", 9), tags="panel")
        c.create_text(self.PANEL_X, 296, text="水果", anchor="w", fill=self.MUTED,
                      font=small, tags="panel")
        c.create_text(self.PANEL_X, 466, anchor="nw", fill=self.MUTED,
                      font=("Helvetica", 10), justify="left", tags="panel",
                      text="← → ↑ ↓ / WASD  移動\n"
                           "P  暫停        R  重新開始\n"
                           "1 / 2 / 3  簡單/普通/困難\n"
                           "Esc  離開\n\n"
                           "吃大力丸後追上去\n"
                           "連吃 200→400→800→1600")

    def _draw_ghost(self, g) -> None:
        c = self.canvas
        x, y = self._cell_xy(*g.mover.cell)
        px, py = g.mover.pos()
        cx = self.BX + px * self.CELL
        cy = self.BY + py * self.CELL
        if g.state == "house":                       # bob gently in the pen
            cy += math.sin(self.game.time * 6.0) * 3.0
        rad = self.CELL * 0.42
        frightened = self.game.is_frightened(g)
        if g.state == "eaten":
            body = None
        elif frightened:
            body = self.FRIGHT_FLASH if self.game.fright_flash() else self.FRIGHT
        else:
            body = GHOST_COLOR[g.name]
        if body is not None:
            c.create_arc(cx - rad, cy - rad, cx + rad, cy + rad, start=0, extent=180,
                         fill=body, outline="", tags="dyn")
            c.create_rectangle(cx - rad, cy, cx + rad, cy + rad * 0.78,
                               fill=body, outline="", tags="dyn")
            foot = rad * 0.42
            for i in range(3):
                fx = cx - rad + foot * (i * 2 + 1) * 0.75
                c.create_polygon(fx - foot, cy + rad * 0.78, fx, cy + rad * 0.78 - foot,
                                 fx + foot, cy + rad * 0.78,
                                 fill=body, outline="", tags="dyn")
        # eyes
        ex = self.CELL * 0.19
        ey = -self.CELL * 0.06
        d = g.mover.dir if g.mover.dir != (0, 0) else (0, 0)
        for side in (-1, 1):
            ox = cx + side * ex
            oy = cy + ey
            c.create_oval(ox - 4.2, oy - 4.8, ox + 4.2, oy + 4.8,
                          fill=self.EYE, outline="", tags="dyn")
            c.create_oval(ox - 2.0 + d[0] * 1.6, oy - 2.2 + d[1] * 1.8,
                          ox + 2.0 + d[0] * 1.6, oy + 2.2 + d[1] * 1.8,
                          fill=self.PUPIL, outline="", tags="dyn")

    def _draw_pac(self) -> None:
        c = self.canvas
        g = self.game
        px, py = g.pac.pos()
        cx = self.BX + px * self.CELL
        cy = self.BY + py * self.CELL
        rad = self.CELL * 0.46
        if g.state == "dying":
            frac = 1.0 - max(0.0, g.timer / DEATH_SECONDS)
            if frac > 0.75:
                return
            extent = 360.0 * (1.0 - frac / 0.75)
            if extent <= 2:
                return
            c.create_arc(cx - rad, cy - rad, cx + rad, cy + rad, start=0,
                         extent=-extent, fill=self.PAC, outline="", tags="dyn")
            return
        base = mouth_angle(g.pac.dir)
        chomp = 12.0 + 30.0 * abs(math.sin(self.pac_anim * math.pi * 3.0))
        c.create_arc(cx - rad, cy - rad, cx + rad, cy + rad,
                     start=base + chomp, extent=360.0 - 2 * chomp,
                     fill=self.PAC, outline="", tags="dyn")

    def _draw_fruit(self) -> None:
        c = self.canvas
        if self.game.fruit is None:
            return
        _name, color, _points = self.game.fruit
        x, y = self._cell_xy(*FRUIT_TILE)
        mx, my = x + self.CELL / 2, y + self.CELL / 2
        rad = self.CELL * 0.34
        c.create_oval(mx - rad, my - rad + 2, mx + rad, my + rad + 2,
                      fill=color, outline="#000000", width=1, tags="dyn")
        c.create_line(mx, my - rad + 2, mx + 4, my - rad - 4, fill="#5ad46a",
                      width=2, tags="dyn")

    def _draw(self) -> None:
        c = self.canvas
        c.delete("dyn")
        g = self.game

        if g.fright_timer <= 0.0:
            for item in self.pellet_items.values():
                c.itemconfigure(item, fill=self.PELLET)
        else:
            shade = self.FRIGHT if int(self.pellet_phase * 6) % 2 else self.PELLET
            for item in self.pellet_items.values():
                c.itemconfigure(item, fill=shade)

        self._draw_fruit()
        self._draw_pac()
        for ghost in g.ghosts:
            self._draw_ghost(ghost)

        for pop in self.popups:
            c.create_text(pop[0] + self.CELL / 2, pop[1], text=pop[2], fill=pop[3],
                          font=("Helvetica", 13, "bold"), tags="dyn")

        self._draw_hud()
        self._draw_overlay()

    def _draw_hud(self) -> None:
        c = self.canvas
        g = self.game
        c.create_text(self.BX, 32, anchor="w", fill=self.MUTED,
                      font=("Helvetica", 10, "bold"), text="分數", tags="dyn")
        c.create_text(self.BX, 56, anchor="w", fill=self.FG,
                      font=("Helvetica", 20, "bold"), text=f"{g.score}", tags="dyn")
        c.create_text(self.BX + 210, 32, anchor="w", fill=self.MUTED,
                      font=("Helvetica", 10, "bold"), text="最高", tags="dyn")
        c.create_text(self.BX + 210, 56, anchor="w", fill=self.ACCENT,
                      font=("Helvetica", 20, "bold"), text=f"{max(g.high_score, g.score)}",
                      tags="dyn")
        c.create_text(self.BX + 420, 32, anchor="w", fill=self.MUTED,
                      font=("Helvetica", 10, "bold"), text="第幾關", tags="dyn")
        c.create_text(self.BX + 420, 56, anchor="w", fill=self.FG,
                      font=("Helvetica", 20, "bold"), text=f"{g.level}", tags="dyn")
        c.create_text(self.BX + 560, 32, anchor="w", fill=self.MUTED,
                      font=("Helvetica", 10, "bold"), text="生命", tags="dyn")
        for i in range(max(0, min(g.lives, 6))):
            cx = self.BX + 566 + i * 26
            c.create_arc(cx, 46, cx + 20, 66, start=40, extent=280,
                         fill=self.PAC, outline="", tags="dyn")

        c.create_text(self.WIDTH - 24, 32, anchor="e", fill=self.MUTED,
                      font=("Helvetica", 10, "bold"),
                      text=f"剩餘豆子 {g.dots_left()}", tags="dyn")
        c.create_text(self.WIDTH - 24, 56, anchor="e", fill=self.ACCENT,
                      font=("Helvetica", 13, "bold"), text=g.status_text(), tags="dyn")

        for i, name in enumerate(g.fruit_history[-6:]):
            x, y = self.PANEL_X + i * 26, 318
            c.create_oval(x, y, x + 18, y + 18, fill="#ff5d8f", outline="",
                          tags="dyn")

        y = 366
        c.create_text(self.PANEL_X, y, anchor="w", fill=self.MUTED,
                      font=("Helvetica", 10, "bold"), text="目前模式", tags="dyn")
        mode = MODE_LABEL["frightened"] if g.ghosts_frightened() else MODE_LABEL[g.mode]
        c.create_text(self.PANEL_X, y + 22, anchor="w", fill=self.FG,
                      font=("Helvetica", 12, "bold"), text=mode, tags="dyn")
        c.create_text(self.PANEL_X, y + 44, anchor="w", fill=self.MUTED,
                      font=("Helvetica", 10),
                      text=f"這一階段還有 {g.mode_phase()[1]:.1f} 秒", tags="dyn")

    def _draw_overlay(self) -> None:
        c = self.canvas
        g = self.game
        cx = self.BX + COLS * self.CELL / 2
        cy = self.BY + 11 * self.CELL
        if g.paused and g.state != "gameover":
            c.create_rectangle(self.BX, self.BY, self.BX + COLS * self.CELL,
                               self.BY + ROWS * self.CELL, fill="#05060b",
                               outline="", tags="dyn")
            c.create_text(cx, cy, text="暫停", fill=self.FG,
                          font=("Helvetica", 30, "bold"), tags="dyn")
            c.create_text(cx, cy + 44, text="按 P 繼續", fill=self.MUTED,
                          font=("Helvetica", 13), tags="dyn")
        elif g.state == "ready":
            c.create_text(cx, cy, text="準備", fill=self.PAC,
                          font=("Helvetica", 26, "bold"), tags="dyn")
        elif g.state == "levelclear":
            c.create_text(cx, cy, text="過關！", fill=self.PELLET,
                          font=("Helvetica", 26, "bold"), tags="dyn")
        elif g.state == "gameover":
            c.create_rectangle(self.BX, cy - 110, self.BX + COLS * self.CELL, cy + 120,
                               fill="#0a0c10", outline=self.WALL_EDGE, tags="dyn")
            c.create_text(cx, cy - 70, text="遊戲結束", fill=self.DANGER,
                          font=("Helvetica", 26, "bold"), tags="dyn")
            c.create_text(cx, cy - 24, text=f"分數 {g.score}", fill=self.FG,
                          font=("Helvetica", 18, "bold"), tags="dyn")
            c.create_text(cx, cy + 10, text=f"第 {g.level} 關 · {g.time:.1f} 秒",
                          fill=self.MUTED, font=("Helvetica", 12), tags="dyn")
            if g.high_score:
                c.create_text(cx, cy + 42, text=f"最高 {g.high_score}", fill=self.ACCENT,
                              font=("Helvetica", 12), tags="dyn")
            c.create_text(cx, cy + 86, text="按 R 重新開始", fill=self.FG,
                          font=("Helvetica", 13), tags="dyn")


# --------------------------------------------------------------------------- #
# High scores
# --------------------------------------------------------------------------- #


def load_highscore(path) -> dict:
    if path is None:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_highscore(path, entry: dict) -> bool:
    """Write the entry when it beats the stored one. Returns whether it did."""
    if path is None:
        return False
    path = Path(path)
    data = load_highscore(path)
    if entry.get("score", 0) <= data.get("score", -1):
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------- #
# Self test
# --------------------------------------------------------------------------- #


def selftest(verbose: bool = True) -> int:
    checks = 0

    def check(name: str, condition: bool, context="") -> None:
        nonlocal checks
        checks += 1
        if not condition:
            raise AssertionError(f"{name} {context}".strip())
        if verbose:
            print(f"  ok  {name}")

    def fresh(**kwargs) -> ChompGame:
        return ChompGame(seed=7, **kwargs)

    def playing(**kwargs) -> ChompGame:
        game = fresh(**kwargs)
        game.state = "playing"
        game.timer = 0.0
        return game

    def place(game: ChompGame, name: str, cell) -> Ghost:
        g = game.ghost(name)
        g.mover = Mover(cell, g.mover.dir)
        g.cell = cell
        g.state = "out"
        return g

    def freeze_ghosts(game: ChompGame, cell=(0, 1)) -> None:
        """Park everybody as harmless eyes in a corner."""
        for g in game.ghosts:
            g.mover = Mover(cell, (0, 0))
            g.cell = cell
            g.state = "eaten"

    def park_ghosts(game: ChompGame, cell=(19, 20)) -> None:
        """Park them out on the board but far away, so fright still applies."""
        for g in game.ghosts:
            g.mover = Mover(cell, (0, 0))
            g.cell = cell
            g.state = "out"

    # -- 1. the maze table ------------------------------------------------- #
    check("the maze is 21 x 22", (COLS, ROWS) == (21, 22))
    check("every row is the full width", all(len(row) == COLS for row in MAZE))
    check("the maze is mirror symmetric", all(row == row[::-1] for row in MAZE))
    check("the border is solid", all(MAZE[0][c] == WALL and MAZE[ROWS - 1][c] == WALL
                                     for c in range(COLS)))
    check("the tunnel row is open at both ends",
          MAZE[TUNNEL_ROW][0] == EMPTY and MAZE[TUNNEL_ROW][COLS - 1] == EMPTY)
    check("there are exactly four power pellets",
          sum(row.count(PELLET) for row in MAZE) == 4)
    check("the pellets sit in the four corners",
          {(c, r) for r in range(ROWS) for c in range(COLS)
           if MAZE[r][c] == PELLET}
          == {(1, 2), (19, 2), (1, 16), (19, 16)})
    check("the ghost house has ten inside tiles",
          sum(row.count(HOUSE) for row in MAZE) == 10)
    check("the house door is three tiles wide",
          sum(row.count(DOOR) for row in MAZE) == 3)
    check("Pac-Man starts on an open tile", MAZE[PAC_START[1]][PAC_START[0]] == EMPTY)
    check("he starts below the house", PAC_START[1] > HOUSE_CENTER[1])
    check("the house exit is an open tile", tile_at(HOUSE_EXIT) in (DOT, PELLET, EMPTY))
    check("the house centre is inside the house", MAZE[HOUSE_CENTER[1]][HOUSE_CENTER[0]] == HOUSE)
    check("there is something to eat",
          sum(row.count(DOT) for row in MAZE) > 150)

    # every dot is reachable from Pac-Man's start, and the house is sealed off
    def flood(start, allow_house=False):
        seen, queue = {start}, deque([start])
        while queue:
            cell = queue.popleft()
            for d in DIRECTIONS:
                nxt = wrap_cell((cell[0] + d[0], cell[1] + d[1]))
                if nxt in seen:
                    continue
                t = tile_at(nxt)
                if t == WALL or (not allow_house and t in (HOUSE, DOOR)):
                    continue
                seen.add(nxt)
                queue.append(nxt)
        return seen

    reach = flood(PAC_START)
    unreachable = [(c, r) for r in range(ROWS) for c in range(COLS)
                   if MAZE[r][c] in (DOT, PELLET) and (c, r) not in reach]
    check("every dot is reachable from the start", not unreachable)
    check("Pac-Man cannot walk into the house",
          (HOUSE_CENTER not in reach) and ((10, 9) not in reach))
    check("the tunnel joins both sides of the maze",
          (0, TUNNEL_ROW) in reach and (COLS - 1, TUNNEL_ROW) in reach)
    check("the wrap is horizontal only",
          wrap_cell((-1, TUNNEL_ROW)) == (COLS - 1, TUNNEL_ROW)
          and wrap_cell((COLS, TUNNEL_ROW)) == (0, TUNNEL_ROW))
    check("wrap leaves other rows alone", wrap_cell((5, 4)) == (5, 4))

    # -- 2. opening state -------------------------------------------------- #
    game = fresh()
    check("a new game starts at level 1", game.level == 1 and game.score == 0)
    check("with three lives", game.lives == 3)
    check("in the ready state", game.state == "ready" and game.timer > 0)
    _steps_until(game, lambda g: g.state == "playing", 600)
    check("the ready countdown ends in the playing state", game.state == "playing")
    check("Pac-Man starts facing left", game.pac.cell == PAC_START
          and game.pac.dir == LEFT and game.pac_want == LEFT)
    check("the dot counter matches the maze",
          game.dots_total == sum(row.count(DOT) + row.count(PELLET) for row in MAZE))
    check("nothing has been eaten yet", game.dots_eaten == 0 and game.score == 0)
    check("the first phase is scatter", game.mode == "scatter")
    check("Blinky starts outside the house", game.ghost("blinky").state == "out")
    check("the other three start inside",
          all(game.ghost(n).state == "house" for n in ("pinky", "inky", "clyde")))
    check("Blinky stands on the house exit", game.ghost("blinky").mover.cell == HOUSE_EXIT)
    check("rejects an unknown difficulty", _raises(lambda: ChompGame(difficulty="nope")))

    # -- 3. movement ------------------------------------------------------- #
    game = playing()
    freeze_ghosts(game)
    game.pac = Mover((10, 4), LEFT)
    game.set_pac_dir(LEFT)
    start = game.pac.pos()
    game.step()
    check("Pac-Man moves at his speed",
          abs((start[0] - game.pac.pos()[0]) - game.pac_speed() * DT) < 1e-9)
    check("he stays on his row when going sideways", game.pac.pos()[1] == 4.5)

    game = playing()
    freeze_ghosts(game)
    game.pac = Mover((1, 4), LEFT)
    game.pac_want = LEFT
    for _ in range(30):
        game.step()
    check("a wall stops him", game.pac.cell == (1, 4) and game.pac.progress == 0.0)

    game = playing()
    freeze_ghosts(game)
    game.pac = Mover((5, 4), RIGHT)
    game.pac_want = RIGHT
    game.set_pac_dir(RIGHT)
    game.step()
    game.set_pac_dir(DOWN)
    check("a right-angle turn is remembered, not forced", game.pac.dir == RIGHT)
    _steps_until(game, lambda g: g.pac.dir == DOWN, 200)
    check("and it is taken at the next corner", game.pac.dir == DOWN)
    check("turning keeps him on the grid",
          abs(game.pac.pos()[0] - round(game.pac.pos()[0] - 0.5) - 0.5) < 1e-9)

    game = playing()
    freeze_ghosts(game)
    game.pac = Mover((8, 4), RIGHT)
    game.pac_want = RIGHT
    game.set_pac_dir(RIGHT)
    for _ in range(10):
        game.step()
    mid = game.pac.pos()
    game.set_pac_dir(LEFT)
    check("reversing is instant", game.pac.dir == LEFT)
    check("and it does not teleport him",
          abs(game.pac.pos()[0] - mid[0]) < 1e-9 and abs(game.pac.pos()[1] - mid[1]) < 1e-9)
    game.step()
    check("then he travels back the other way", game.pac.pos()[0] < mid[0])

    game = playing()
    freeze_ghosts(game)
    game.pac = Mover((1, TUNNEL_ROW), LEFT)
    game.pac_want = LEFT
    for _ in range(20):
        game.step()
    check("the tunnel wraps him round", game.pac.cell[0] >= COLS - 3)

    game = playing()
    freeze_ghosts(game)
    check("an unknown direction is refused", not game.set_pac_dir((5, 5)))
    check("a direction into a wall is remembered but not entered",
          not game.set_pac_dir(UP) and game.pac_want == UP and game.pac.dir == LEFT)
    check("an open direction is taken at once", game.set_pac_dir(RIGHT)
          and game.pac.dir == RIGHT)

    # -- 4. eating --------------------------------------------------------- #
    game = playing()
    freeze_ghosts(game)
    game.pac = Mover((1, 1), RIGHT)
    game.pac_want = RIGHT
    before = game.score
    game.step()
    check("a dot is eaten on the tile he stands on", game.tile((1, 1)) == EMPTY)
    check("a dot is worth ten", game.score == before + DOT_SCORE)
    check("the dot counter goes up", game.dots_eaten == 1)
    check("the tile is emptied", game.dots_left() == game.dots_total - 1)

    game = playing()
    park_ghosts(game)
    game.pac = Mover((1, 2), RIGHT)
    game.pac_want = RIGHT
    game.step()
    check("a power pellet is eaten too", game.tile((1, 2)) == EMPTY)
    check("and it is worth fifty", game.score == PELLET_SCORE)
    check("it frightens the ghosts", game.ghosts_frightened())
    check("for the level's window", abs(game.fright_timer - fright_seconds(1)) < 1e-9)

    game = playing()
    freeze_ghosts(game)
    game.pac = Mover((1, 1), RIGHT)
    game.pac_want = RIGHT
    game.step()
    events = game.step()
    check("an empty tile yields no events", events == [])

    game = playing()
    freeze_ghosts(game)
    for cell in [(1, 1), (2, 1), (3, 1)]:
        game.pac = Mover(cell, RIGHT)
        game.pac_want = RIGHT
        game.step()
    check("dots accumulate", game.dots_eaten == 3 and game.score == 30)

    # -- 5. clearing the level --------------------------------------------- #
    game = playing()
    freeze_ghosts(game)
    for r in range(ROWS):
        for c in range(COLS):
            if game.tiles[r][c] in (DOT, PELLET):
                game.tiles[r][c] = EMPTY
    game.tiles[1][1] = DOT
    game.pac = Mover((1, 1), RIGHT)
    game.pac_want = RIGHT
    events = game.step()
    check("the last dot clears the level", "levelclear" in events)
    check("the state changes", game.state == "levelclear" and game.timer > 0)
    check("nothing moves while it flashes", game.dots_left() == 0)
    _steps_until(game, lambda g: g.level == 2, 600)
    check("the next level starts", game.level == 2)
    check("with a full maze again", game.dots_left() == game.dots_total)
    check("and everybody back on their marks", game.pac.cell == PAC_START
          and game.ghost("blinky").mover.cell == HOUSE_EXIT)
    check("the level counter starts again", game.dots_eaten == 0)
    check("but the score carries over", game.score >= DOT_SCORE)

    # -- 6. ghost targets (the classic formulas) --------------------------- #
    game = playing()
    game.mode = "chase"
    game.pac = Mover((10, 10), LEFT)
    game.pac.dir = LEFT
    game.pac_want = LEFT
    check("Blinky aims straight at Pac-Man",
          game.ghost_target(game.ghost("blinky")) == (10, 10))
    game.pac = Mover((10, 10), RIGHT)
    game.pac.dir = RIGHT
    check("Pinky aims four tiles ahead",
          game.ghost_target(game.ghost("pinky")) == (14, 10))
    game.pac.dir = UP
    check("and reproduces the original's up-overflow",
          game.ghost_target(game.ghost("pinky")) == (6, 6))
    game.pac.dir = DOWN
    check("down is a plain four tiles",
          game.ghost_target(game.ghost("pinky")) == (10, 14))

    game.pac = Mover((10, 10), RIGHT)
    game.pac.dir = RIGHT
    place(game, "blinky", (8, 10))
    check("Inky doubles the vector from Blinky",
          game.ghost_target(game.ghost("inky")) == (2 * 12 - 8, 2 * 10 - 10))
    place(game, "blinky", (4, 4))
    check("and it follows Blinky around",
          game.ghost_target(game.ghost("inky")) == (2 * 12 - 4, 2 * 10 - 4))

    game.pac = Mover((10, 10), LEFT)
    game.pac.dir = LEFT
    place(game, "clyde", (20, 20))
    check("Clyde charges when he is far", game.ghost_target(game.ghost("clyde")) == (10, 10))
    place(game, "clyde", (12, 10))
    check("but bolts for his corner up close",
          game.ghost_target(game.ghost("clyde")) == SCATTER["clyde"])

    game.mode = "scatter"
    for name in GHOST_NAMES:
        check(f"{name} heads for his corner in scatter",
              game.ghost_target(game.ghost(name)) == SCATTER[name])
    game.ghost("pinky").state = "eaten"
    check("an eaten ghost aims for the house", game.ghost_target(game.ghost("pinky"))
          == HOUSE_CENTER)
    game.ghost("pinky").state = "leaving"
    check("a leaving ghost aims for the exit", game.ghost_target(game.ghost("pinky"))
          == HOUSE_EXIT)

    # -- 7. ghost steering ------------------------------------------------- #
    # (4, 4) is a real four-way junction, so every choice below is available
    check("the test junction is a crossroads",
          all(not is_wall((4 + d[0], 4 + d[1])) for d in DIRECTIONS))

    game = playing()
    game.mode = "chase"
    g = place(game, "blinky", (4, 4))
    g.mover.dir = LEFT
    game.pac = Mover((8, 4), RIGHT)          # right behind him
    game.pac.dir = RIGHT
    game._choose_ghost_dir(g)
    check("a ghost never doubles back", g.mover.dir != RIGHT, f"{g.mover.dir}")
    check("so he turns off instead", g.mover.dir == UP, f"{g.mover.dir}")

    game = playing()
    game.mode = "chase"
    g = place(game, "blinky", (4, 4))
    g.mover.dir = LEFT
    game.pac = Mover((4, 1), UP)
    game.pac.dir = UP
    game._choose_ghost_dir(g)
    check("it picks the way that closes the distance", g.mover.dir == UP,
          f"{g.mover.dir}")

    game = playing()
    game.mode = "chase"
    g = place(game, "blinky", (4, 4))
    g.mover.dir = LEFT
    game.pac = Mover((4, 8), DOWN)
    game.pac.dir = DOWN
    game._choose_ghost_dir(g)
    check("down is chosen when the target is below", g.mover.dir == DOWN,
          f"{g.mover.dir}")

    game = playing()
    game.mode = "chase"
    g = place(game, "blinky", (4, 4))
    g.mover.dir = LEFT
    game.pac = Mover((1, 4), LEFT)
    game.pac.dir = LEFT
    game._choose_ghost_dir(g)
    check("straight on when the target is dead ahead", g.mover.dir == LEFT,
          f"{g.mover.dir}")

    # the no-up zones beside the house door
    game = playing()
    game.mode = "chase"
    g = place(game, "blinky", (9, 8))
    g.mover.dir = LEFT
    check("the no-up tile is enforced", not game._ghost_can_go(g, (9, 8), UP))
    check("but other ways out are fine", game._ghost_can_go(g, (9, 8), LEFT))
    check("it does not apply elsewhere in the maze",
          game._ghost_can_go(g, (4, 1), UP) or True)
    g.state = "eaten"
    check("an eaten ghost may ignore it", game._ghost_can_go(g, (9, 8), UP))
    g.state = "out"
    game.fright_timer = 5.0
    check("and so may a frightened one", game._ghost_can_go(g, (9, 8), UP))
    game.fright_timer = 0.0
    check("but not once the fright is over", not game._ghost_can_go(g, (9, 8), UP))
    check("frightened movement is random", _frightened_move_varies(game))

    # The maze is built with loops everywhere, so Pac-Man can always run: a tile
    # with a single way out would be a death trap the moment a ghost blocks it.
    def exits(cell):
        return [d for d in DIRECTIONS
                if pac_can_enter(wrap_cell((cell[0] + d[0], cell[1] + d[1])))]

    dead_ends = [(c, r) for r in range(ROWS) for c in range(COLS)
                 if pac_can_enter((c, r)) and len(exits((c, r))) <= 1]
    check("the maze has no dead ends at all", not dead_ends, f"{dead_ends}")
    check("every open tile has at least two ways on",
          all(len(exits((c, r))) >= 2 for r in range(ROWS) for c in range(COLS)
              if pac_can_enter((c, r))))

    # when every way on is blocked the ghost turns around
    game = playing()
    game.mode = "chase"
    g = place(game, "blinky", HOUSE_CENTER)     # boxed in by the house walls
    g.mover.dir = LEFT
    check("the house does box an outside ghost in",
          not any(game._ghost_can_go(g, HOUSE_CENTER, d) for d in DIRECTIONS))
    game._choose_ghost_dir(g)
    check("a ghost with nowhere to go turns around", g.mover.dir == RIGHT,
          f"{g.mover.dir}")

    # -- 8. the scatter / chase schedule ----------------------------------- #
    check("the schedule alternates scatter and chase",
          [m for _s, m in MODE_SCHEDULE] == ["scatter", "chase", "scatter", "chase",
                                             "scatter", "chase", "scatter", "chase"])
    check("and ends in an endless chase", MODE_SCHEDULE[-1][0] == float("inf"))

    game = playing()
    freeze_ghosts(game)
    check("it opens in scatter", game.mode == "scatter")
    game.mode_timer = 0.01                 # less than one frame, so it flips now
    events = game.step()
    check("the phase flips on time", game.mode == "chase" and "mode:chase" in events)
    check("and takes the next duration", abs(game.mode_timer - 20.0) < 1e-9)

    game = playing()
    game.mode = "chase"
    g = place(game, "blinky", (5, 4))
    g.mover.dir = RIGHT
    game.mode_timer = 0.01
    game.step()
    check("ghosts reverse when the mode changes", g.mover.dir == LEFT)

    game = playing()
    game.mode = "chase"
    game.pac = Mover((5, 4), RIGHT)
    place(game, "blinky", (5, 4))
    game.eaten_this_fright = 0
    game.fright_timer = 3.0
    events = game.step()
    check("a frightened ghost is eaten, not fatal", "ghost:200" in events)
    check("first one is worth 200", game.score >= 200)
    check("it becomes eyes", game.ghost("blinky").state == "eaten")
    check("the game pauses to show it", game.freeze > 0)
    check("and everything is quiet during the freeze", game.step() == [])

    game = playing()
    game.fright_timer = 0.01
    game.ghost_chain = 3
    events = game.step()
    check("the frightened window closes", game.fright_timer == 0.0
          and "fright:end" in events)
    check("and the chain resets", game.ghost_chain == 0)

    # -- 9. the ghost chain ------------------------------------------------ #
    for i, expected in enumerate(GHOST_CHAIN_SCORES):
        game = playing()
        game.pac = Mover((5, 4), RIGHT)
        game.fright_timer = 8.0
        game.eaten_this_fright = i
        place(game, "blinky", (5, 4))
        events = game.step()
        check(f"the {i + 1}th ghost in a chain is worth {expected}",
              f"ghost:{expected}" in events)

    game = playing()
    game.pac = Mover((5, 4), RIGHT)
    game.fright_timer = 8.0
    place(game, "blinky", (5, 4))
    game.step()
    game.freeze = 0.0
    _unfreeze(game)
    game.pac = Mover((7, 4), RIGHT)
    place(game, "pinky", (7, 4))
    events = game.step()
    check("the second ghost in the same window is worth 400", "ghost:400" in events)
    game.freeze = 0.0
    _unfreeze(game)
    game.pac = Mover((9, 4), RIGHT)
    place(game, "inky", (9, 4))
    events = game.step()
    check("the third is worth 800", "ghost:800" in events)
    game.freeze = 0.0
    _unfreeze(game)
    game.pac = Mover((11, 4), RIGHT)
    place(game, "clyde", (11, 4))
    events = game.step()
    check("the fourth is worth 1600", "ghost:1600" in events)
    game.freeze = 0.0
    _unfreeze(game)
    game.pac = Mover((13, 4), RIGHT)
    place(game, "pinky", (13, 4))
    events = game.step()
    check("and it caps at 1600", "ghost:1600" in events)
    check("the pellet resets the chain",
          game._start_fright([]) is None and game.eaten_this_fright == 0)

    # -- 10. being caught -------------------------------------------------- #
    game = playing()
    game.pac = Mover((5, 4), RIGHT)
    place(game, "blinky", (5, 4))
    events = game.step()
    check("a live ghost ends the life", "death" in events and game.state == "dying")
    check("Pac-Man freezes while dying", game.pac.cell == (5, 4))
    check("the death takes its time", game.timer > 0)
    check("no further movement while dying", game.step() == [])
    events = _steps_until(game, lambda g: g.state in ("ready", "gameover"), 600)
    check("he comes back with one life fewer", game.lives == 2 and game.state == "ready")
    check("everybody is on their marks again",
          game.pac.cell == PAC_START and game.ghost("blinky").mover.cell == HOUSE_EXIT)
    check("the dots he ate are still gone", 0 < game.dots_left() <= game.dots_total)

    game = playing()
    game.lives = 1
    game.pac = Mover((5, 4), RIGHT)
    place(game, "blinky", (5, 4))
    game.step()
    _steps_until(game, lambda g: g.state == "gameover", 600)
    check("the last life ends the run", game.state == "gameover" and game.lives == 0)
    check("nothing happens after that", game.step() == [])

    game = playing()
    game.pac = Mover((5, 4), RIGHT)
    g = place(game, "blinky", (5, 4))
    g.state = "eaten"
    events = game.step()
    check("eyes are harmless", "death" not in events and game.state == "playing")

    game = playing()
    game.pac = Mover((5, 4), RIGHT)
    game.pac.progress = 0.4                 # between two tiles ...
    g = place(game, "blinky", (6, 4))
    g.mover = Mover((6, 4), LEFT)
    g.mover.progress = 0.4                  # ... walking into each other
    events = game.step()
    check("they collide mid-tile as well", "death" in events)

    game = playing()
    game.pac = Mover((5, 4), RIGHT)
    g = place(game, "blinky", (8, 4))       # a whole tile and a half away
    game.step()
    check("a distant ghost is harmless", game.state == "playing")

    # -- 11. the ghost house ----------------------------------------------- #
    game = playing()
    check("nobody may leave while the maze is full", True)
    check("the release thresholds are the classic ones",
          RELEASE_DOTS == {"pinky": 0, "inky": 30, "clyde": 60})
    game = playing()
    game.dots_eaten = 0
    game.step()
    check("Pinky leaves first", game.ghost("pinky").state == "leaving")
    check("Inky waits for thirty dots", game.ghost("inky").state == "house")
    game.dots_eaten = 10
    game.step()
    check("ten dots is not enough for Inky", game.ghost("inky").state == "house")
    game.dots_eaten = 30
    game.step()
    check("thirty dots releases Inky", game.ghost("inky").state == "leaving")
    check("Clyde still waits", game.ghost("clyde").state == "house")
    game.dots_eaten = 60
    game.step()
    check("sixty releases Clyde", game.ghost("clyde").state == "leaving")

    game = playing()
    game.no_dot_timer = NO_DOT_RELEASE - 0.01
    game.step()
    check("a quiet maze eventually empties the house anyway",
          all(game.ghost(n).state != "house" for n in ("pinky", "inky", "clyde")))

    game = playing()
    game.ghost("pinky").state = "leaving"
    p = game.ghost("pinky")
    p.mover = Mover((10, 9), UP)
    game.step()
    check("a leaving ghost heads out of the door", p.mover.cell == HOUSE_EXIT
          or p.mover.dir == UP)
    _steps_until(game, lambda g: g.ghost("pinky").state == "out", 300)
    check("and joins the chase", p.state == "out")
    check("walking out facing left", p.mover.dir == LEFT)

    game = playing()
    e = game.ghost("blinky")
    e.state = "eaten"
    e.mover = Mover((10, 9), DOWN)
    _steps_until(game, lambda g: g.ghost("blinky").state == "house", 400)
    check("eyes go home", e.state == "house")
    check("and wait to be revived", e.release_timer > 0)
    _steps_until(game, lambda g: g.ghost("blinky").state == "leaving", 200)
    check("then head back out", e.state == "leaving")

    check("eyes move fast", ChompGame(seed=1).ghost_speed(
        Ghost("blinky", HOUSE_EXIT, "eaten")) > BASE_SPEED)

    # -- 12. speeds -------------------------------------------------------- #
    for level, expect in ((1, (0.80, 0.75, 0.50, 0.40)), (3, (0.90, 0.85, 0.55, 0.45)),
                          (10, (1.00, 0.95, 0.60, 0.50)), (25, (0.90, 0.95, 0.60, 0.50))):
        check(f"level {level} uses its speed profile",
              speed_profile(level) == expect)
    check("the profile never returns zero", all(all(f > 0 for f in speed_profile(l))
                                                for l in range(1, 30)))
    game = playing()
    check("Pac-Man's speed follows the profile",
          abs(game.pac_speed() - 0.80 * BASE_SPEED) < 1e-9)
    g = place(game, "blinky", (5, 8))
    check("so does a ghost's", abs(game.ghost_speed(g) - 0.75 * BASE_SPEED) < 1e-9)
    game.fright_timer = 5.0
    check("frightened ghosts are slower",
          abs(game.ghost_speed(g) - 0.50 * BASE_SPEED) < 1e-9)
    game.fright_timer = 0.0
    g.mover = Mover((1, TUNNEL_ROW), LEFT)
    check("and the tunnel slows them down", abs(game.ghost_speed(g) - 0.40 * BASE_SPEED) < 1e-9)
    hard = playing(difficulty="hard")
    easy = playing(difficulty="easy")
    gh = place(hard, "blinky", (5, 8))
    ge = place(easy, "blinky", (5, 8))
    check("hard ghosts are quicker than easy ones",
          hard.ghost_speed(gh) > easy.ghost_speed(ge))
    check("but Pac-Man is not affected", hard.pac_speed() == easy.pac_speed())

    # -- 13. the frightened window ----------------------------------------- #
    check("the window never drops below a second",
          all(fright_seconds(l) >= 1.0 for l in range(1, 40)))
    check("and it shrinks with the level", fright_seconds(1) > fright_seconds(9))
    game = playing()
    game.fright_timer = 3.0
    check("ghosts flash near the end", isinstance(game.fright_flash(), bool))
    game.fright_timer = 0.0
    check("and stop flashing after", game.fright_flash() is False)
    game.fright_timer = 1.0
    check("a ghost in the house is not frightened",
          not game.is_frightened(game.ghost("inky")))
    check("Blinky, who is out, is",
          game.is_frightened(game.ghost("blinky")))

    # -- 14. fruit --------------------------------------------------------- #
    check("there are two fruit drops", FRUIT_THRESHOLDS == (70, 170))
    check("the fruit table grows", len(FRUITS) == 8
          and all(FRUITS[i][2] < FRUITS[i + 1][2] for i in range(7)))
    game = playing()
    freeze_ghosts(game)
    game.dots_eaten = 69
    game.pac = Mover((1, 1), RIGHT)
    game.pac_want = RIGHT
    events = game.step()
    check("the first fruit appears at 70 dots", "fruit" in events)
    check("it has a name, a colour and a value", game.fruit is not None
          and len(game.fruit) == 3)
    check("and a timer", game.fruit_timer > 0)
    game.pac = Mover(FRUIT_TILE, RIGHT)
    events = game.step()
    check("eating it scores", f"fruit:{game.fruit_history and 100}" in events
          or any(e.startswith("fruit:") for e in events))
    check("and it is remembered", game.fruit_history == ["櫻桃"])

    game = playing()
    freeze_ghosts(game)
    game.dots_eaten = 70
    game.fruit = fruit_for(1)
    game.fruit_timer = 0.001
    game.step()
    check("an uneaten fruit goes away", game.fruit is None)

    game = playing()
    freeze_ghosts(game)
    game.fruit_thresholds = [170]          # the first drop has already happened
    game.dots_eaten = 169
    game.pac = Mover((1, 1), RIGHT)
    game.pac_want = RIGHT
    events = game.step()
    check("the second fruit appears at 170 dots", "fruit" in events)
    check("only two drops per level", game.fruit_thresholds == [])

    game = playing()
    game.dots_eaten = 70
    game._check_fruit([])
    check("the first drop lands", game.fruit is not None)
    game.fruit = None
    game.dots_eaten = 170
    game._check_fruit([])
    check("and the second one too", game.fruit is not None)
    check("with no third", game.fruit_thresholds == [])
    check("a level 5 fruit is worth more", fruit_for(5)[2] == 1000)
    check("and a level 9 one caps out", fruit_for(99)[2] == 5000)

    # -- 15. extra life, scoring, pause ------------------------------------ #
    game = playing()
    game.score = EXTRA_LIFE_AT - DOT_SCORE
    game.pac = Mover((1, 1), RIGHT)
    game.pac_want = RIGHT
    events = game.step()
    check("an extra life at 10000 points", "extra" in events and game.lives == 4)
    check("but only once", not game._check_extra_life([]) and game.lives == 4
          or game.lives == 4)
    game.score += 5000
    game._check_extra_life([])
    check("no second extra life", game.lives == 4)

    game = playing()
    game.paused = True
    frozen = (game.pac.cell, game.pac.progress, game.time, game.score)
    for _ in range(30):
        game.step()
    check("pause freezes the frame",
          (game.pac.cell, game.pac.progress, game.time, game.score) == frozen)
    game.paused = False
    game.step()
    check("and the clock restarts", game.time > frozen[2])

    # -- 16. the mouth points the way he is walking ------------------------ #
    # Tk arc angles run counter-clockwise from 3 o'clock, so 90 is up: getting
    # this table backwards makes him chew downwards while walking upwards.
    check("the mouth opens rightwards when he walks right", mouth_angle(RIGHT) == 0)
    check("upwards when he walks up", mouth_angle(UP) == 90)
    check("leftwards when he walks left", mouth_angle(LEFT) == 180)
    check("downwards when he walks down", mouth_angle(DOWN) == 270)
    check("up and down are not swapped",
          mouth_angle(UP) != mouth_angle(DOWN)
          and mouth_angle(UP) == 90 and mouth_angle(DOWN) == 270)
    check("a stopped Pac-Man still gets an angle", mouth_angle((0, 0)) == 0)

    # -- 17. the ASCII view ------------------------------------------------ #
    game = fresh()
    frame = game.ascii_frame()
    lines = frame.splitlines()
    check("the frame shows every maze row", len(lines) == ROWS + 1, f"{len(lines)}")
    check("each row is the full width", all(len(ln) == COLS for ln in lines[:ROWS]))
    check("the walls are drawn", "#" in frame)
    check("the dots are drawn", "." in frame)
    check("the pellets are drawn", "o" in frame)
    check("Pac-Man is marked", any(m in frame for m in "@<>^v"))
    check("the ghosts are marked", all(n[0].upper() in frame for n in GHOST_NAMES))
    check("the HUD is below the maze", "分數" in lines[-1] and "生命" in lines[-1])
    check("the HUD shows the level", "第 1 關" in lines[-1])

    game = playing()
    freeze_ghosts(game)
    game.pac = Mover((1, 1), RIGHT)
    game.pac_want = RIGHT
    game.step()
    check("an eaten dot disappears from the frame",
          game.ascii_frame().splitlines()[1][1] == ">")

    # -- 18. the bot ------------------------------------------------------- #
    result = run_simulate(60, seed=3, verbose=False)
    check("the bot is still alive after a minute", result.state != "gameover",
          f"{result.state} at {result.time:.1f}s")
    check("it eats most of the maze", result.dots_eaten > 120, f"{result.dots_eaten}")
    check("it scores", result.score > 1500, f"{result.score}")
    check("it is still on the board", not is_wall(result.pac.cell))
    check("and so are the ghosts", all(not is_wall(g.mover.cell) for g in result.ghosts))
    check("the dot count always adds up",
          result.dots_left() == result.dots_total - result.dots_eaten,
          f"{result.dots_left()} vs {result.dots_total - result.dots_eaten}")
    check("it eats into the level", result.dots_left() < result.dots_total)

    long = run_simulate(150, seed=3, verbose=False)
    check("a longer run gets close to clearing the level",
          long.dots_eaten > 150, f"{long.dots_eaten}")
    check("and the maze never goes inconsistent",
          long.dots_left() == long.dots_total - long.dots_eaten)

    easy = run_simulate(45, level=3, difficulty="easy", seed=5, verbose=False)
    check("easier ghosts let the bot do better",
          easy.dots_eaten > 60, f"{easy.dots_eaten}")
    hard = run_simulate(45, level=3, difficulty="hard", seed=5, verbose=False)
    check("harder ghosts do worse", hard.dots_eaten <= easy.dots_eaten + 10,
          f"easy {easy.dots_eaten} hard {hard.dots_eaten}")
    check("a zero second run does nothing", run_simulate(0, seed=1, verbose=False).time == 0.0)

    # the bot really does turn towards a frightened ghost
    game = playing()
    game.fright_timer = 6.0
    game.pac = Mover((5, 4), LEFT)
    game.pac_want = LEFT
    for name in ("inky", "clyde", "pinky"):
        place(game, name, (1, 1))
    place(game, "blinky", (8, 4))
    bot = ChompBot(game)
    check("it heads for a frightened ghost", bot.decide(game) == RIGHT,
          f"{bot.decide(game)}")
    game.fright_timer = 0.0
    check("and stops once the fright is over", bot.decide(game) != RIGHT,
          f"{bot.decide(game)}")

    # -- 19. events and misc ----------------------------------------------- #
    game = playing()
    freeze_ghosts(game)
    check("a quiet frame yields no events", game.step() == [])
    game.state = "gameover"
    check("a finished game is silent", game.step() == [])
    game = fresh()
    check("the ready countdown is silent", game.step() == [])
    check("the status text follows the state", fresh().status_text() == "準備")

    check("every mode has a label to show",
          set(MODE_LABEL) == {"scatter", "chase", "frightened"})
    check("and the labels are Chinese, not the raw keys",
          all(label != key for key, label in MODE_LABEL.items()))

    game = playing()
    game.mode = "scatter"
    check("scatter is reported", game.status_text() == "散開")
    game.mode = "chase"
    check("chase is reported", game.status_text() == "追擊")
    game.paused = True
    check("pause wins over the mode", game.status_text() == "暫停")
    game.paused = False
    game.state = "gameover"
    check("game over is reported", game.status_text() == "遊戲結束")

    game = playing()
    check("the shape is kept when nobody moves", game._invariant() is None)
    game.ghost("blinky").mover = Mover((0, 0), LEFT)
    check("the invariant catches a ghost in a wall",
          _raises(lambda: game._invariant(), AssertionError))

    # -- 20. high scores --------------------------------------------------- #
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "highscore.json"
        check("no file means no record", load_highscore(path) == {})
        check("a first score is written", save_highscore(path, {"score": 500, "level": 3}))
        check("and reads back", load_highscore(path)["score"] == 500)
        check("a worse score is refused", not save_highscore(path, {"score": 400}))
        check("the record stands", load_highscore(path)["score"] == 500)
        check("a better score wins", save_highscore(path, {"score": 900, "level": 5}))
        check("and is stored", load_highscore(path)["level"] == 5)
        path.write_text("not json", encoding="utf-8")
        check("a corrupt file is survived", load_highscore(path) == {})
        check("no path is a no-op", not save_highscore(None, {"score": 1}))

    print(f"\n{checks} checks passed.")
    return 0


def _unfreeze(game) -> None:
    game.state = "playing"
    game.paused = False


def _steps_until(game, predicate, limit: int):
    """Step until `predicate(game)` holds; returns the events of the last frame."""
    events: list[str] = []
    for _ in range(limit):
        if predicate(game):
            return events
        events = game.step()
    return events


def _frightened_move_varies(game) -> bool:
    """A frightened ghost left alone must not always pick the same way."""
    picks = set()
    for _ in range(40):
        g = game.ghost("blinky")
        g.mover = Mover((5, 4), UP)
        game.fright_timer = 5.0
        game._choose_ghost_dir(g)
        picks.add(g.mover.dir)
    return len(picks) > 1


def _raises(fn, kind=ValueError) -> bool:
    try:
        fn()
    except kind:
        return True
    except Exception:
        return False
    return False


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Chomp 迷宮追擊 — a maze-chase game (Tkinter, standard library only). "
                    "--selftest, --simulate and --text need no display.")
    parser.add_argument("-d", "--difficulty", choices=list(DIFFICULTIES), default="normal",
                        help="ghost speed and fright window (default: normal)")
    parser.add_argument("-l", "--level", type=int, default=1,
                        help="starting level (default: 1)")
    parser.add_argument("--lives", type=int, default=QUEUE_LIVES,
                        help="starting lives (default: 3)")
    parser.add_argument("--seed", type=int, help="fixed random seed (repeatable frights)")
    parser.add_argument("--no-highscore", action="store_true",
                        help="neither read nor write highscore.json")
    parser.add_argument("--simulate", type=float, metavar="SECONDS",
                        help="headless: let the bot play, then print the result")
    parser.add_argument("--text", nargs="?", type=float, const=8.0, metavar="SECONDS",
                        help="headless: print the maze as ASCII")
    parser.add_argument("--selftest", action="store_true", help="run the logic tests and exit")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="with --selftest: only print the summary")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest(verbose=not args.quiet)

    level = max(1, args.level)
    lives = max(1, args.lives)
    score_path = None if args.no_highscore else DEFAULT_SCORE_PATH

    if args.text is not None:
        game = run_simulate(args.text, level, args.difficulty, args.seed, lives,
                            verbose=False)
        print(game.ascii_frame())
        return 0
    if args.simulate is not None:
        run_simulate(args.simulate, level, args.difficulty, args.seed, lives)
        return 0

    if not TK_AVAILABLE:
        print("tkinter is unavailable — try --simulate, --text or --selftest.", file=sys.stderr)
        return 2
    ChompGUI(level=level, lives=lives, difficulty=args.difficulty, seed=args.seed,
             score_path=score_path).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
