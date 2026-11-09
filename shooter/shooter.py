#!/usr/bin/env python3
"""Barrage — 彈幕突擊: a bullet-hell shooter in one file (Tkinter, standard library only).

GUI controls
    W A S D / 方向鍵 / 滑鼠    move (the cannon fires by itself)
    Shift                      focus: slower, tighter, shows the tiny hitbox
    Space / 左鍵               dash (brief invulnerability, ~1 s cooldown)
    X                          bomb: wipe the screen, damage everything, brief invulnerability
    P                          pause
    R / N / F2                 new run
    1 / 2 / 3                  easy / normal / hard (starts a new run)
    Esc                        quit

Other modes
    --selftest                 run the logic tests and exit
    --simulate SECONDS         headless: a dodging bot plays, then print the result
    --text [SECONDS]           headless: print an ASCII picture of one frame

`step()` is exactly one 1/60 s frame and every speed is in pixels per second, so the
logic layer never touches the display and the tests can drive it directly.
"""

from __future__ import annotations

import argparse
import inspect
import json
import math
import random
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

try:  # tkinter is optional: --selftest, --simulate and --text need no display
    import tkinter as tk

    TK_AVAILABLE = True
except ImportError:  # pragma: no cover
    TK_AVAILABLE = False

try:  # sound is optional too: shooter_audio.py is a sibling file, and silence is fine
    from shooter_audio import BELL_EVENTS, EVENT_SOUNDS, SoundBank
except ImportError:  # pragma: no cover - the game still plays, just quietly
    SoundBank = None
    EVENT_SOUNDS: dict = {}
    BELL_EVENTS = frozenset({"boss", "over", "bomb", "perfect"})


# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #

TAU = math.pi * 2
DT = 1.0 / 60.0

MOVE_SPEED = 300.0          # px/s, normal
FOCUS_SPEED = 132.0         # px/s while holding Shift
ACCEL = 2600.0              # px/s^2
DAMPING = 0.88              # velocity kept per frame when letting go
DASH_SPEED = 900.0
DASH_TIME = 0.15
DASH_COOLDOWN = 1.15
INVULN_HIT = 1.7
INVULN_SHIELD = 1.1
PLAYER_R = 4.6              # the hitbox is much smaller than the ship (as in every danmaku)
GRAZE_R = 22.0              # passing this close to a bullet scores

BOMB_START = 2               # screen-clearing bombs you begin with
MAX_BOMBS = 5
BOMB_DAMAGE = 14
BOMB_INVULN = 1.4
BOMB_SCORE_PER_BULLET = 8

SEEKER_TURN = 1.9            # rad/s a homing bullet can turn
ZONE_LENGTH = 5              # waves per background zone

SHOT_SPEED = 760.0
SHOT_R = 3.2
SHOT_COOLDOWN = {1: 0.130, 2: 0.120, 3: 0.105, 4: 0.100, 5: 0.092}
# power level -> ((x offset, angle in degrees, damage), ...)
WEAPONS = {
    1: ((0.0, 0.0, 1),),
    2: ((-7.0, 0.0, 1), (7.0, 0.0, 1)),
    3: ((-10.0, 0.0, 1), (10.0, 0.0, 1), (0.0, 0.0, 2)),
    4: ((-11.0, -7.0, 1), (0.0, 0.0, 2), (11.0, 7.0, 1)),
    5: ((-13.0, -13.0, 1), (-6.0, -5.0, 1), (0.0, 0.0, 3), (6.0, 5.0, 1), (13.0, 13.0, 1)),
}

ENEMY_SHOT = "#ff6b6b"      # slow orbs
NEEDLE_COLOR = "#ffd166"    # fast aimed needles
BOSS_SHOT = "#c084fc"
SEEKER_COLOR = "#4ade80"    # homing
BOUNCER_COLOR = "#38bdf8"   # bounces off the side walls
LASER_COLOR = "#f472b6"
PLAYER_COLOR = "#5ea1ff"
SHOT_COLOR = "#9fe8ff"

ENEMY_TYPES = {
    "drone":  {"hp": 3,   "r": 13.0, "score": 100,  "color": "#5c7cfa", "fire": 1.75, "vy": 112.0},
    "weaver": {"hp": 4,   "r": 13.0, "score": 150,  "color": "#f59e0b", "fire": 1.50, "vy": 134.0},
    "hunter": {"hp": 6,   "r": 15.0, "score": 220,  "color": "#8b5cf6", "fire": 1.10, "vy": 78.0},
    "turret": {"hp": 9,   "r": 19.0, "score": 260,  "color": "#e74c3c", "fire": 2.30, "vy": 74.0},
    "boss":   {"hp": 260, "r": 44.0, "score": 3000, "color": "#f43f5e", "fire": 1.35, "vy": 90.0},
    "splitter": {"hp": 10, "r": 18.0, "score": 280, "color": "#14b8a6", "fire": 2.10, "vy": 70.0},
    "mote":     {"hp": 1,  "r": 8.0,  "score": 60,  "color": "#2dd4bf", "fire": 99.0, "vy": 170.0},
    "sniper":   {"hp": 7,  "r": 16.0, "score": 240, "color": "#e879f9", "fire": 2.60, "vy": 66.0},
}

# A zone repaints the background and the HUD accent every five waves.
ZONES = ("深空", "星雲", "熔核", "極地")

# `bullet` scales enemy shot speed, `fire` multiplies the gap between shots
# (higher = shoots less often), `hp`/`count` scale enemy health and wave size.
DIFFICULTIES = {
    "easy":   {"bullet": 0.80, "fire": 1.30, "hp": 0.85, "count": 0.80, "drop": 1.20},
    "normal": {"bullet": 1.00, "fire": 1.00, "hp": 1.00, "count": 1.00, "drop": 1.00},
    "hard":   {"bullet": 1.25, "fire": 0.78, "hp": 1.25, "count": 1.25, "drop": 0.85},
}
DIFFICULTY_NAMES = {"easy": "簡單", "normal": "普通", "hard": "困難"}

POWERUP_KINDS = ("power", "shield", "heal", "score")
POWERUP_LABELS = {"power": "P", "shield": "S", "heal": "+", "score": "$"}
POWERUP_COLORS = {"power": "#5ea1ff", "shield": "#4ade80", "heal": "#f472b6", "score": "#fbbf24"}


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def circles_meet(x1: float, y1: float, r1: float, x2: float, y2: float, r2: float) -> bool:
    return (x1 - x2) ** 2 + (y1 - y2) ** 2 <= (r1 + r2) ** 2


# --------------------------------------------------------------------------- #
# Entities
# --------------------------------------------------------------------------- #


@dataclass
class Bullet:
    """A player shot."""

    x: float
    y: float
    vx: float
    vy: float
    r: float = SHOT_R
    dmg: int = 1


@dataclass
class EnemyBullet:
    x: float
    y: float
    vx: float
    vy: float
    r: float = 5.0
    color: str = ENEMY_SHOT
    kind: str = "orb"        # orb | needle | seeker | bouncer
    grazed: bool = False     # a bullet only pays out once
    life: float = 0.0        # seconds left; 0 means "until it leaves the screen"
    bounces: int = 0         # wall bounces left (bouncer)


@dataclass
class Enemy:
    kind: str
    x: float
    y: float
    hp: int
    max_hp: int
    r: float
    color: str
    score: float
    fire_interval: float
    vy: float
    fire_t: float = 0.0
    t: float = 0.0
    phase: float = 0.0
    hold_y: float = 0.0
    entered: bool = False
    flash: float = 0.0
    pattern: int = 0
    pattern_t: float = 0.0
    rot: float = 0.0
    aux_t: float = 0.0


@dataclass
class Laser:
    """A telegraphed beam: harmless while it warms up, lethal while it burns."""

    x: float
    y: float
    angle: float
    warm: float = 0.9
    life: float = 0.55
    r: float = 7.0
    sweep: float = 0.0       # rad/s rotation once it is on
    hit_cd: float = 0.0


@dataclass
class Powerup:
    x: float
    y: float
    kind: str
    vy: float = 70.0
    t: float = 0.0
    r: float = 10.0


@dataclass
class Particle:
    x: float
    y: float
    vx: float
    vy: float
    life: float
    max_life: float
    color: str
    size: float
    drag: float = 0.94


@dataclass
class Popup:
    """A little "+160" floating where an enemy died."""

    x: float
    y: float
    text: str
    color: str
    life: float = 0.8
    max_life: float = 0.8


@dataclass
class Player:
    x: float
    y: float
    hp: int = 3
    max_hp: int = 3
    shield: int = 0
    power: int = 1
    bombs: int = BOMB_START
    invuln: float = 0.0
    dash_t: float = 0.0
    dash_cd: float = 0.0
    fire_t: float = 0.0
    vx: float = 0.0
    vy: float = 0.0
    focus: bool = False
    facing: tuple[float, float] = (0.0, -1.0)
    r: float = PLAYER_R
    hit_flash: float = 0.0

    @property
    def invulnerable(self) -> bool:
        return self.invuln > 0.0


# --------------------------------------------------------------------------- #
# Game logic — no UI code in here, so it is fully testable.
# --------------------------------------------------------------------------- #


class ShooterGame:
    """Bullet hell: waves of enemies, graze for score, dash to survive."""

    WIDTH, HEIGHT = 720, 560
    PLAY_TOP = 30.0               # the HUD strip lives above this line
    BOSS_EVERY = 5                # every fifth wave is a boss

    MAX_ENEMY_BULLETS = 340
    MAX_PLAYER_BULLETS = 90
    MAX_PARTICLES = 170
    MAX_POPUPS = 12
    MAX_POWERUPS = 10

    COMBO_WINDOW = 1.6
    MAX_COMBO = 8
    REST_TIME = 1.8               # breather between waves

    def __init__(self, difficulty: str = "normal", seed: int | None = None,
                 lives: int = 3, score_path: Path | str | None = None,
                 bombs: int = BOMB_START):
        if difficulty not in DIFFICULTIES:
            raise ValueError(f"unknown difficulty: {difficulty!r}")
        self.difficulty = difficulty
        self.diff = DIFFICULTIES[difficulty]
        self.rng = random.Random(seed)
        self.seed = seed
        self.lives = lives
        self.bombs = bombs
        self.score_path = Path(score_path) if score_path else None
        self.runs: list[dict] = []
        self.best = self._load_scores()
        self.reset()

    # -- setup ------------------------------------------------------------- #

    def reset(self) -> None:
        """Fresh run. The best score survives a restart."""
        self.state = "menu"                     # menu | playing | over
        self.paused = False
        self.frame = 0
        self.time = 0.0

        self.score = 0
        self.wave = 0
        self.kills = 0
        self.grazes = 0
        self.shots = 0                  # shots fired / that connected, for the accuracy readout
        self.hits = 0
        self.damage_taken = 0
        self.perfect_waves = 0
        self.best_combo = 1
        self.bombs_used = 0
        self.wave_damage = 0            # hits taken during the current wave
        self.wave_total = 0
        self.wave_done = 0
        self._combo_called = 0
        self._recorded = False
        self.combo = 1
        self.combo_t = 0.0
        self.wave_t = 0.0
        self.rest_t = 0.0
        self.banner = ""
        self.banner_t = 0.0
        self.shake = 0.0
        self.events: list[str] = []

        self.player = Player(x=self.WIDTH / 2, y=self.HEIGHT - 74, hp=self.lives,
                             max_hp=self.lives, bombs=self.bombs)
        self.bullets: list[Bullet] = []
        self.enemy_bullets: list[EnemyBullet] = []
        self.enemies: list[Enemy] = []
        self.powerups: list[Powerup] = []
        self.particles: list[Particle] = []
        self.popups: list[Popup] = []
        self.lasers: list[Laser] = []
        self.spawn_queue: list[tuple[float, str, float]] = []
        self.boss: Enemy | None = None
        self.pointer: tuple[float, float] | None = None
        self.using_pointer = False          # mouse and keyboard both steer; last one wins

    def start(self) -> None:
        """Begin a fresh run from wave 1; the best score survives."""
        keep_best, keep_path = self.best, self.score_path
        seed = self.rng.random()
        self.reset()
        self.rng = random.Random(seed)
        self.best, self.score_path = keep_best, keep_path
        self.state = "playing"
        self._start_wave(1)

    @property
    def accuracy(self) -> float:
        return self.hits / self.shots if self.shots else 0.0

    @property
    def zone_index(self) -> int:
        """Every ZONE_LENGTH waves the background changes."""
        return (max(1, self.wave) - 1) // ZONE_LENGTH % len(ZONES)

    @property
    def zone(self) -> str:
        return ZONES[self.zone_index]

    @property
    def wave_progress(self) -> float:
        """0 at the start of a wave, 1 when every enemy of it is gone."""
        if not self.wave_total:
            return 0.0
        return clamp(self.wave_done / self.wave_total, 0.0, 1.0)

    @property
    def over(self) -> bool:
        return self.state == "over"

    @property
    def alive(self) -> bool:
        return self.state == "playing"

    # -- input helpers ----------------------------------------------------- #

    def move_to(self, x: float, y: float) -> None:
        """Mouse control: aim the ship at a point (it still obeys its top speed)."""
        self.pointer = (clamp(x, 18, self.WIDTH - 18), clamp(y, self.PLAY_TOP + 16, self.HEIGHT - 18))
        self.using_pointer = True

    def input_direction(self, dx: float, dy: float, focus: bool = False) -> None:
        """Keyboard control: a direction in -1..1 (it takes over from a mouse aim)."""
        if abs(dx) > 0.01 or abs(dy) > 0.01:
            self.using_pointer = False

    # -- wave building ----------------------------------------------------- #

    def _wave_plan(self, wave: int) -> list[tuple[float, str, float]]:
        """(time, enemy kind, x) for one wave — deterministic for a given rng."""
        if wave % self.BOSS_EVERY == 0:
            return [(0.8, "boss", self.WIDTH / 2)]

        count = max(3, int(min(4 + wave * 2, 20) * self.diff["count"]))
        pool = ["drone"]
        if wave >= 2:
            pool += ["weaver"]
        if wave >= 3:
            pool += ["hunter"]
        if wave >= 4:
            pool += ["turret"]
        if wave >= 5:
            pool += ["splitter"]
        if wave >= 6:
            pool += ["sniper"]

        formation = self.rng.choice(["row", "row", "vee", "random", "sides"])
        gap = max(0.22, 0.75 - wave * 0.030)
        plan: list[tuple[float, str, float]] = []
        for i in range(count):
            kind = self.rng.choice(pool)
            if i < 2 and wave <= 2:
                kind = "drone"                      # the opening wave stays gentle
            if formation == "row":
                x = self.WIDTH * (i + 1) / (count + 1)
            elif formation == "vee":
                x = self.WIDTH / 2 + (-1) ** i * (i // 2 + 1) * (self.WIDTH / (count + 4))
            elif formation == "sides":
                x = 70.0 if i % 2 == 0 else self.WIDTH - 70.0
            else:
                x = self.rng.uniform(60, self.WIDTH - 60)
            plan.append((0.25 + i * gap, kind, x))
        return plan

    def _start_wave(self, wave: int) -> None:
        previous_zone = self.zone_index if self.wave else -1
        self.wave = wave
        self.wave_t = 0.0
        self.wave_damage = 0
        self.spawn_queue = self._wave_plan(wave)
        self.wave_total = len(self.spawn_queue)
        self.wave_done = 0
        boss_wave = wave % self.BOSS_EVERY == 0
        label = f"BOSS  WAVE {wave}" if boss_wave else f"WAVE {wave}"
        if self.zone_index != previous_zone:
            label = f"{self.zone} · {label}"      # the palette just changed
        self.banner = label
        self.banner_t = 1.5
        self.events.append("boss" if boss_wave else "wave")

    # -- spawning ---------------------------------------------------------- #

    def spawn_enemy(self, kind: str, x: float, y: float | None = None) -> Enemy:
        spec = ENEMY_TYPES[kind]
        boss = kind == "boss"
        if boss:
            hp = (spec["hp"] + self.wave * 20) * self.diff["hp"]
        else:
            hp = spec["hp"] * self.diff["hp"] * (1 + (self.wave - 1) * 0.16)
        rng = self.rng
        enemy = Enemy(
            kind=kind,
            x=clamp(x, 30, self.WIDTH - 30),
            y=(self.PLAY_TOP + 8) if y is None else y,
            hp=max(1, round(hp)),
            max_hp=max(1, round(hp)),
            r=spec["r"],
            color=spec["color"],
            score=spec["score"],
            fire_interval=spec["fire"],
            vy=spec["vy"] * (1 + (self.wave - 1) * 0.03),
            fire_t=spec["fire"] * rng.uniform(0.55, 1.0),
            phase=rng.uniform(0, TAU),
            hold_y=rng.uniform(80, 170),
        )
        self.enemies.append(enemy)
        if boss:
            self.boss = enemy
        return enemy

    def _spawn_enemy_bullet(self, x: float, y: float, angle: float, speed: float,
                            color: str = ENEMY_SHOT, r: float = 5.0,
                            kind: str = "orb", life: float = 0.0,
                            bounces: int = 0) -> EnemyBullet | None:
        if len(self.enemy_bullets) >= self.MAX_ENEMY_BULLETS:
            return None
        speed *= self.diff["bullet"]
        bullet = EnemyBullet(
            x=x, y=y,
            vx=math.cos(angle) * speed, vy=math.sin(angle) * speed,
            r=r, color=color, kind=kind, life=life, bounces=bounces,
        )
        self.enemy_bullets.append(bullet)
        return bullet

    def _fire_aimed(self, enemy: Enemy, count: int, spread: float, speed: float,
                    color: str = ENEMY_SHOT, r: float = 5.0, kind: str = "orb") -> None:
        base = math.atan2(self.player.y - enemy.y, self.player.x - enemy.x)
        if count == 1:
            angles = [base]
        else:
            start = base - spread * (count - 1) / 2
            angles = [start + spread * i for i in range(count)]
        for angle in angles:
            self._spawn_enemy_bullet(enemy.x, enemy.y + enemy.r * 0.5, angle, speed, color, r, kind)

    def _fire_ring(self, enemy: Enemy, count: int, speed: float, offset: float,
                   color: str = ENEMY_SHOT, r: float = 5.5) -> None:
        for i in range(count):
            self._spawn_enemy_bullet(enemy.x, enemy.y, offset + i * TAU / count, speed, color, r)

    MAX_LASERS = 8

    def _aim_laser(self, enemy: Enemy, sweep: float = 0.0, warm: float = 0.9,
                   life: float = 0.55, r: float = 7.0) -> Laser | None:
        """Telegraph a beam straight at the player, then burn for `life` seconds."""
        if len(self.lasers) >= self.MAX_LASERS:
            return None
        angle = math.atan2(self.player.y - enemy.y, self.player.x - enemy.x)
        laser = Laser(x=enemy.x, y=enemy.y + enemy.r * 0.4, angle=angle,
                      warm=warm, life=life, r=r, sweep=sweep)
        self.lasers.append(laser)
        self.events.append("laser_warn")
        return laser

    def _ray_hits(self, laser: Laser) -> bool:
        """Is the ship inside the beam? (forward of the muzzle and within its width)"""
        player = self.player
        dx, dy = player.x - laser.x, player.y - laser.y
        forward = dx * math.cos(laser.angle) + dy * math.sin(laser.angle)
        if forward < 0:
            return False
        across = abs(-dx * math.sin(laser.angle) + dy * math.cos(laser.angle))
        return across <= laser.r + player.r

    def _step_lasers(self) -> None:
        for laser in list(self.lasers):
            if laser.warm > 0.0:
                laser.warm -= DT
                if laser.warm <= 0.0:
                    self.events.append("laser")
                    self.shake = max(self.shake, 7.0)
                continue
            laser.angle += laser.sweep * DT
            laser.life -= DT
            if laser.life <= 0.0:
                self.lasers.remove(laser)
                continue
            laser.hit_cd = max(0.0, laser.hit_cd - DT)
            if laser.hit_cd <= 0.0 and self._ray_hits(laser):
                if self.hurt_player():
                    laser.hit_cd = 0.6

    def _spawn_powerup(self, x: float, y: float, kind: str | None = None) -> Powerup | None:
        if len(self.powerups) >= self.MAX_POWERUPS:
            return None
        if kind is None:
            weights = [("power", 0.45), ("shield", 0.22), ("score", 0.20), ("heal", 0.13)]
            if self.player.hp >= self.player.max_hp:
                weights[3] = ("heal", 0.0)
            total = sum(w for _, w in weights)
            roll = self.rng.random() * total
            kind = "score"
            for name, weight in weights:
                roll -= weight
                if roll <= 0:
                    kind = name
                    break
        powerup = Powerup(x=x, y=y, kind=kind, t=self.rng.uniform(0, TAU))
        self.powerups.append(powerup)
        return powerup

    def _drop_from(self, enemy: Enemy) -> None:
        if enemy.kind == "boss":
            for kind in ("power", "shield", "score"):
                self._spawn_powerup(enemy.x + self.rng.uniform(-30, 30), enemy.y, kind)
        elif self.rng.random() < 0.16 * self.diff["drop"]:
            self._spawn_powerup(enemy.x, enemy.y)

    # -- effects ----------------------------------------------------------- #

    def _burst(self, x: float, y: float, color: str, count: int = 12,
               speed: float = 130.0, size: float = 3.0, life: float = 0.5) -> None:
        for _ in range(count):
            if len(self.particles) >= self.MAX_PARTICLES:
                return
            angle = self.rng.uniform(0, TAU)
            scale = self.rng.uniform(0.25, 1.0) * speed
            span = life * self.rng.uniform(0.6, 1.3)
            self.particles.append(Particle(
                x=x, y=y,
                vx=math.cos(angle) * scale, vy=math.sin(angle) * scale,
                life=span, max_life=span, color=color,
                size=size * self.rng.uniform(0.7, 1.4),
            ))

    def _spark(self, x: float, y: float, vx: float, vy: float, color: str,
               size: float = 2.0, life: float = 0.3) -> None:
        if len(self.particles) >= self.MAX_PARTICLES:
            return
        self.particles.append(Particle(x=x, y=y, vx=vx, vy=vy, life=life, max_life=life,
                                       color=color, size=size))

    def _popup(self, x: float, y: float, text: str, color: str) -> None:
        if len(self.popups) >= self.MAX_POPUPS:
            self.popups.pop(0)
        self.popups.append(Popup(x=x, y=y, text=text, color=color))

    # -- damage ------------------------------------------------------------ #

    def kill_enemy(self, enemy: Enemy) -> None:
        if enemy in self.enemies:
            self.enemies.remove(enemy)
        if self.combo_t > 0:
            self.combo = min(self.MAX_COMBO, self.combo + 1)
        else:
            self.combo = 1
        self.combo_t = self.COMBO_WINDOW
        gained = int(enemy.score * self.combo)
        self.score += gained
        self.kills += 1
        self.events.append("boom")
        self._popup(enemy.x, enemy.y, f"+{gained}", "#fbbf24" if self.combo > 1 else "#e8e8e8")
        self.best_combo = max(self.best_combo, self.combo)
        if self.combo in (4, 6, 8) and self.combo > self._combo_called:
            self._combo_called = self.combo
            self._popup(self.player.x, self.player.y - 34, f"COMBO x{self.combo}", "#fbbf24")
        if enemy.kind == "splitter":                    # it comes apart into shards
            for i in range(3):
                mote = self.spawn_enemy("mote", enemy.x + (i - 1) * 24.0, enemy.y)
                mote.fire_t = 99.0
        big = enemy.kind == "boss"
        self._burst(enemy.x, enemy.y, enemy.color, 34 if big else 14,
                    260.0 if big else 150.0, 5.0 if big else 3.0, 0.8 if big else 0.5)
        self._burst(enemy.x, enemy.y, "#ffffff", 8 if big else 3, 200.0, 2.5, 0.3)
        self.shake = max(self.shake, 14.0 if big else 4.0)
        self._drop_from(enemy)
        self.wave_done += 1
        if enemy is self.boss:
            self.boss = None
            self.score += 1000
            self.player.bombs = min(MAX_BOMBS, self.player.bombs + 1)   # a boss pays a bomb
            self._popup(self.WIDTH / 2, self.HEIGHT / 2, "BOSS DOWN  +1000", "#f43f5e")

    def hurt_player(self) -> bool:
        """Returns True when the hit actually landed (invulnerability blocks it)."""
        player = self.player
        if player.invuln > 0 or self.state != "playing":
            return False
        player.hit_flash = 0.25
        if player.shield > 0:
            player.shield -= 1
            player.invuln = INVULN_SHIELD
            self.events.append("shield")
            self.shake = max(self.shake, 6.0)
            self._burst(player.x, player.y, POWERUP_COLORS["shield"], 12, 150.0, 3.0, 0.4)
            return True
        player.hp -= 1
        player.invuln = INVULN_HIT
        self.damage_taken += 1
        self.wave_damage += 1
        player.power = max(1, player.power - 1)     # a hit costs one power level
        self.combo = 1
        self.combo_t = 0.0
        self.events.append("hurt")
        self.shake = max(self.shake, 16.0)
        self._burst(player.x, player.y, PLAYER_COLOR, 22, 220.0, 3.5, 0.6)
        if player.hp <= 0:
            self.game_over()
        return True

    def game_over(self) -> None:
        if self.state == "over":
            return
        self.state = "over"
        self.player.hp = 0
        self.banner = ""
        self.banner_t = 0.0
        self.events.append("over")
        self.shake = 24.0
        self._burst(self.player.x, self.player.y, PLAYER_COLOR, 40, 280.0, 4.0, 1.0)
        self.record_run()

    def record_run(self) -> bool:
        """Fold this run into the best score and the top-five table (once)."""
        if self._recorded or (self.score <= 0 and self.kills <= 0):
            return False
        self._recorded = True
        self.best = max(self.best, self.score)
        self.runs.append({
            "score": int(self.score), "wave": int(self.wave), "kills": int(self.kills),
            "accuracy": round(self.accuracy * 100), "difficulty": self.difficulty,
        })
        self.runs.sort(key=lambda run: -int(run.get("score", 0)))
        del self.runs[5:]
        self._save_scores()
        return True

    # -- powerups ---------------------------------------------------------- #

    def collect(self, powerup: Powerup) -> None:
        if powerup in self.powerups:
            self.powerups.remove(powerup)
        player = self.player
        kind = powerup.kind
        if kind == "power":
            if player.power >= 5:
                self.score += 500
                self._popup(powerup.x, powerup.y, "+500", POWERUP_COLORS["power"])
            else:
                player.power += 1
                self._popup(powerup.x, powerup.y, f"POWER {player.power}", POWERUP_COLORS["power"])
        elif kind == "shield":
            player.shield = min(2, player.shield + 1)
            self._popup(powerup.x, powerup.y, "SHIELD", POWERUP_COLORS["shield"])
        elif kind == "heal":
            player.hp = min(player.max_hp, player.hp + 1)
            self._popup(powerup.x, powerup.y, "+1 LIFE", POWERUP_COLORS["heal"])
        else:
            self.score += 400
            self._popup(powerup.x, powerup.y, "+400", POWERUP_COLORS["score"])
        self.events.append("pickup")
        self._burst(powerup.x, powerup.y, POWERUP_COLORS[kind], 8, 110.0, 2.5, 0.35)

    # -- the frame --------------------------------------------------------- #

    def step(self, dx: float = 0.0, dy: float = 0.0, dash: bool = False,
             focus: bool = False, bomb: bool = False) -> list[str]:
        """Advance exactly one 1/60 s frame; returns the events that happened."""
        self.events = []
        if self.state != "playing" or self.paused:
            return self.events

        self.frame += 1
        self.time += DT
        self.banner_t = max(0.0, self.banner_t - DT)
        self.shake = max(0.0, self.shake - 60.0 * DT)

        if bomb:
            self.drop_bomb()
        self._step_player(dx, dy, dash, focus)
        self._step_waves()
        self._step_enemies()
        self._step_bullets()
        self._step_lasers()
        self._step_powerups()
        self._step_effects()
        self._collide()

        if self.combo_t > 0.0:
            self.combo_t = max(0.0, self.combo_t - DT)
        if self.combo_t <= 0.0 and self.combo != 1:
            self.combo = 1
        return self.events

    def _step_player(self, dx: float, dy: float, dash: bool, focus: bool) -> None:
        player = self.player
        player.focus = focus
        player.invuln = max(0.0, player.invuln - DT)
        player.dash_cd = max(0.0, player.dash_cd - DT)
        player.hit_flash = max(0.0, player.hit_flash - DT)

        if self.using_pointer and self.pointer is not None:
            px, py = self.pointer
            ddx, ddy = px - player.x, py - player.y
            distance = math.hypot(ddx, ddy)
            if distance > 3.0:
                dx, dy = ddx / distance, ddy / distance
            else:
                dx = dy = 0.0

        length = math.hypot(dx, dy)
        if length > 1.0:
            dx, dy = dx / length, dy / length
            length = 1.0
        if length > 0.01:
            player.facing = (dx, dy)

        if dash and player.dash_cd <= 0.0 and player.dash_t <= 0.0:
            ux, uy = player.facing
            player.dash_t = DASH_TIME
            player.dash_cd = DASH_COOLDOWN
            player.invuln = max(player.invuln, DASH_TIME + 0.12)
            player.vx, player.vy = ux * DASH_SPEED, uy * DASH_SPEED
            self.events.append("dash")
            self._burst(player.x, player.y, SHOT_COLOR, 10, 150.0, 3.0, 0.35)

        if player.dash_t > 0.0:
            player.dash_t -= DT
            player.vx *= DAMPING ** 0.35
            player.vy *= DAMPING ** 0.35
        else:
            top = FOCUS_SPEED if focus else MOVE_SPEED
            player.vx += dx * ACCEL * DT
            player.vy += dy * ACCEL * DT
            speed = math.hypot(player.vx, player.vy)
            if speed > top:
                player.vx *= top / speed
                player.vy *= top / speed
            if length < 0.01:
                player.vx *= DAMPING
                player.vy *= DAMPING

        player.x = clamp(player.x + player.vx * DT, 18, self.WIDTH - 18)
        player.y = clamp(player.y + player.vy * DT, self.PLAY_TOP + 16, self.HEIGHT - 18)

        if self.frame % 2 == 0 and (length > 0.05 or player.dash_t > 0.0):
            fx, fy = player.facing             # a little exhaust behind the ship
            self._spark(player.x - fx * 13, player.y - fy * 13 + 4,
                        -fx * 30 + self.rng.uniform(-14, 14), -fy * 30 + self.rng.uniform(0, 30),
                        SHOT_COLOR, 1.6, 0.28)

        player.fire_t -= DT
        if player.fire_t <= 0.0:
            self._fire_player()
            player.fire_t = SHOT_COOLDOWN[player.power]

    def _fire_player(self) -> None:
        player = self.player
        if len(self.bullets) >= self.MAX_PLAYER_BULLETS:
            return
        for offset, degrees, dmg in WEAPONS[player.power]:
            if len(self.bullets) >= self.MAX_PLAYER_BULLETS:
                return
            radians = math.radians(degrees)
            self.bullets.append(Bullet(
                x=player.x + offset, y=player.y - 12,
                vx=math.sin(radians) * SHOT_SPEED, vy=-math.cos(radians) * SHOT_SPEED,
                dmg=dmg,
            ))
            self.shots += 1
        self.events.append("shoot")

    def drop_bomb(self) -> bool:
        """Clear the screen: enemy fire becomes score and every enemy takes a hit."""
        player = self.player
        if self.state != "playing" or player.bombs <= 0:
            return False
        player.bombs -= 1
        self.bombs_used += 1
        player.invuln = max(player.invuln, BOMB_INVULN)
        self.score += len(self.enemy_bullets) * BOMB_SCORE_PER_BULLET
        self.enemy_bullets.clear()
        self.lasers.clear()
        self.shake = max(self.shake, 20.0)
        for enemy in list(self.enemies):
            enemy.hp -= BOMB_DAMAGE
            enemy.flash = 0.12
            if enemy.hp <= 0:
                self.kill_enemy(enemy)
        self.events.append("bomb")
        self._burst(player.x, player.y, "#ffffff", 40, 320.0, 4.5, 0.9)
        self._burst(player.x, player.y, PLAYER_COLOR, 24, 220.0, 3.5, 1.1)
        return True

    def _step_waves(self) -> None:
        if self.rest_t > 0.0:
            self.rest_t -= DT
            if self.rest_t <= 0.0:
                self._start_wave(self.wave + 1)
            return

        self.wave_t += DT
        while self.spawn_queue and self.spawn_queue[0][0] <= self.wave_t:
            _, kind, x = self.spawn_queue.pop(0)
            self.spawn_enemy(kind, x)

        if not self.spawn_queue and not self.enemies and not self.over:
            bonus = 150 * self.wave
            self.score += bonus
            self._spawn_powerup(self.WIDTH / 2, self.PLAY_TOP + 46, "power")
            label = f"{self.zone}  WAVE {self.wave} CLEAR  +{bonus}"
            if self.wave_damage == 0:              # untouched: no-hit bonus
                self.perfect_waves += 1
                bonus = 400 * self.wave
                self.score += bonus
                label = f"PERFECT  WAVE {self.wave}  +{bonus}"
                self.events.append("perfect")
            if self.wave % 3 == 0:                 # a bomb every third wave
                self.player.bombs = min(MAX_BOMBS, self.player.bombs + 1)
                self._spawn_powerup(self.WIDTH / 2 - 60, self.PLAY_TOP + 46, "shield")
            self.banner = label
            self.banner_t = self.REST_TIME
            self.rest_t = self.REST_TIME
            self.events.append("clear")

    def _step_enemies(self) -> None:
        for enemy in list(self.enemies):
            enemy.t += DT
            enemy.flash = max(0.0, enemy.flash - DT)
            enemy.fire_t -= DT
            self._move_enemy(enemy)
            if enemy.y > self.HEIGHT + 60 or enemy.x < -90 or enemy.x > self.WIDTH + 90:
                if enemy in self.enemies:
                    self.enemies.remove(enemy)
                if enemy is self.boss:
                    self.boss = None

    def _move_enemy(self, enemy: Enemy) -> None:
        kind = enemy.kind
        if kind == "drone":
            enemy.y += enemy.vy * DT
            enemy.x += math.sin(enemy.t * 1.6 + enemy.phase) * 46.0 * DT
            if enemy.fire_t <= 0 and enemy.y > self.PLAY_TOP + 10:
                self._fire_aimed(enemy, 1, 0.0, 210.0)
                enemy.fire_t = enemy.fire_interval * self.diff["fire"]
        elif kind == "weaver":
            enemy.y += enemy.vy * DT
            enemy.x += math.sin(enemy.t * 2.2 + enemy.phase) * 130.0 * DT
            if enemy.fire_t <= 0 and enemy.y > self.PLAY_TOP + 10:
                enemy.pattern ^= 1                      # alternate the two attacks
                if enemy.pattern and self.wave >= 6:
                    for side in (-1.0, 1.0):            # ricochet rounds off the walls
                        self._spawn_enemy_bullet(enemy.x, enemy.y,
                                                 math.pi / 2 + side * 0.55, 210.0,
                                                 BOUNCER_COLOR, 5.0, "bouncer", bounces=1)
                else:
                    self._fire_aimed(enemy, 3, 0.30, 190.0)
                enemy.fire_t = enemy.fire_interval * self.diff["fire"]
        elif kind == "hunter":
            enemy.y += enemy.vy * DT
            chase = clamp(self.player.x - enemy.x, -95.0 * DT, 95.0 * DT)
            enemy.x += chase
            if enemy.fire_t <= 0 and enemy.y > self.PLAY_TOP + 10:
                self._fire_aimed(enemy, 1, 0.0, 330.0, NEEDLE_COLOR, 4.0, "needle")
                enemy.fire_t = enemy.fire_interval * self.diff["fire"]
        elif kind == "turret":
            if not enemy.entered:
                enemy.y += enemy.vy * DT
                if enemy.y >= enemy.hold_y:
                    enemy.entered = True
            else:
                enemy.y += math.sin(enemy.t * 0.9) * 16.0 * DT
                enemy.x += math.sin(enemy.t * 0.5 + enemy.phase) * 34.0 * DT
            if enemy.fire_t <= 0 and enemy.entered:
                enemy.rot += 0.26
                self._fire_ring(enemy, 12 + min(self.wave, 6), 135.0, enemy.rot)
                enemy.fire_t = enemy.fire_interval * self.diff["fire"]
        elif kind == "splitter":
            enemy.y += enemy.vy * DT
            enemy.x += math.sin(enemy.t * 1.1 + enemy.phase) * 62.0 * DT
            if enemy.fire_t <= 0 and enemy.y > self.PLAY_TOP + 10:
                self._fire_ring(enemy, 5, 150.0, enemy.phase + enemy.t, "#14b8a6", 5.0)
                enemy.fire_t = enemy.fire_interval * self.diff["fire"]
        elif kind == "mote":
            enemy.y += enemy.vy * DT            # a shard of a splitter: no guns, just a ram
            enemy.x += clamp(self.player.x - enemy.x, -160.0 * DT, 160.0 * DT)
        elif kind == "sniper":
            if not enemy.entered:
                enemy.y += enemy.vy * DT
                if enemy.y >= enemy.hold_y:
                    enemy.entered = True
            else:
                enemy.y += math.sin(enemy.t * 0.7 + enemy.phase) * 12.0 * DT
            if enemy.fire_t <= 0 and enemy.entered:
                self._aim_laser(enemy)
                enemy.fire_t = enemy.fire_interval * self.diff["fire"]
        elif kind == "boss":
            self._move_boss(enemy)
        enemy.x = clamp(enemy.x, 26, self.WIDTH - 26)

    def _move_boss(self, enemy: Enemy) -> None:
        if not enemy.entered:
            enemy.y += enemy.vy * DT
            if enemy.y >= 118.0:
                enemy.entered = True
            return
        enemy.y = 118.0 + math.sin(enemy.t * 0.8) * 16.0
        enemy.x = self.WIDTH / 2 + math.sin(enemy.t * 0.42) * (self.WIDTH / 2 - 130.0)

        enemy.aux_t -= DT
        enemy.pattern_t -= DT
        if enemy.pattern_t <= 0:
            enemy.pattern = (enemy.pattern + 1) % 4
            enemy.pattern_t = 3.4
            enemy.rot = 0.0
            enemy.fire_t = 0.25
            if enemy.pattern == 3:                  # the sweeping beam is telegraphed first
                self._aim_laser(enemy, sweep=self.rng.choice([-1.0, 1.0]) * 0.55,
                                warm=1.0, life=1.7, r=9.0)
                enemy.fire_t = 3.0

        if enemy.pattern in (1, 2) and enemy.aux_t <= 0:   # a slow homing round now and then
            self._spawn_enemy_bullet(enemy.x, enemy.y + enemy.r * 0.5,
                                     math.atan2(self.player.y - enemy.y,
                                                self.player.x - enemy.x),
                                     120.0, SEEKER_COLOR, 6.5, "seeker", life=4.0)
            enemy.aux_t = 1.5

        if enemy.fire_t > 0:
            return
        if enemy.pattern == 0:                      # rotating ring
            enemy.rot += 0.27
            self._fire_ring(enemy, 22, 140.0, enemy.rot, BOSS_SHOT, 5.5)
            enemy.fire_t = 1.35 * self.diff["fire"]
        elif enemy.pattern == 1:                    # aimed spread
            self._fire_aimed(enemy, 3, 0.34, 250.0, ENEMY_SHOT, 5.5)
            enemy.fire_t = 0.55 * self.diff["fire"]
        elif enemy.pattern == 2:                    # twin spiral arms
            for arm in (0.0, math.pi):
                self._spawn_enemy_bullet(enemy.x, enemy.y + enemy.r * 0.5,
                                         enemy.rot + arm, 170.0, BOSS_SHOT, 6.0)
            enemy.rot += 0.21
            enemy.fire_t = 0.08
        else:                                       # the beam is up: keep spraying slowly
            self._fire_aimed(enemy, 2, 0.5, 190.0, BOSS_SHOT, 5.0)
            enemy.fire_t = 0.9

    def _step_bullets(self) -> None:
        for bullet in self.bullets:
            bullet.x += bullet.vx * DT
            bullet.y += bullet.vy * DT
        self.bullets = [b for b in self.bullets
                        if -20 < b.x < self.WIDTH + 20 and -20 < b.y < self.HEIGHT + 20]

        kept: list[EnemyBullet] = []
        for bullet in self.enemy_bullets:
            if bullet.kind == "seeker":                 # home in, but only just
                current = math.atan2(bullet.vy, bullet.vx)
                wanted = math.atan2(self.player.y - bullet.y, self.player.x - bullet.x)
                delta = (wanted - current + math.pi) % TAU - math.pi
                angle = current + clamp(delta, -SEEKER_TURN * DT, SEEKER_TURN * DT)
                speed = math.hypot(bullet.vx, bullet.vy)
                bullet.vx, bullet.vy = math.cos(angle) * speed, math.sin(angle) * speed

            bullet.x += bullet.vx * DT
            bullet.y += bullet.vy * DT

            if bullet.kind == "bouncer":                # ricochet off the side walls
                if bullet.x < bullet.r and bullet.vx < 0:
                    bullet.x = bullet.r
                    bullet.vx = -bullet.vx
                    bullet.bounces -= 1
                    self._spark(bullet.x, bullet.y, 0.0, 0.0, bullet.color, 2.5, 0.2)
                elif bullet.x > self.WIDTH - bullet.r and bullet.vx > 0:
                    bullet.x = self.WIDTH - bullet.r
                    bullet.vx = -bullet.vx
                    bullet.bounces -= 1
                    self._spark(bullet.x, bullet.y, 0.0, 0.0, bullet.color, 2.5, 0.2)

            if bullet.life:
                bullet.life -= DT
                if bullet.life <= 0.0:
                    continue
            if bullet.bounces < 0:
                continue
            if -30 < bullet.x < self.WIDTH + 30 and -30 < bullet.y < self.HEIGHT + 30:
                kept.append(bullet)
        self.enemy_bullets = kept

    def _step_powerups(self) -> None:
        player = self.player
        for powerup in list(self.powerups):
            powerup.t += DT
            distance = math.hypot(player.x - powerup.x, player.y - powerup.y)
            if distance < 120.0:                    # magnet
                pull = 260.0 * DT
                if distance > 0.01:
                    powerup.x += (player.x - powerup.x) / distance * pull
                    powerup.y += (player.y - powerup.y) / distance * pull
            else:
                powerup.y += powerup.vy * DT
                powerup.x += math.sin(powerup.t * 2.0) * 26.0 * DT
            if powerup.y > self.HEIGHT + 30:
                self.powerups.remove(powerup)

    def _step_effects(self) -> None:
        for particle in self.particles:
            particle.x += particle.vx * DT
            particle.y += particle.vy * DT
            particle.vx *= particle.drag
            particle.vy *= particle.drag
            particle.life -= DT
        self.particles = [p for p in self.particles if p.life > 0]

        for popup in self.popups:
            popup.y -= 26.0 * DT
            popup.life -= DT
        self.popups = [p for p in self.popups if p.life > 0]

    def _collide(self) -> None:
        player = self.player

        for bullet in list(self.bullets):
            for enemy in self.enemies:
                if circles_meet(bullet.x, bullet.y, bullet.r, enemy.x, enemy.y, enemy.r):
                    enemy.hp -= bullet.dmg
                    enemy.flash = 0.10
                    self.hits += 1
                    if bullet in self.bullets:
                        self.bullets.remove(bullet)
                    self.events.append("hit")
                    if enemy.hp <= 0:
                        self.kill_enemy(enemy)
                    break

        for bullet in list(self.enemy_bullets):
            distance = math.hypot(bullet.x - player.x, bullet.y - player.y)
            if distance < bullet.r + player.r and player.invuln <= 0:
                if bullet in self.enemy_bullets:
                    self.enemy_bullets.remove(bullet)
                self.hurt_player()
            elif not bullet.grazed and distance < bullet.r + GRAZE_R:
                bullet.grazed = True
                self.grazes += 1
                self.score += 12 * self.combo
                player.dash_cd = max(0.0, player.dash_cd - 0.06)
                self.events.append("graze")
                self._burst(bullet.x, bullet.y, "#ffffff", 3, 70.0, 2.0, 0.25)

        for enemy in list(self.enemies):
            if circles_meet(enemy.x, enemy.y, enemy.r, player.x, player.y, player.r):
                if player.invuln <= 0:
                    self.hurt_player()
                enemy.hp -= 3
                enemy.flash = 0.10
                if enemy.hp <= 0:
                    self.kill_enemy(enemy)

        for powerup in list(self.powerups):
            if circles_meet(powerup.x, powerup.y, powerup.r, player.x, player.y, player.r + 8):
                self.collect(powerup)

    # -- readouts ---------------------------------------------------------- #

    def hud_text(self) -> str:
        player = self.player
        return (f"分數 {self.score}   最高 {self.best}   {self.zone} 第 {self.wave} 波   "
                f"生命 {max(0, player.hp)}/{player.max_hp}   護盾 {player.shield}   "
                f"炸彈 {player.bombs}   火力 {player.power}   連段 x{self.combo}   "
                f"擊破 {self.kills}   擦彈 {self.grazes}   命中率 {self.accuracy * 100:.0f}%")

    def ascii_frame(self, cols: int = 72, rows: int = 28) -> str:
        """A text picture of the current frame — the headless way to eyeball a layout."""
        grid = [[" "] * cols for _ in range(rows)]

        def put(x: float, y: float, char: str) -> None:
            col = int(x / self.WIDTH * (cols - 1))
            row = int(y / self.HEIGHT * (rows - 1))
            if 0 <= row < rows and 0 <= col < cols:
                grid[row][col] = char

        letters = {"drone": "d", "weaver": "w", "turret": "t", "hunter": "h", "boss": "B",
                   "splitter": "s", "mote": "m", "sniper": "n"}
        for enemy in self.enemies:
            put(enemy.x, enemy.y, letters.get(enemy.kind, "?"))
        for bullet in self.enemy_bullets:
            put(bullet.x, bullet.y, "*")
        for bullet in self.bullets:
            put(bullet.x, bullet.y, "|")
        for powerup in self.powerups:
            put(powerup.x, powerup.y, POWERUP_LABELS[powerup.kind])
        put(self.player.x, self.player.y, "@")

        hud = f"  #{self.frame}  {self.hud_text()}"
        body = "\n".join("  " + "".join(row) for row in grid)
        return (hud + "\n" + body + "\n"
                "  @ 你   d/w/t/h/B/s/m/n 敵人   * 敵彈   | 你的彈   P/S/+/$ 強化\n")

    # -- high score -------------------------------------------------------- #

    def _load_scores(self) -> int:
        """Read {"best": n, "runs": [...]} — an older file with only "best" still works."""
        self.runs = []
        if not self.score_path or not Path(self.score_path).exists():
            return 0
        try:
            data = json.loads(Path(self.score_path).read_text())
            best = int(data.get("best", 0) or 0)
            runs = data.get("runs") or []
        except (OSError, ValueError, AttributeError, TypeError):
            return 0
        self.runs = [run for run in runs if isinstance(run, dict)][:5]
        return best

    def _save_scores(self) -> None:
        if not self.score_path:
            return
        payload = {"best": int(self.best), "runs": self.runs}
        try:
            Path(self.score_path).write_text(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError:  # a read-only folder must never break the game
            pass


# --------------------------------------------------------------------------- #
# Headless bot — used by --simulate and by the self test
# --------------------------------------------------------------------------- #


def bot_control(game: ShooterGame) -> tuple[float, float, bool, bool, bool]:
    """A dodging policy: push away from incoming fire, line up on the nearest enemy.

    Returns (dx, dy, dash, focus, bomb) exactly like a human at the keyboard.
    """
    player = game.player
    dx = dy = 0.0

    for bullet in game.enemy_bullets:
        for lead, weight in ((0.0, 1.0), (0.11, 0.75), (0.24, 0.5)):
            bx = bullet.x + bullet.vx * lead
            by = bullet.y + bullet.vy * lead
            ox, oy = player.x - bx, player.y - by
            distance = math.hypot(ox, oy)
            if distance < 1e-6 or distance > 105.0:
                continue
            force = (105.0 - distance) / 105.0 * weight * 2.6
            dx += ox / distance * force
            dy += oy / distance * force

    for laser in game.lasers:                      # step out of a beam line
        lx, ly = math.cos(laser.angle), math.sin(laser.angle)
        ox, oy = player.x - laser.x, player.y - laser.y
        if ox * lx + oy * ly < 0:                  # the beam points the other way
            continue
        across = -ox * ly + oy * lx                # signed distance from the beam axis
        if abs(across) < 78.0:
            side = 1.0 if across >= 0 else -1.0
            urgency = (78.0 - abs(across)) / 78.0 * (2.4 if laser.warm > 0 else 3.6)
            dx += -ly * side * urgency
            dy += lx * side * urgency

    for enemy in game.enemies:
        ox, oy = player.x - enemy.x, player.y - enemy.y
        distance = math.hypot(ox, oy)
        if distance < 1e-6 or distance > 130.0:
            continue
        force = (130.0 - distance) / 130.0 * 3.0      # never let a hull land on us
        dx += ox / distance * force
        dy += oy / distance * force

    # line up under an enemy that is still well above us — the closest one in x
    overhead = [e for e in game.enemies if e.y < player.y - 140]
    if overhead:
        target = min(overhead, key=lambda e: abs(e.x - player.x))
        dx += clamp((target.x - player.x) / 70.0, -1.0, 1.0) * 1.1

    dy += clamp((game.HEIGHT - 96.0 - player.y) / 60.0, -1.0, 1.0) * 0.8
    if player.x < 52:
        dx += 0.8
    elif player.x > game.WIDTH - 52:
        dx -= 0.8
    if player.y < game.PLAY_TOP + 90:
        dy += 0.6

    dash = False
    if player.dash_cd <= 0:
        for bullet in game.enemy_bullets:
            if math.hypot(bullet.x - player.x, bullet.y - player.y) < 30.0:
                dash = True
                break

    # cornered? spend a bomb rather than a life
    bomb = False
    if player.bombs > 0 and player.dash_cd > 0.2:
        close = sum(1 for b in game.enemy_bullets
                    if math.hypot(b.x - player.x, b.y - player.y) < 68.0)
        bomb = close >= 5
    return dx, dy, dash, False, bomb


def run_simulate(seconds: float, difficulty: str = "normal", seed: int | None = None,
                 lives: int = 3, verbose: bool = True, score_path=None,
                 bombs: int = BOMB_START) -> ShooterGame:
    """Headless run with the built-in bot — used by --simulate and the self test."""
    game = ShooterGame(difficulty, seed, lives, score_path=score_path, bombs=bombs)
    game.start()
    frames = int(seconds * 60)
    next_report = 300
    for frame in range(frames):
        if game.over:
            break
        game.step(*bot_control(game))
        if verbose and frame >= next_report:
            next_report += 300
            print(f"  t={frame / 60:5.1f}s  第 {game.wave} 波  分數 {game.score:6d}  "
                  f"生命 {game.player.hp}  敵 {len(game.enemies):2d}  敵彈 {len(game.enemy_bullets):3d}")
    return game


# --------------------------------------------------------------------------- #
# Tkinter GUI
# --------------------------------------------------------------------------- #


class Pool:
    """Reuse Canvas items instead of creating them again every frame.

    Tk item creation dominates the frame cost (a 450-item delete-and-recreate frame
    measured ~4 ms; reusing hidden items brings that down to ~1.4 ms), and a bullet
    hell needs the headroom.
    """

    def __init__(self, canvas: "tk.Canvas"):
        self.canvas = canvas
        self.buckets: dict[tuple, list[int]] = {}
        self.used: dict[tuple, int] = {}
        self.hidden: set[int] = set()

    def take(self, key: tuple, factory) -> int:
        bucket = self.buckets.get(key)
        if bucket is None:
            bucket = self.buckets[key] = []
        index = self.used.get(key, 0)
        if index >= len(bucket):
            bucket.append(factory())
        item = bucket[index]
        self.used[key] = index + 1
        if item in self.hidden:
            self.canvas.itemconfigure(item, state="normal")
            self.hidden.discard(item)
        return item

    def end_frame(self) -> None:
        # Every bucket, not just the ones used this frame: a bucket that drops to
        # zero instances would otherwise leave its last items painted on screen.
        for key, bucket in self.buckets.items():
            for item in bucket[self.used.get(key, 0):]:
                if item not in self.hidden:
                    self.canvas.itemconfigure(item, state="hidden")
                    self.hidden.add(item)
        self.used.clear()

    def clear(self) -> None:
        self.buckets.clear()
        self.used.clear()
        self.hidden.clear()


class ShooterGUI:
    BG = "#0b0e14"
    HUD_BG = "#11141d"
    FG = "#e8e8e8"
    MUTED = "#8e92a7"
    ACCENT = "#5ea1ff"
    DANGER = "#ff6b6b"
    STAR_COLORS = ("#3a4256", "#5b6684", "#8b93ad")
    TICK_MS = 16  # ~60 frames per second

    # One palette per zone (see ZONES): background tint, stars, accent.
    ZONE_PALETTES = (
        ("#0b0e14", ("#3a4256", "#5b6684", "#8b93ad"), "#5ea1ff"),   # 深空
        ("#120c1c", ("#4b3a63", "#6d5292", "#a98cd8"), "#c084fc"),   # 星雲
        ("#170f0b", ("#5c3a2a", "#8a5230", "#d08a4a"), "#fb923c"),   # 熔核
        ("#0a1418", ("#2f4d55", "#3f6f7d", "#9fd8e4"), "#38bdf8"),   # 極地
    )

    def __init__(self, difficulty: str = "normal", seed: int | None = None,
                 lives: int = 3, score_path=None, sound: bool = False,
                 bombs: int = BOMB_START, volume: float = 0.6,
                 sound_backend: str = "auto"):
        self.difficulty = difficulty
        self.seed = seed
        self.lives = lives
        self.score_path = score_path
        self.sound = sound
        self.bombs = bombs
        self.sounds = None
        if sound and SoundBank is not None:
            self.sounds = SoundBank(volume=volume, backend=sound_backend)
            if self.sounds.available:
                print(f"sound: {self.sounds.backend_name} backend, volume {volume:g}")
            else:
                print(f"sound unavailable ({self.sounds.reason}) — the system bell it is",
                      file=sys.stderr)
        self.game = ShooterGame(difficulty, seed, lives, score_path=score_path, bombs=bombs)
        self.paused = False
        self.pressed: set[str] = set()
        self.release_tokens: dict[str, str] = {}
        self.flash = 0.0
        self.shake_rng = random.Random(1234)
        self.closed = False              # set once the window is gone: stop the loop
        self.after_id: str | None = None

        self.root = tk.Tk()
        self.root.title("Barrage — 彈幕突擊")
        self.root.resizable(False, False)
        self.root.configure(bg=self.BG)

        self.width, self.height = ShooterGame.WIDTH, ShooterGame.HEIGHT
        self.canvas = tk.Canvas(self.root, width=self.width, height=self.height,
                                bg=self.BG, highlightthickness=0)
        self.canvas.grid(row=0, column=0)
        self.pool = Pool(self.canvas)
        self.zone_index = -1            # forces the palette to be applied on frame 1
        self.stars = [
            [self.shake_rng.uniform(0, self.width), self.shake_rng.uniform(0, self.height),
             self.shake_rng.uniform(10, 40), self.shake_rng.randrange(3)]
            for _ in range(72)
        ]

        self.root.bind("<KeyPress>", self._on_key_press)
        self.root.bind("<KeyRelease>", self._on_key_release)
        self.root.bind("<F2>", lambda _e: self.new_run())
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.focus_set()

        self._draw()
        self._tick()

    # -- input ------------------------------------------------------------- #

    KEYS = {"w": (0, -1), "s": (0, 1), "a": (-1, 0), "d": (1, 0),
            "up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}

    def _on_key_press(self, event) -> None:
        key = event.keysym.lower()
        token = self.release_tokens.pop(key, None)
        if token is not None:  # macOS fires KeyRelease between auto-repeats
            self.root.after_cancel(token)
        if key in self.KEYS or key in {"shift_l", "shift_r"}:
            self.pressed.add(key)
            return
        if key == "space":
            self.dash_or_start()
        elif key == "x":
            self.pending_bomb = True
        elif key == "p":
            self.toggle_pause()
        elif key in {"r", "n"}:
            self.new_run()
        elif key in {"1", "2", "3"}:
            self.set_difficulty(("easy", "normal", "hard")[int(key) - 1])
        elif key == "escape":
            self.close()

    def _on_key_release(self, event) -> None:
        key = event.keysym.lower()
        if key in self.KEYS or key in {"shift_l", "shift_r"}:
            token = self.root.after(50, lambda k=key: self.pressed.discard(k))
            self.release_tokens[key] = token

    def _on_motion(self, event) -> None:
        self.game.move_to(event.x, event.y)

    def _on_click(self, _event) -> None:
        self.dash_or_start()

    # -- game flow --------------------------------------------------------- #

    def dash_or_start(self) -> None:
        game = self.game
        if game.state == "menu":
            game.start()
            self.paused = False
        elif game.over:
            self.new_run()
        else:
            self.pending_dash = True

    def new_run(self) -> None:
        self.game.start()
        self.paused = False
        self.pool.clear()
        self.canvas.delete("all")

    def toggle_pause(self) -> None:
        if self.game.state == "playing":
            self.paused = not self.paused

    def set_difficulty(self, difficulty: str) -> None:
        self.difficulty = difficulty
        self.game = ShooterGame(difficulty, self.seed, self.lives,
                                score_path=self.score_path, bombs=self.bombs)
        self.new_run()

    def _play(self, event: str) -> None:
        """The real effect when a backend is there, the system bell for the big moments."""
        if self.sounds is not None and self.sounds.event(event):
            return
        if event in BELL_EVENTS:
            self.root.bell()

    def close(self) -> None:
        """A run abandoned mid-flight still counts for the score table."""
        if self.closed:
            return
        self.closed = True
        if self.sounds is not None:
            self.sounds.close()
            self.sounds = None
        self.game.record_run()
        if self.after_id is not None:
            try:
                self.root.after_cancel(self.after_id)
            except tk.TclError:          # already torn down
                pass
            self.after_id = None
        self.root.destroy()

    def _direction(self) -> tuple[float, float, bool, bool, bool]:
        pressed = self.pressed
        dx = dy = 0.0
        for key, (kx, ky) in self.KEYS.items():
            if key in pressed:
                dx += kx
                dy += ky
        focus = "shift_l" in pressed or "shift_r" in pressed
        dash = getattr(self, "pending_dash", False)
        bomb = getattr(self, "pending_bomb", False)
        self.pending_dash = self.pending_bomb = False
        if dx or dy:
            self.game.input_direction(dx, dy, focus)
        return dx, dy, dash, focus, bomb

    # -- loop -------------------------------------------------------------- #

    def _tick(self) -> None:
        if self.closed:
            return
        try:
            if self.game.state == "playing" and not self.paused:
                dx, dy, dash, focus, bomb = self._direction()
                for event in self.game.step(dx, dy, dash, focus, bomb):
                    if event in {"hurt", "shield"}:
                        self.flash = 1.0
                    if self.sound:
                        self._play(event)
            self.pending_dash = self.pending_bomb = False
            self._draw()
        except tk.TclError:              # the window went away mid-frame
            self.closed = True
            return
        self.after_id = self.root.after(self.TICK_MS, self._tick)

    # -- drawing ----------------------------------------------------------- #

    def _shake(self) -> tuple[float, float]:
        magnitude = self.game.shake
        if magnitude <= 0.2:
            return 0.0, 0.0
        return (self.shake_rng.uniform(-magnitude, magnitude),
                self.shake_rng.uniform(-magnitude, magnitude))

    def _zone(self) -> tuple[str, tuple[str, str, str], str]:
        """(background, star colours, accent) for the zone the current wave is in."""
        return self.ZONE_PALETTES[self.game.zone_index]

    def _draw(self) -> None:
        canvas, game = self.canvas, self.game
        canvas.delete("hud")
        ox, oy = self._shake()
        if game.zone_index != self.zone_index:      # the palette changed: repaint the sky
            self.zone_index = game.zone_index
            self.pool.clear()
            canvas.delete("all")
            canvas.configure(bg=self._zone()[0])
        self._draw_stars()
        self._draw_pooled(ox, oy)
        self._draw_lasers(ox, oy)
        self._draw_enemies(ox, oy)
        self._draw_powerups(ox, oy)
        if game.state == "playing" and not self.paused:
            self._draw_player(ox, oy)
        self._draw_popups(ox, oy)
        self._draw_hud()
        self._draw_overlays()
        if self.flash > 0:
            self.flash = max(0.0, self.flash - 0.12)
            canvas.create_rectangle(2, 2, self.width - 2, self.height - 2,
                                    outline=self.DANGER, width=3, tags="hud")
        self.pool.end_frame()

    def _draw_stars(self) -> None:
        canvas = self.canvas
        colors = self._zone()[1]
        for star in self.stars:
            star[1] += star[2] / 60.0
            if star[1] > self.height:
                star[1] = 0.0
                star[0] = self.shake_rng.uniform(0, self.width)
            size = 1.0 + star[3]
            item = self.pool.take(("star", self.game.zone_index, star[3]),
                                  lambda s=size, c=colors[star[3]]: canvas.create_oval(
                                      0, 0, 0, 0, fill=c, outline=""))
            canvas.coords(item, star[0] - size, star[1] - size, star[0] + size, star[1] + size)

    def _draw_pooled(self, ox: float, oy: float) -> None:
        """Player shots, enemy shots and particles — the high-count layers."""
        canvas, game = self.canvas, self.game
        for bullet in game.bullets:
            item = self.pool.take(("shot", bullet.r),
                                  lambda r=bullet.r: canvas.create_oval(0, 0, 0, 0, fill=SHOT_COLOR,
                                                                        outline=""))
            canvas.coords(item, bullet.x + ox - bullet.r, bullet.y + oy - bullet.r,
                          bullet.x + ox + bullet.r, bullet.y + oy + bullet.r)
        for bullet in game.enemy_bullets:
            if bullet.kind == "needle":
                half_w, half_h = bullet.r * 0.5, bullet.r * 2.4
                item = self.pool.take(("needle", bullet.color),
                                      lambda c=bullet.color: canvas.create_rectangle(0, 0, 0, 0, fill=c,
                                                                                     outline=""))
                canvas.coords(item, bullet.x + ox - half_w, bullet.y + oy - half_h,
                              bullet.x + ox + half_w, bullet.y + oy + half_h)
            else:
                item = self.pool.take(("ball", bullet.color, bullet.r),
                                      lambda c=bullet.color, r=bullet.r: canvas.create_oval(
                                          0, 0, 0, 0, fill=c, outline=""))
                canvas.coords(item, bullet.x + ox - bullet.r, bullet.y + oy - bullet.r,
                              bullet.x + ox + bullet.r, bullet.y + oy + bullet.r)
        for particle in game.particles:
            size = particle.size * max(0.25, particle.life / particle.max_life)
            item = self.pool.take(("dot", particle.color, round(size, 1)),
                                  lambda c=particle.color, s=size: canvas.create_oval(
                                      0, 0, 0, 0, fill=c, outline=""))
            canvas.coords(item, particle.x - size, particle.y - size,
                          particle.x + size, particle.y + size)

    def _draw_lasers(self, ox: float, oy: float) -> None:
        """Thin dotted warning line while it warms up, thick beam while it burns."""
        canvas, game = self.canvas, self.game
        for laser in game.lasers:
            x, y = laser.x + ox, laser.y + oy
            dx, dy = math.cos(laser.angle), math.sin(laser.angle)
            far = 1400.0
            if laser.warm > 0.0:
                canvas.create_line(x, y, x + dx * far, y + dy * far, fill="#7a5c78",
                                   dash=(6, 6), tags="hud")
                canvas.create_oval(x - 9, y - 9, x + 9, y + 9, outline="#7a5c78", tags="hud")
            else:
                canvas.create_line(x, y, x + dx * far, y + dy * far, fill=LASER_COLOR,
                                   width=laser.r * 2, tags="hud")
                canvas.create_line(x, y, x + dx * far, y + dy * far, fill="#ffffff",
                                   width=max(1.0, laser.r * 0.7), tags="hud")

    def _draw_enemies(self, ox: float, oy: float) -> None:
        canvas, game = self.canvas, self.game
        for enemy in game.enemies:
            x, y, r = enemy.x + ox, enemy.y + oy, enemy.r
            color = "#ffffff" if enemy.flash > 0 else enemy.color
            if enemy.kind == "drone":
                canvas.create_polygon(x, y - r, x + r, y, x, y + r, x - r, y,
                                      fill=color, outline="", tags="hud")
            elif enemy.kind == "weaver":
                canvas.create_polygon(x - r, y - r * 0.7, x + r, y - r * 0.7, x, y + r,
                                      fill=color, outline="", tags="hud")
            elif enemy.kind == "hunter":
                canvas.create_polygon(x, y - r, x + r, y + r * 0.6, x, y + r * 0.2,
                                      x - r, y + r * 0.6, fill=color, outline="", tags="hud")
            elif enemy.kind == "turret":
                canvas.create_rectangle(x - r, y - r * 0.8, x + r, y + r * 0.8,
                                        fill=color, outline="", tags="hud")
                canvas.create_oval(x - r * 0.45, y - r * 0.45, x + r * 0.45, y + r * 0.45,
                                   fill=self.BG, outline=self.FG, tags="hud")
            elif enemy.kind == "splitter":
                canvas.create_polygon(x, y - r, x + r, y - r * 0.2, x + r * 0.6, y + r,
                                      x - r * 0.6, y + r, x - r, y - r * 0.2,
                                      fill=color, outline="#8ff0e0", tags="hud")
                canvas.create_oval(x - r * 0.34, y - r * 0.34, x + r * 0.34, y + r * 0.34,
                                   fill=self.BG, outline="#8ff0e0", tags="hud")
            elif enemy.kind == "mote":
                canvas.create_oval(x - r, y - r, x + r, y + r, fill=color, outline="",
                                   tags="hud")
                canvas.create_line(x, y - r, x, y - r * 2.4, fill=color, tags="hud")
            elif enemy.kind == "sniper":
                canvas.create_rectangle(x - r * 0.45, y - r, x + r * 0.45, y + r,
                                        fill=color, outline="", tags="hud")
                eye = "#ffffff" if enemy.fire_t < 0.35 else "#3b0a2e"
                canvas.create_oval(x - r * 0.4, y - r * 0.4, x + r * 0.4, y + r * 0.4,
                                   fill=eye, outline="", tags="hud")
                canvas.create_line(x - r, y + r * 0.9, x + r, y + r * 0.9, fill=color,
                                   width=2, tags="hud")
            else:  # boss
                canvas.create_polygon(x, y - r, x + r * 1.3, y, x, y + r, x - r * 1.3, y,
                                      fill=color, outline="#ffd7d7", width=2, tags="hud")
                canvas.create_oval(x - r * 0.4, y - r * 0.4, x + r * 0.4, y + r * 0.4,
                                   fill="#2b0713", outline="#ffd7d7", tags="hud")
            if enemy.max_hp > 3 and enemy.hp < enemy.max_hp:
                width = r * 2
                canvas.create_rectangle(x - r, y - r - 9, x + r, y - r - 5,
                                        fill="#2a2f3d", outline="", tags="hud")
                canvas.create_rectangle(x - r, y - r - 9, x - r + width * enemy.hp / enemy.max_hp,
                                        y - r - 5, fill=self.DANGER, outline="", tags="hud")

    def _draw_powerups(self, ox: float, oy: float) -> None:
        canvas, game = self.canvas, self.game
        for powerup in game.powerups:
            x, y = powerup.x + ox, powerup.y + oy
            color = POWERUP_COLORS[powerup.kind]
            pulse = 1.0 + 0.12 * math.sin(powerup.t * 6.0)
            r = powerup.r * pulse
            canvas.create_oval(x - r, y - r, x + r, y + r, outline=color, width=2, tags="hud")
            canvas.create_text(x, y, text=POWERUP_LABELS[powerup.kind], fill=color,
                               font=("Helvetica", 11, "bold"), tags="hud")

    def _draw_player(self, ox: float, oy: float) -> None:
        canvas, game = self.canvas, self.game
        player = game.player
        if player.invulnerable and int(player.invuln * 20) % 2 == 0:
            return  # blink while invulnerable
        x, y = player.x + ox, player.y + oy
        color = "#ffffff" if player.hit_flash > 0 else PLAYER_COLOR
        canvas.create_polygon(x, y - 16, x + 11, y + 10, x, y + 5, x - 11, y + 10,
                              fill=color, outline="#cfe4ff", tags="hud")
        canvas.create_oval(x - 3, y - 3, x + 3, y + 3, fill="#ffffff", outline="", tags="hud")
        if player.shield > 0:
            sr = 20 + 3 * math.sin(game.time * 8)
            canvas.create_oval(x - sr, y - sr, x + sr, y + sr, outline=POWERUP_COLORS["shield"],
                               width=2, tags="hud")
        if player.focus:  # the classic tiny hitbox marker
            canvas.create_oval(x - player.r, y - player.r, x + player.r, y + player.r,
                               outline=self.DANGER, width=1, tags="hud")

    def _draw_popups(self, ox: float, oy: float) -> None:
        canvas = self.canvas
        for popup in self.game.popups:
            canvas.create_text(popup.x + ox, popup.y + oy, text=popup.text, fill=popup.color,
                               font=("Helvetica", 11, "bold"), tags="hud")

    def _draw_hud(self) -> None:
        canvas, game = self.canvas, self.game
        player = game.player
        accent = self._zone()[2]
        canvas.create_rectangle(0, 0, self.width, ShooterGame.PLAY_TOP, fill=self.HUD_BG,
                                outline="", tags="hud")
        canvas.create_line(0, ShooterGame.PLAY_TOP, self.width, ShooterGame.PLAY_TOP,
                           fill="#1e2431", tags="hud")
        progress = game.wave_progress              # how much of this wave is gone
        canvas.create_rectangle(0, self.height - 3, self.width, self.height, fill="#161b26",
                                outline="", tags="hud")
        canvas.create_rectangle(0, self.height - 3, self.width * progress, self.height,
                                fill=accent, outline="", tags="hud")
        canvas.create_text(10, ShooterGame.PLAY_TOP / 2, anchor="w", fill=self.FG,
                           font=("Helvetica", 13, "bold"), text=f"分數 {game.score}", tags="hud")
        centre = f"{game.zone} · 第 {game.wave} 波   x{game.combo}" if game.wave else "彈幕突擊"
        canvas.create_text(self.width / 2, ShooterGame.PLAY_TOP / 2, fill=accent,
                           font=("Helvetica", 12, "bold"), text=centre, tags="hud")
        canvas.create_text(self.width - 10, ShooterGame.PLAY_TOP / 2, anchor="e",
                           fill=self.MUTED, font=("Helvetica", 11),
                           text=f"最高 {game.best}   火力 {player.power}   "
                                f"{DIFFICULTY_NAMES[self.difficulty]}", tags="hud")
        for i in range(player.max_hp):
            color = self.DANGER if i < player.hp else "#3a3f4d"
            canvas.create_text(16 + i * 16, ShooterGame.PLAY_TOP + 12, text="♥", fill=color,
                               font=("Helvetica", 12), tags="hud")
        for i in range(player.shield):
            canvas.create_oval(74 + i * 14, ShooterGame.PLAY_TOP + 7,
                               84 + i * 14, ShooterGame.PLAY_TOP + 17,
                               outline=POWERUP_COLORS["shield"], width=2, tags="hud")
        if player.bombs:
            canvas.create_text(112, ShooterGame.PLAY_TOP + 12, anchor="w", fill=accent,
                               font=("Helvetica", 10, "bold"),
                               text="X " + "◆" * player.bombs, tags="hud")
        ratio = 1.0 - clamp(player.dash_cd / DASH_COOLDOWN, 0.0, 1.0)
        canvas.create_rectangle(self.width - 96, ShooterGame.PLAY_TOP + 6, self.width - 16,
                                ShooterGame.PLAY_TOP + 16, outline="#3a3f4d", tags="hud")
        canvas.create_rectangle(self.width - 95, ShooterGame.PLAY_TOP + 7,
                                self.width - 95 + 78 * ratio, ShooterGame.PLAY_TOP + 15,
                                fill=accent if ratio >= 1.0 else "#38507d", outline="",
                                tags="hud")
        canvas.create_text(self.width - 100, ShooterGame.PLAY_TOP + 11, anchor="e", fill=self.MUTED,
                           font=("Helvetica", 9), text="衝刺", tags="hud")
        if game.boss is not None:
            boss = game.boss
            canvas.create_text(112, 25, anchor="e", fill=self.DANGER,
                               font=("Helvetica", 9, "bold"), text="BOSS", tags="hud")
            canvas.create_rectangle(118, 21, self.width - 118, 29, fill="#2a2f3d", outline="",
                                    tags="hud")
            width = (self.width - 236) * boss.hp / boss.max_hp
            canvas.create_rectangle(118, 21, 118 + width, 29, fill=self.DANGER, outline="",
                                    tags="hud")
        if game.banner_t > 0 and game.banner:
            half = 12 + len(game.banner) * 9
            canvas.create_rectangle(self.width / 2 - half, 106, self.width / 2 + half, 142,
                                    fill=self.HUD_BG, outline=accent, tags="hud")
            canvas.create_text(self.width / 2, 124, text=game.banner, fill=self.FG,
                               font=("Helvetica", 20, "bold"), tags="hud")

    def _draw_overlays(self) -> None:
        canvas, game = self.canvas, self.game
        if self.paused:
            canvas.create_text(self.width / 2, self.height / 2 - 10, text="暫停", fill=self.FG,
                               font=("Helvetica", 30, "bold"), tags="hud")
            canvas.create_text(self.width / 2, self.height / 2 + 30, text="按 P 繼續", fill=self.MUTED,
                               font=("Helvetica", 13), tags="hud")
            return
        if game.state == "menu":
            canvas.create_text(self.width / 2, 112, text="彈幕突擊", fill=self.FG,
                               font=("Helvetica", 38, "bold"), tags="hud")
            canvas.create_text(self.width / 2, 148, text="B A R R A G E", fill=self.ACCENT,
                               font=("Helvetica", 13, "bold"), tags="hud")
            lines = ("ＷＡＳＤ ／ 方向鍵 ／ 滑鼠　　移動（自動射擊）",
                     "Shift 集中　　Space ／ 左鍵 衝刺　　X 炸彈　　P 暫停　　R 重來",
                     f"難度：{DIFFICULTY_NAMES[self.difficulty]}（1 簡單 · 2 普通 · 3 困難）",
                     "擦彈得分、連段加倍、無傷清波有額外獎勵；每 5 波一隻頭目，每 5 波換一區。")
            for i, line in enumerate(lines):
                canvas.create_text(self.width / 2, 194 + i * 24, text=line, fill=self.MUTED,
                                   font=("Helvetica", 12), tags="hud")
            canvas.create_text(self.width / 2, 306, text="按 Space 或點擊畫面開始", fill=self.FG,
                               font=("Helvetica", 15, "bold"), tags="hud")
            self._draw_table(344)
        elif game.over:
            canvas.create_text(self.width / 2, 118, text="遊戲結束", fill=self.DANGER,
                               font=("Helvetica", 32, "bold"), tags="hud")
            canvas.create_text(self.width / 2, 160,
                               text=f"分數 {game.score}　·　最高 {game.best}", fill=self.FG,
                               font=("Helvetica", 18, "bold"), tags="hud")
            canvas.create_text(self.width / 2, 196,
                               text=f"第 {game.wave} 波（{game.zone}）· 擊破 {game.kills} · "
                                    f"擦彈 {game.grazes} · 命中率 {game.accuracy * 100:.0f}%",
                               fill=self.MUTED, font=("Helvetica", 12), tags="hud")
            canvas.create_text(self.width / 2, 220,
                               text=f"無傷清波 {game.perfect_waves} · 最高連段 x{game.best_combo} · "
                                    f"炸彈 {game.bombs_used} · 受傷 {game.damage_taken}",
                               fill=self.MUTED, font=("Helvetica", 12), tags="hud")
            self._draw_table(252)
            canvas.create_text(self.width / 2, 452, text="按 R 或點擊畫面重來", fill=self.FG,
                               font=("Helvetica", 14, "bold"), tags="hud")

    def _draw_table(self, top: float) -> None:
        """The five best runs on this machine."""
        canvas, game = self.canvas, self.game
        canvas.create_text(self.width / 2, top, text="最高分排行榜", fill=self.MUTED,
                           font=("Helvetica", 11, "bold"), tags="hud")
        if not game.runs:
            canvas.create_text(self.width / 2, top + 24, text="（還沒有紀錄）", fill=self.MUTED,
                               font=("Helvetica", 11), tags="hud")
            return
        for i, run in enumerate(game.runs[:5]):
            line = (f"{i + 1}.  {int(run.get('score', 0)):>7}   第 {int(run.get('wave', 0))} 波   "
                    f"擊破 {int(run.get('kills', 0))}   命中 {int(run.get('accuracy', 0))}%   "
                    f"{DIFFICULTY_NAMES.get(str(run.get('difficulty')), '?')}")
            canvas.create_text(self.width / 2, top + 26 + i * 20, text=line, fill=self.FG,
                               font=("Helvetica", 11), tags="hud")

    def run(self) -> None:
        self.root.mainloop()


# --------------------------------------------------------------------------- #
# Self test
# --------------------------------------------------------------------------- #


def selftest(verbose: bool = True) -> int:
    checks = 0

    def check(name: str, condition: bool) -> None:
        nonlocal checks
        checks += 1
        if not condition:
            raise AssertionError(name)
        if verbose:
            print(f"  ok  {name}")

    def fresh(difficulty: str = "normal", seed: int = 0, lives: int = 3) -> ShooterGame:
        game = ShooterGame(difficulty, seed=seed, lives=lives)
        game.start()
        return game

    def run_frames(game: ShooterGame, frames: int, dx: float = 0.0, dy: float = 0.0,
                   dash: bool = False, focus: bool = False) -> list[str]:
        events: list[str] = []
        for _ in range(frames):
            events += game.step(dx, dy, dash, focus)
            if game.over:
                break
        return events

    def immortal(game: ShooterGame) -> ShooterGame:
        """Plenty of lives, so a long test never ends early."""
        game.player.max_hp = 99
        game.player.hp = 99
        return game

    def arena(game: ShooterGame, kind: str = "drone", x: float | None = None,
              y: float = 60.0, hp: int = 99) -> Enemy:
        """One tough enemy alone on the field: it never dies, so it never drops loot."""
        game.enemies.clear()
        game.spawn_queue.clear()
        enemy = game.spawn_enemy(kind, game.WIDTH / 2 if x is None else x, y)
        enemy.hp = enemy.max_hp = hp
        return enemy

    def point_blank(game: ShooterGame, speed: float = 200.0) -> None:
        """An enemy bullet 20 px above the ship, falling straight onto it."""
        game._spawn_enemy_bullet(game.player.x, game.player.y - 20, math.pi / 2, speed)

    # -- opening state ----------------------------------------------------- #
    game = ShooterGame("normal", seed=0)
    check("waits in the menu until start",
          game.state == "menu" and game.wave == 0 and not game.enemies and not game.bullets)
    check("the hitbox is much smaller than the ship", 0 < PLAYER_R < 8)
    check("a step in the menu does nothing", game.step() == [] and game.frame == 0)

    game.start()
    check("start() opens wave 1", game.state == "playing" and game.wave == 1)
    check("waves are queued, not spawned all at once", bool(game.spawn_queue) and not game.enemies)
    check("the ship starts centred near the bottom",
          game.player.x == ShooterGame.WIDTH / 2 and game.player.y > ShooterGame.HEIGHT * 0.7)
    check("lives come from the constructor", game.player.hp == 3 and game.player.max_hp == 3)
    run_frames(game, 30)
    check("the first enemies arrive within half a second", bool(game.enemies))

    # -- the cannon -------------------------------------------------------- #
    game = fresh()
    events = game.step()
    check("the cannon fires by itself on the first frame", "shoot" in events and game.bullets)
    check("level 1 is a single shot", len(game.bullets) == 1)
    check("shots travel upwards", all(b.vy < 0 for b in game.bullets))
    run_frames(game, 7)
    check("the fire rate respects the cooldown", len(game.bullets) == 1)

    game = fresh()
    game.player.power = 5
    run_frames(game, 1)
    check("level 5 fires a five-way spread", len(game.bullets) == 5)
    check("the spread fans out in five directions",
          len({round(b.vx, 3) for b in game.bullets}) == 5)
    check("the middle shot of level 5 hits hardest", max(b.dmg for b in game.bullets) == 3)

    # -- movement ---------------------------------------------------------- #
    game = fresh()
    start_x = game.player.x
    run_frames(game, 30, dx=1.0)
    check("holding right moves the ship right", game.player.x > start_x + 20)

    game = fresh()
    game.move_to(100, 200)
    check("mouse aim is remembered", game.using_pointer and game.pointer == (100, 200))
    game.input_direction(1, 0)
    check("the keyboard takes over from a mouse aim", game.using_pointer is False)

    game = fresh()
    run_frames(game, 120, dx=-1.0, dy=1.0)
    check("the ship never leaves the play area",
          18 <= game.player.x <= ShooterGame.WIDTH - 18
          and ShooterGame.PLAY_TOP + 16 <= game.player.y <= ShooterGame.HEIGHT - 18)
    check("the walls clamp the ship", game.player.x <= 18.001)

    game = fresh()
    run_frames(game, 40, dx=1.0)
    fast = abs(game.player.vx)
    game = fresh()
    run_frames(game, 40, dx=1.0, focus=True)
    check("focus mode is slower than normal", abs(game.player.vx) < fast)
    check("focus mode raises the focus flag", game.player.focus is True)

    game = fresh()
    game.move_to(-500, 5000)
    check("mouse aim is clamped inside the play area",
          game.pointer is not None
          and 18 <= game.pointer[0] <= ShooterGame.WIDTH - 18
          and ShooterGame.PLAY_TOP <= game.pointer[1] <= ShooterGame.HEIGHT - 18)
    game.move_to(600, 300)
    run_frames(game, 60)
    check("the ship flies toward the mouse aim", abs(game.player.x - 600) < 40)

    # -- dash -------------------------------------------------------------- #
    game = fresh()
    events = run_frames(game, 1, dx=1.0, dash=True)
    check("dash reports itself", "dash" in events)
    check("dash grants invulnerability", game.player.invulnerable)
    check("dash goes on cooldown", game.player.dash_cd > 0.9)
    dash_x = game.player.x
    check("dash covers far more ground than one frame of flying",
          dash_x - ShooterGame.WIDTH / 2 > 8)
    cooldown = game.player.dash_cd
    run_frames(game, 1, dx=1.0, dash=True)
    check("dash cannot restart while it is still running", game.player.dash_cd < cooldown)

    # -- damage, shields, death -------------------------------------------- #
    game = fresh()
    enemy = arena(game, hp=1)
    game.bullets.append(Bullet(x=enemy.x, y=enemy.y, vx=0.0, vy=-50.0, dmg=1))
    events = run_frames(game, 2)
    check("a shot kills a one-hit enemy", "boom" in events and not game.enemies)
    check("a kill scores", game.score > 0 and game.kills == 1)
    check("a kill sets the combo to 1", game.combo == 1)
    check("a kill throws a score popup", bool(game.popups))

    game = fresh()
    enemy = arena(game, hp=99)
    game.bullets.append(Bullet(x=enemy.x, y=enemy.y, vx=0.0, vy=-50.0, dmg=3))
    run_frames(game, 2)
    check("shots damage a tough enemy without killing it",
          enemy.hp == 96 and enemy in game.enemies)

    game = fresh()
    arena(game)
    point_blank(game)
    run_frames(game, 12)
    check("an enemy bullet costs a life", game.player.hp == 2)
    check("a hit grants invulnerability", game.player.invulnerable)
    game.player.invuln = 0.0
    game.player.hp = 3
    point_blank(game)
    run_frames(game, 12)
    check("a second hit lands once the invulnerability ends", game.player.hp == 2)

    game = fresh()
    arena(game)
    game.player.shield = 1
    point_blank(game)
    run_frames(game, 12)
    check("a shield absorbs the hit instead of a life",
          game.player.hp == 3 and game.player.shield == 0)

    game = fresh()
    arena(game)
    game.player.power = 4
    point_blank(game)
    run_frames(game, 12)
    check("getting hit costs a power level", game.player.power == 3)

    game = fresh()
    arena(game)
    enemy = game.enemies[0]
    enemy.x, enemy.y = game.player.x, game.player.y - enemy.r * 0.5
    hp_before = enemy.hp
    run_frames(game, 6)
    check("flying into a hull costs a life", game.player.hp == 2)
    check("ramming also damages the enemy", enemy.hp < hp_before)

    game = fresh()
    game.player.hp = 1
    point_blank(game)
    run_frames(game, 12)
    check("the run ends when the lives run out", game.over)
    frame_at_death = game.frame
    game.step(dx=1.0)
    check("a finished run is frozen", game.state == "over" and game.frame == frame_at_death)
    check("the best score is kept", game.best >= game.score)

    # -- grazing ----------------------------------------------------------- #
    game = fresh()
    arena(game, x=660.0)
    game.player.x, game.player.y = 360.0, 400.0
    game._spawn_enemy_bullet(360 + PLAYER_R + 8, 400, 0.0, 0.0)
    run_frames(game, 2)
    check("a near miss is a graze, not a hit", game.grazes == 1 and game.player.hp == 3)
    check("grazing scores", game.score > 0)
    check("a bullet only grazes once", game.enemy_bullets[0].grazed is True)

    # -- powerups ---------------------------------------------------------- #
    game = fresh()
    game.player.power = 1
    game.collect(game._spawn_powerup(game.player.x, game.player.y, "power"))
    check("the power pickup raises the weapon level", game.player.power == 2)
    game.player.power = 5
    score_before = game.score
    game.collect(game._spawn_powerup(game.player.x, game.player.y, "power"))
    check("power at maximum converts into score",
          game.player.power == 5 and game.score > score_before)
    game.player.shield = 0
    game.collect(game._spawn_powerup(game.player.x, game.player.y, "shield"))
    check("the shield pickup grants a shield", game.player.shield == 1)
    game.player.hp = 2
    game.collect(game._spawn_powerup(game.player.x, game.player.y, "heal"))
    check("the heal pickup restores a life", game.player.hp == 3)
    game.collect(game._spawn_powerup(game.player.x, game.player.y, "heal"))
    check("healing never goes over the maximum", game.player.hp == game.player.max_hp)
    score_before = game.score
    game.collect(game._spawn_powerup(game.player.x, game.player.y, "score"))
    check("the score pickup pays out", game.score == score_before + 400)
    check("collected pickups leave the field", not game.powerups)
    game = fresh()
    pickup = game._spawn_powerup(game.player.x + 6, game.player.y, "power")
    run_frames(game, 2)
    check("flying into a pickup collects it",
          pickup not in game.powerups and game.player.power >= 2)

    # -- waves ------------------------------------------------------------- #
    game = fresh()
    plan = game.spawn_queue
    check("a wave is a timed plan", len(plan) >= 3 and plan[0][0] < plan[-1][0])
    check("a wave only uses known enemies",
          all(kind in ENEMY_TYPES for _, kind, _ in plan))
    check("the boss only appears on boss waves",
          all(kind != "boss" for _, kind, _ in plan))

    game = immortal(fresh(seed=4))
    game.enemies.clear()
    game.spawn_queue.clear()
    game.step()
    check("clearing a wave pays a bonus and starts a breather",
          game.rest_t > 0 and "clear" in game.events)
    run_frames(game, 200)
    check("the next wave follows the breather", game.wave == 2 and bool(game.enemies))

    game = immortal(fresh(seed=4))
    game.wave = 4
    game.enemies.clear()
    game.spawn_queue.clear()
    game.step()
    run_frames(game, 200)
    check("wave 5 is a boss wave",
          game.wave == 5 and any(e.kind == "boss" for e in game.enemies))
    check("the boss is tracked for the HUD bar", game.boss is not None and game.boss.hp > 100)
    boss = game.boss
    run_frames(game, 180)
    check("the boss flies in and opens fire", boss.entered and bool(game.enemy_bullets))
    check("enemy fire respects the bullet cap",
          len(game.enemy_bullets) <= ShooterGame.MAX_ENEMY_BULLETS)
    seen_boss_shot = False
    for _ in range(360):
        game.step()
        if any(b.color == BOSS_SHOT for b in game.enemy_bullets):
            seen_boss_shot = True
    check("the boss cycles through its patterns", seen_boss_shot)
    check("the boss is still standing after all that", game.boss is boss and boss.hp > 0)
    boss.hp = 1
    game.bullets.append(Bullet(x=boss.x, y=boss.y + 8, vx=0.0, vy=-200.0, dmg=1))
    run_frames(game, 2)
    check("a player shot can finish the boss", game.boss is None)
    check("killing the boss pays a bonus", game.score >= 3000)

    # -- difficulty -------------------------------------------------------- #
    check("there are three difficulty presets", set(DIFFICULTIES) == {"easy", "normal", "hard"})
    check("hard enemies are tougher",
          DIFFICULTIES["hard"]["hp"] > DIFFICULTIES["easy"]["hp"])
    check("hard enemies shoot more often",
          DIFFICULTIES["hard"]["fire"] < DIFFICULTIES["easy"]["fire"])
    easy = fresh("easy", seed=3)
    hard = fresh("hard", seed=3)
    easy._spawn_enemy_bullet(100.0, 100.0, 0.0, 100.0)
    hard._spawn_enemy_bullet(100.0, 100.0, 0.0, 100.0)
    check("hard bullets fly faster",
          abs(hard.enemy_bullets[0].vx) > abs(easy.enemy_bullets[0].vx))
    check("hard sends bigger waves", len(hard.spawn_queue) > len(easy.spawn_queue))
    try:
        ShooterGame("nightmare")
        check("an unknown difficulty is rejected", False)
    except ValueError:
        check("an unknown difficulty is rejected", True)

    # -- culling, caps and effects ----------------------------------------- #
    game = fresh()
    game.bullets.append(Bullet(x=10.0, y=-60.0, vx=0.0, vy=-10.0))
    game.enemy_bullets.append(EnemyBullet(x=-60.0, y=10.0, vx=-10.0, vy=0.0))
    run_frames(game, 2)
    check("bullets are culled off-screen",
          all(b.y > -20 for b in game.bullets) and all(b.x > -30 for b in game.enemy_bullets))
    game = fresh()
    for _ in range(ShooterGame.MAX_PLAYER_BULLETS + 40):
        game._fire_player()
    check("player shots are capped", len(game.bullets) <= ShooterGame.MAX_PLAYER_BULLETS)
    game = fresh()
    arena(game, hp=1)
    game.kill_enemy(game.enemies[0])
    check("a kill throws particles", bool(game.particles))
    check("a kill shakes the screen", game.shake > 0)
    for _ in range(40):
        game.kill_enemy(game.spawn_enemy("drone", 100.0, 50.0))
    check("particles stay capped", len(game.particles) <= ShooterGame.MAX_PARTICLES)
    check("score popups stay capped", len(game.popups) <= ShooterGame.MAX_POPUPS)

    # -- combo ------------------------------------------------------------- #
    game = fresh()
    arena(game, hp=1)
    game.kill_enemy(game.enemies[0])
    check("the first kill sets combo 1", game.combo == 1)
    game.kill_enemy(game.spawn_enemy("drone", 120.0, 50.0))
    check("a quick second kill doubles the multiplier", game.combo == 2)
    game.combo_t = 0.0
    run_frames(game, 2)
    check("the combo resets once the window closes", game.combo == 1)
    for i in range(ShooterGame.MAX_COMBO + 4):
        game.kill_enemy(game.spawn_enemy("drone", 200.0 + i, 50.0))
    check("the multiplier is capped", game.combo == ShooterGame.MAX_COMBO)

    # -- pause and restart ------------------------------------------------- #
    game = fresh()
    run_frames(game, 30)
    frame_before = game.frame
    game.paused = True
    game.step(dx=1.0)
    check("pause freezes the world", game.frame == frame_before)
    game.paused = False
    game.score = 4321
    game.best = 99
    game.start()
    check("a new run wipes score, wave and enemies",
          game.score == 0 and game.wave == 1 and game.state == "playing")
    check("a new run keeps the best score", game.best == 99)

    # -- high score file --------------------------------------------------- #
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "highscore.json"
        game = ShooterGame("normal", seed=1, score_path=path)
        game.start()
        game.score = 777
        game.game_over()
        check("a finished run writes the high score", path.exists() and game.best == 777)
        check("a fresh session reads it back", ShooterGame("normal", 1, 3, path).best == 777)
        game = ShooterGame("normal", seed=1, score_path=path)
        game.start()
        game.score = 12
        game.game_over()
        check("a worse run does not overwrite it", game.best == 777)

    # -- headless bot ------------------------------------------------------ #
    result = run_simulate(60.0, "easy", seed=5, verbose=False)
    check("the bot survives a minute on easy", not result.over)
    check("the bot kills things", result.kills >= 5)
    check("the bot clears waves", result.wave >= 2)
    check("the bot scores", result.score > 0)
    check("the bot keeps a hit rate", 0.0 < result.accuracy <= 1.0)
    check("the bot stays inside the play area",
          18 <= result.player.x <= ShooterGame.WIDTH - 18
          and result.player.y >= ShooterGame.PLAY_TOP)

    # -- the canvas item pool (runs without a display) ---------------------- #
    class FakeCanvas:
        """Enough Canvas for Pool: item ids, kwargs and a state flag."""

        def __init__(self):
            self.items: dict[int, dict] = {}
            self._next = 1

        def __getattr__(self, name):
            if name.startswith("create_"):
                def make(*_args, **kwargs):
                    item = self._next
                    self._next += 1
                    self.items[item] = dict(kwargs, state="normal")
                    return item
                return make
            raise AttributeError(name)

        def itemconfigure(self, item, **kwargs):
            self.items[item].update(kwargs)

    fake = FakeCanvas()
    pool = Pool(fake)
    factory = lambda: fake.create_oval(0, 0, 0, 0, fill="#fff")  # noqa: E731
    item = pool.take(("oval", "#fff"), factory)
    pool.end_frame()
    check("an item drawn this frame stays visible", fake.items[item]["state"] == "normal")
    pool.end_frame()
    check("a bucket that goes unused is hidden (the ghost-item fix)",
          fake.items[item]["state"] == "hidden")
    check("the hidden item is handed out again instead of recreated",
          pool.take(("oval", "#fff"), factory) == item
          and fake.items[item]["state"] == "normal")
    check("reuse does not grow the pool", len(fake.items) == 1)
    pool.end_frame()
    pool.end_frame()
    check("hiding an already hidden item is harmless", fake.items[item]["state"] == "hidden")
    pool.take(("oval", "#000"), factory)
    pool.end_frame()
    check("a second bucket owns its own items",
          len(fake.items) == 2 and fake.items[2]["state"] == "normal")
    pool.end_frame()
    check("both buckets hide once nothing is drawn",
          fake.items[1]["state"] == "hidden" and fake.items[2]["state"] == "hidden")
    pool.clear()
    check("clear() forgets every bucket", not pool.buckets and not pool.hidden)

    # -- the roster -------------------------------------------------------- #
    check("the enemy roster has eight kinds",
          set(ENEMY_TYPES) == {"drone", "weaver", "hunter", "turret", "boss",
                               "splitter", "mote", "sniper"})
    check("a mote carries no gun", ENEMY_TYPES["mote"]["fire"] > 5)
    early = ShooterGame("normal", seed=3)
    late = ShooterGame("normal", seed=3)
    late.wave = 6
    check("early waves keep the simple roster",
          all(kind in {"drone", "weaver", "hunter", "turret"}
              for _, kind, _ in early.spawn_queue))
    check("late waves draw from the bigger roster",
          bool({"splitter", "sniper"} & {kind for _, kind, _ in late._wave_plan(7)}))

    # -- splitters and motes ----------------------------------------------- #
    game = fresh()
    game.enemies.clear()
    game.spawn_queue.clear()
    splitter = game.spawn_enemy("splitter", 300.0, 120.0)
    splitter.hp = 1
    game.bullets.append(Bullet(x=splitter.x, y=splitter.y, vx=0.0, vy=-50.0, dmg=1))
    run_frames(game, 2)
    motes = [e for e in game.enemies if e.kind == "mote"]
    check("a splitter comes apart into three motes", len(motes) == 3)
    check("motes are one-hit shards", all(m.hp == 1 for m in motes))
    mote = motes[0]
    mote.x = 60.0
    run_frames(game, 30)
    check("motes chase the ship sideways", mote.x > 60.0)
    check("motes never shoot", not any(b.color == ENEMY_SHOT for b in game.enemy_bullets))

    # -- snipers and beams -------------------------------------------------- #
    game = fresh()
    game.enemies.clear()
    game.spawn_queue.clear()
    sniper = game.spawn_enemy("sniper", 200.0, 90.0)
    sniper.entered = True
    sniper.fire_t = 0.0
    game.step()
    check("a sniper telegraphs a beam", len(game.lasers) == 1)
    laser = game.lasers[0]
    check("the beam points at the ship",
          abs(laser.angle - math.atan2(game.player.y - sniper.y,
                                       game.player.x - sniper.x)) < 1e-9)
    hp_before = game.player.hp
    run_frames(game, 30)
    check("a warming beam is harmless", game.player.hp == hp_before and laser.warm > 0.0)
    run_frames(game, 40)
    check("a lit beam burns", game.player.hp < hp_before)
    run_frames(game, 30)
    check("a beam stops after its life", laser not in game.lasers)
    game.player.x, game.player.y = 600.0, 480.0
    check("a beam that points away from the ship cannot hit",
          not game._ray_hits(Laser(x=600.0, y=100.0, angle=-math.pi / 2)))
    check("a beam aimed at the ship does hit",
          game._ray_hits(Laser(x=600.0, y=100.0, angle=math.pi / 2)))
    game.lasers.clear()
    check("beams are capped",
          all(game._aim_laser(sniper) is not None for _ in range(ShooterGame.MAX_LASERS))
          and game._aim_laser(sniper) is None)
    game = fresh()
    game.enemies.clear()
    game.spawn_queue.clear()
    game.lasers.clear()
    sweep = Laser(x=360.0, y=100.0, angle=0.0, warm=0.0, life=2.0, sweep=0.5)
    game.lasers.append(sweep)
    run_frames(game, 30)
    check("a sweeping beam keeps turning", sweep.angle > 0.2 and sweep.warm <= 0.0)

    # -- homing and ricochet rounds ---------------------------------------- #
    def angle_error(bullet) -> float:
        wanted = math.atan2(game.player.y - bullet.y, game.player.x - bullet.x)
        current = math.atan2(bullet.vy, bullet.vx)
        return abs((wanted - current + math.pi) % TAU - math.pi)

    game = fresh()
    game.enemies.clear()
    game.spawn_queue.clear()
    game.player.x, game.player.y = 360.0, 480.0
    seeker = game._spawn_enemy_bullet(100.0, 100.0, 0.0, 140.0, SEEKER_COLOR, 6.5,
                                      "seeker", life=1.0)
    initial = angle_error(seeker)
    run_frames(game, 20)
    check("a seeker homes in on the ship", angle_error(seeker) < initial)
    check("homing does not change its speed",
          abs(math.hypot(seeker.vx, seeker.vy) - 140.0 * game.diff["bullet"]) < 1e-6)
    run_frames(game, 60)
    check("a seeker expires when its life runs out", seeker not in game.enemy_bullets)
    check("seekers are colour-coded", SEEKER_COLOR not in (ENEMY_SHOT, NEEDLE_COLOR, BOSS_SHOT))

    game = fresh()
    game.enemies.clear()
    game.spawn_queue.clear()
    bouncer = game._spawn_enemy_bullet(game.WIDTH - 40.0, 200.0, 0.0, 300.0, BOUNCER_COLOR,
                                       5.0, "bouncer", bounces=1)
    run_frames(game, 30)
    check("a ricochet turns around at the wall", bouncer.vx < 0 and bouncer.bounces < 1)
    check("a ricochet stays on screen", 0.0 <= bouncer.x <= ShooterGame.WIDTH)
    run_frames(game, 200)
    check("a ricochet dies once its bounces are spent", bouncer not in game.enemy_bullets)

    # -- bombs -------------------------------------------------------------- #
    game = fresh()
    game.enemies.clear()
    game.spawn_queue.clear()
    game.player.bombs = 2
    for i in range(5):
        game._spawn_enemy_bullet(100.0 + i * 20.0, 200.0, math.pi / 2, 100.0)
    tank = game.spawn_enemy("drone", 300.0, 100.0)
    tank.hp = tank.max_hp = 99
    fragile = game.spawn_enemy("drone", 400.0, 100.0)
    fragile.hp = 3
    game._aim_laser(tank, warm=0.0)
    score_before = game.score
    events = game.step(bomb=True)
    check("a bomb announces itself", "bomb" in events)
    check("a bomb wipes every enemy bullet", not game.enemy_bullets)
    check("a bomb wipes beams too", not game.lasers)
    check("each wiped bullet pays score", game.score >= score_before + 5 * BOMB_SCORE_PER_BULLET)
    check("a bomb spends a charge", game.player.bombs == 1)
    check("a bomb grants invulnerability", game.player.invulnerable)
    check("a bomb hurts every enemy", tank.hp == 99 - BOMB_DAMAGE)
    check("a bomb finishes a weakened enemy", fragile not in game.enemies)

    game = fresh()
    game.player.bombs = 0
    events = game.step(bomb=True)
    check("a bomb with no charges does nothing",
          "bomb" not in events and game.player.bombs == 0)
    check("a fresh run starts with the configured bombs",
          ShooterGame("normal", seed=1).player.bombs == BOMB_START)
    generous = ShooterGame("normal", seed=1, bombs=4)
    generous.start()
    check("a restart keeps the configured bomb count",
          generous.player.bombs == 4 == generous.bombs)
    check("the headless runner passes bombs through",
          run_simulate(0.05, "easy", seed=1, verbose=False, bombs=3).bombs == 3)
    check("the bot reports five controls, exactly like the keyboard",
          len(bot_control(ShooterGame("normal", seed=1))) == 5)

    game = fresh()
    game.player.bombs = 0
    game.kill_enemy(game.spawn_enemy("boss", 300.0, 100.0))
    check("a boss pays out a bomb", game.player.bombs == 1 and game.boss is None)
    for _ in range(8):
        game.kill_enemy(game.spawn_enemy("boss", 300.0, 100.0))
    check("bombs stop at the maximum", game.player.bombs == MAX_BOMBS)

    game = immortal(fresh(seed=4))
    game.player.bombs = 0
    game.wave = 3
    game.enemies.clear()
    game.spawn_queue.clear()
    game.step()
    check("every third cleared wave hands out a bomb", game.player.bombs == 1)

    # -- statistics --------------------------------------------------------- #
    game = fresh()
    game.player.power = 5
    game.step()
    check("every shot is counted off", game.shots == 5)
    enemy = arena(game, hp=99)
    game.bullets.append(Bullet(x=enemy.x, y=enemy.y, vx=0.0, vy=-50.0, dmg=1))
    run_frames(game, 2)
    check("every hit is counted", game.hits == 1)
    check("accuracy is hits over shots", 0.0 < game.accuracy < 1.0)

    game = fresh()
    game.enemies.clear()
    game.spawn_queue.clear()
    for i in range(4):
        game.kill_enemy(game.spawn_enemy("drone", 100.0 + i * 20.0, 60.0))
    check("the run remembers its best combo", game.best_combo == 4)

    game = immortal(fresh(seed=4))
    game.enemies.clear()
    game.spawn_queue.clear()
    game.step()
    check("clearing a wave untouched is a perfect wave",
          "perfect" in game.events and game.perfect_waves == 1)
    check("a perfect wave pays extra", game.score >= 400)
    game = fresh()
    game.wave_damage = 1
    game.enemies.clear()
    game.spawn_queue.clear()
    game.step()
    check("a wave where you were hit is not perfect", game.perfect_waves == 0)

    game = fresh()
    check("wave progress starts near zero", game.wave_progress < 0.5)
    game.wave_done = game.wave_total
    check("wave progress reaches one", game.wave_progress == 1.0)

    # -- zones -------------------------------------------------------------- #
    game = ShooterGame("normal", seed=0)
    game.wave = 1
    check("the first five waves are the first zone",
          game.zone_index == 0 and game.zone == ZONES[0])
    game.wave = 6
    check("the palette changes after five waves", game.zone_index == 1)
    game.wave = 16
    check("the last zone is reached at wave 16", game.zone_index == len(ZONES) - 1)
    game.wave = 21
    check("the zones wrap around", game.zone_index == 0)
    check("there are four zones and four palettes",
          len(ZONES) == 4 and len(ShooterGUI.ZONE_PALETTES) == 4)

    game = immortal(fresh(seed=4))
    game.wave = 5
    game.enemies.clear()
    game.spawn_queue.clear()
    for _ in range(220):
        game.step()
        if game.wave == 6:
            break
    check("a new zone announces itself", ZONES[1] in game.banner)

    # -- the top-five table ------------------------------------------------- #
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "scores.json"
        game = ShooterGame("normal", seed=1, score_path=path)
        game.start()
        for value in (100, 900, 500, 700, 300, 1200):
            game.score = value
            game.kills = 1
            game._recorded = False
            game.record_run()
        check("only five runs are kept", len(game.runs) == 5)
        check("the table is sorted best first",
              [run["score"] for run in game.runs] == [1200, 900, 700, 500, 300])
        reader = ShooterGame("normal", seed=1, score_path=path)
        check("the table survives a new session",
              [run["score"] for run in reader.runs] == [1200, 900, 700, 500, 300])
        check("the best score is the top of the table", reader.best == 1200)
        check("a run is folded in exactly once",
              ShooterGame("normal", seed=1, score_path=None).record_run() is False)
        path.write_text('{"best": 42}\n')
        older = ShooterGame("normal", seed=1, score_path=path)
        check("a score file from the older format still loads",
              older.best == 42 and older.runs == [])
        game = ShooterGame("normal", seed=2, score_path=path)
        game.start()
        game.score = 55
        check("recording twice does nothing the second time",
              game.record_run() is True and game.record_run() is False)
        check("a new run keeps the table", bool(game.runs))

    # -- invariants under a long, badly played run -------------------------- #
    rng = random.Random(4)
    game = ShooterGame("hard", seed=8)
    game.start()
    in_bounds = counts_ok = ranges_ok = finite_ok = True
    for _ in range(60 * 40):
        if game.over:
            game.start()
        game.step(rng.choice([-1.0, 0.0, 1.0]), rng.choice([-1.0, 0.0, 1.0]),
                  rng.random() < 0.06, rng.random() < 0.2, rng.random() < 0.02)
        player = game.player
        in_bounds &= (18 <= player.x <= ShooterGame.WIDTH - 18
                      and ShooterGame.PLAY_TOP + 16 <= player.y <= ShooterGame.HEIGHT - 18)
        counts_ok &= (len(game.enemy_bullets) <= ShooterGame.MAX_ENEMY_BULLETS
                      and len(game.bullets) <= ShooterGame.MAX_PLAYER_BULLETS
                      and len(game.particles) <= ShooterGame.MAX_PARTICLES
                      and len(game.powerups) <= ShooterGame.MAX_POWERUPS
                      and len(game.popups) <= ShooterGame.MAX_POPUPS
                      and len(game.lasers) <= ShooterGame.MAX_LASERS)
        ranges_ok &= (0 <= player.hp <= player.max_hp and 1 <= player.power <= 5
                      and 0 <= player.shield <= 2 and 0 <= player.bombs <= MAX_BOMBS
                      and 1 <= game.combo <= ShooterGame.MAX_COMBO)
        finite_ok &= all(math.isfinite(b.x) and math.isfinite(b.y)
                         for b in game.enemy_bullets)
    check("a long fuzzed run keeps the ship inside the play area", in_bounds)
    check("a long fuzzed run respects every cap", counts_ok)
    check("a long fuzzed run keeps every counter in range", ranges_ok)
    check("a long fuzzed run never produces a NaN position", finite_ok)
    check("the fuzzed run still made progress", game.kills > 0 or game.wave > 1)

    picture = result.ascii_frame().splitlines()
    grid = picture[1:-1]
    check("the ASCII frame is a grid",
          len(grid) == 28 and all(len(line) == 2 + 72 for line in grid))
    check("the ship shows up in the grid", any("@" in line for line in grid))
    check("the HUD line reports the score", "分數" in picture[0])
    check("hud_text summarises the run", "分數" in result.hud_text() and "波" in result.hud_text())

    check("headless bot runs default to no score file",
          inspect.signature(run_simulate).parameters["score_path"].default is None)

    if SoundBank is not None:
        emitted = {"shoot", "hit", "boom", "dash", "hurt", "shield", "pickup", "graze",
                   "clear", "perfect", "wave", "boss", "laser_warn", "laser", "bomb", "over"}
        check("every event the game emits is either sounded or deliberately silent",
              emitted - set(EVENT_SOUNDS) == {"dash", "graze", "clear", "laser_warn"})
        check("the bell fallback only covers events the game really emits",
              set(BELL_EVENTS) <= emitted)
        check("the sound module exposes a working bank",
              SoundBank(backend="none").available is False)

    print(f"\n{checks} checks passed.")
    return 0




# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


DEFAULT_SCORE_FILE = Path(__file__).with_name("highscore.json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Barrage 彈幕突擊 (Tkinter GUI). "
                    "--selftest, --simulate and --text need no display.",
    )
    parser.add_argument("-d", "--difficulty", choices=list(DIFFICULTIES), default="normal",
                        help="difficulty preset (default: normal)")
    parser.add_argument("--lives", type=int, default=3, help="starting lives (default: 3)")
    parser.add_argument("--bombs", type=int, default=BOMB_START,
                        help=f"starting screen-clearing bombs (default: {BOMB_START})")
    parser.add_argument("--sound", action="store_true",
                        help="play synthesized effects (needs shooter_audio.py)")
    parser.add_argument("--volume", type=float, default=0.6,
                        help="--sound volume, 0..1 (default: 0.6)")
    parser.add_argument("--sound-backend", choices=("auto", "pygame", "nssound", "command", "none"),
                        default="auto", help="force one playback backend (default: best)")
    parser.add_argument("--seed", type=int, help="fixed random seed (repeatable waves)")
    parser.add_argument("--no-highscore", action="store_true",
                        help="do not read or write shooter/highscore.json "
                             "(bot runs never do, whatever this says)")
    parser.add_argument("--simulate", type=float, metavar="SECONDS",
                        help="run the headless bot for this many seconds")
    parser.add_argument("--text", nargs="?", type=float, const=8.0, metavar="SECONDS",
                        help="print an ASCII picture of the frame after SECONDS (default 8)")
    parser.add_argument("--selftest", action="store_true", help="run the logic tests and exit")
    args = parser.parse_args(argv)

    score_path = None if args.no_highscore else DEFAULT_SCORE_FILE

    if args.selftest:
        print("running Barrage self tests...")
        return selftest()

    if args.text is not None:
        # headless bot runs never touch the player's own score table
        game = run_simulate(args.text, args.difficulty, args.seed, args.lives,
                            verbose=False, score_path=None, bombs=args.bombs)
        print(f"after {args.text:g}s of headless play "
              f"({DIFFICULTY_NAMES[args.difficulty]}, seed {args.seed}):\n")
        print(game.ascii_frame())
        return 0

    if args.simulate is not None:
        print(f"headless bot, difficulty {args.difficulty}, {args.simulate:g}s:")
        game = run_simulate(args.simulate, args.difficulty, args.seed, args.lives,
                            verbose=True, score_path=None, bombs=args.bombs)
        outcome = "survived" if not game.over else "was destroyed"
        print(f"\nthe bot {outcome} {game.frame / 60:.1f}s — {game.zone} wave {game.wave}, "
              f"score {game.score}, kills {game.kills}, grazes {game.grazes}, "
              f"accuracy {game.accuracy * 100:.0f}%, bombs {game.bombs_used}, "
              f"perfect waves {game.perfect_waves}")
        return 0

    if not TK_AVAILABLE:
        print("tkinter is unavailable — try --simulate, --text or --selftest.", file=sys.stderr)
        return 1
    try:
        ShooterGUI(args.difficulty, args.seed, args.lives, score_path,
                   sound=args.sound, bombs=args.bombs, volume=args.volume,
                   sound_backend=args.sound_backend).run()
    except tk.TclError as exc:  # no display
        print(f"cannot open a window ({exc}); try --simulate instead.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
