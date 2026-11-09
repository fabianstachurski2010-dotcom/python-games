#!/usr/bin/env python3
"""Train a Barrage-playing neural network by evolution (numpy only, no other installs).

    python3 shooter_train.py                 train, saves shooter_model.npz
    python3 shooter_train.py --jobs 4        evaluate the population on four cores
    python3 shooter_train.py --resume        keep going from the saved model
    python3 shooter_train.py --test          score the saved model (and the scripted bot)

A small network sees raw numbers — its own ship, the eight nearest enemy bullets, the three
nearest enemies, the two nearest beams and a few counters — and answers with a movement
vector, a dash and a bomb, every single frame. The best networks of a generation are kept
and mutated to make the next one. Ctrl+C stops and keeps the best.

The hand-written dodging policy in `shooter.py` (`bot_control`) is never used for control;
`--test` only runs it afterwards, as the baseline to beat.
"""

from __future__ import annotations

import argparse
import heapq
import multiprocessing
import os
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

from shooter import (BOMB_START, DASH_COOLDOWN, DT, MAX_BOMBS, ShooterGame, bot_control)

N_BULLETS = 8          # nearest enemy shots the network can see
N_ENEMIES = 3
N_BEAMS = 2
N_SELF = 8
N_WORLD = 5
N_IN = N_SELF + N_BULLETS * 4 + N_ENEMIES * 4 + N_BEAMS * 6 + N_WORLD
N_HID = 32
N_OUT = 4              # dx, dy, dash, bomb
N_PARAMS = (N_IN + 1) * N_HID + (N_HID + 1) * N_OUT
MODEL_FILE = "shooter_model.npz"

# reward weights: progress first, then kills, then simply staying alive
W_KILL, W_WAVE, W_PERFECT, W_SCORE, W_GRAZE = 2.0, 30.0, 6.0, 0.002, 0.5


# --------------------------------------------------------------------------- #
# What the network sees
# --------------------------------------------------------------------------- #


def observe(game: ShooterGame) -> np.ndarray:
    """Raw state -> a fixed-length vector. No rules, no hand-written policy."""
    width, height = ShooterGame.WIDTH, ShooterGame.HEIGHT
    player = game.player
    vector = np.zeros(N_IN, dtype=np.float64)
    index = 0

    vector[index:index + N_SELF] = (
        player.x / width,
        player.y / height,
        player.hp / player.max_hp,
        player.shield / 2.0,
        player.power / 5.0,
        player.bombs / MAX_BOMBS,
        1.0 - min(1.0, player.dash_cd / DASH_COOLDOWN),
        1.0 if player.invulnerable else 0.0,
    )
    index += N_SELF

    def distance2(entity) -> float:
        return (entity.x - player.x) ** 2 + (entity.y - player.y) ** 2

    bullets = heapq.nsmallest(N_BULLETS, game.enemy_bullets, key=distance2)
    for slot in range(N_BULLETS):
        if slot < len(bullets):
            bullet = bullets[slot]
            vector[index:index + 4] = (
                (bullet.x - player.x) / width,
                (bullet.y - player.y) / height,
                bullet.vx / 300.0,
                bullet.vy / 300.0,
            )
        index += 4

    enemies = heapq.nsmallest(N_ENEMIES, game.enemies, key=distance2)
    for slot in range(N_ENEMIES):
        if slot < len(enemies):
            enemy = enemies[slot]
            vector[index:index + 4] = (
                (enemy.x - player.x) / width,
                (enemy.y - player.y) / height,
                enemy.hp / enemy.max_hp,
                1.0 if enemy.entered else 0.0,
            )
        index += 4

    beams = heapq.nsmallest(N_BEAMS, game.lasers, key=distance2)
    for slot in range(N_BEAMS):
        if slot < len(beams):
            beam = beams[slot]
            vector[index:index + 6] = (
                (beam.x - player.x) / width,
                (beam.y - player.y) / height,
                np.sin(beam.angle),
                np.cos(beam.angle),
                1.0 if beam.warm > 0.0 else 0.0,
                1.0 if beam.warm <= 0.0 else 0.0,
            )
        index += 6

    vector[index:index + N_WORLD] = (
        min(1.0, game.wave / 20.0),
        len(game.enemies) / 20.0,
        len(game.enemy_bullets) / ShooterGame.MAX_ENEMY_BULLETS,
        game.wave_progress,
        game.combo / ShooterGame.MAX_COMBO,
    )
    return vector


# --------------------------------------------------------------------------- #
# The network
# --------------------------------------------------------------------------- #


class Brain:
    """One hidden layer, tanh; movement is continuous, the two buttons are 0/1."""

    def __init__(self, params: np.ndarray):
        params = np.asarray(params, dtype=np.float64).ravel()
        if params.size != N_PARAMS:
            raise ValueError(f"expected {N_PARAMS} parameters, got {params.size}")
        cut = (N_IN + 1) * N_HID
        self.w1 = params[:N_IN * N_HID].reshape(N_IN, N_HID)
        self.b1 = params[N_IN * N_HID:cut]
        self.w2 = params[cut:cut + N_HID * N_OUT].reshape(N_HID, N_OUT)
        self.b2 = params[cut + N_HID * N_OUT:]

    def forward(self, x: np.ndarray) -> np.ndarray:
        hidden = np.tanh(x @ self.w1 + self.b1)
        return hidden @ self.w2 + self.b2

    def act(self, game: ShooterGame) -> tuple[float, float, bool, bool]:
        """The four controls `ShooterGame.step()` takes, straight from the network."""
        out = self.forward(observe(game))
        return (float(np.tanh(out[0])), float(np.tanh(out[1])),
                bool(out[2] > 0.0), bool(out[3] > 0.0))

    def controls(self, game: ShooterGame) -> tuple[float, float, bool, bool, bool]:
        """`act()` in the shape `step()` wants (with focus off)."""
        dx, dy, dash, bomb = self.act(game)
        return dx, dy, dash, False, bomb

    @classmethod
    def load(cls, path: str = MODEL_FILE) -> "Brain":
        return cls(np.load(path)["params"])


def save(params: np.ndarray, path: str = MODEL_FILE) -> None:
    np.savez(path, params=np.asarray(params, dtype=np.float64),
             n_in=N_IN, n_hid=N_HID, n_out=N_OUT)


# --------------------------------------------------------------------------- #
# One episode, and the fitness of a genome
# --------------------------------------------------------------------------- #


def fitness_of(game: ShooterGame) -> float:
    """The reward: progress first, then kills, then simply staying alive."""
    return (game.frame * DT
            + W_KILL * game.kills
            + W_WAVE * max(0, game.wave - 1)
            + W_PERFECT * game.perfect_waves
            + W_SCORE * game.score
            + W_GRAZE * game.grazes)


def episode(params: np.ndarray, seed: int, difficulty: str, seconds: float,
            lives: int = 3) -> dict:
    game = ShooterGame(difficulty, seed=seed, lives=lives, score_path=None)
    game.start()
    brain = Brain(params)
    for _ in range(int(seconds / DT)):
        if game.over:
            break
        game.step(*brain.controls(game))
    cleared = max(0, game.wave - 1)
    return {
        "fitness": fitness_of(game),
        "seconds": game.frame * DT,
        "wave": game.wave,
        "cleared": cleared,
        "kills": game.kills,
        "score": game.score,
        "grazes": game.grazes,
        "accuracy": game.accuracy,
        "alive": not game.over,
    }


def evaluate(params: np.ndarray, seeds, difficulty: str, seconds: float,
             lives: int = 3) -> tuple[float, dict]:
    """Mean fitness over the seeds, plus the averaged numbers worth printing."""
    totals = {"seconds": 0.0, "wave": 0.0, "cleared": 0.0, "kills": 0.0,
              "score": 0.0, "grazes": 0.0, "accuracy": 0.0, "alive": 0.0, "fitness": 0.0}
    for seed in seeds:
        result = episode(params, int(seed), difficulty, seconds, lives)
        for key in totals:
            totals[key] += result[key]
    count = max(1, len(seeds))
    return totals["fitness"] / count, {key: value / count for key, value in totals.items()}


def _score_job(job) -> tuple[float, dict]:
    """Module-level so multiprocessing can pickle it."""
    params, seeds, difficulty, seconds, lives = job
    return evaluate(params, seeds, difficulty, seconds, lives)


def report(params: np.ndarray, difficulty: str, seconds: float, episodes: int = 20,
           lives: int = 3, seed: int = 12345, label: str = "network") -> dict:
    rng = np.random.default_rng(seed)
    seeds = [int(rng.integers(1 << 30)) for _ in range(episodes)]
    fitness, stats = evaluate(params, seeds, difficulty, seconds, lives)
    print(f"  {label:9s} fitness {fitness:6.1f} | survived {stats['seconds']:5.1f}s of "
          f"{seconds:g}s ({stats['alive'] * 100:3.0f}% still alive) | wave {stats['wave']:4.1f} "
          f"| kills {stats['kills']:5.1f} | score {stats['score']:7.0f} "
          f"| acc {stats['accuracy'] * 100:4.1f}% | graze {stats['grazes']:4.1f}")
    return stats


# --------------------------------------------------------------------------- #
# Evolution
# --------------------------------------------------------------------------- #


def train(args) -> int:
    rng = np.random.default_rng(args.seed)
    population = rng.normal(0, 1, (args.pop, N_PARAMS))
    if args.resume and os.path.exists(MODEL_FILE):
        champion = np.load(MODEL_FILE)["params"]
        population[0] = champion
        for row in range(1, args.pop):
            population[row] = champion + rng.normal(0, 0.1, N_PARAMS)
        print(f"resumed from {MODEL_FILE}")

    if args.jobs > 1:
        pool = multiprocessing.get_context("spawn").Pool(args.jobs)
        print(f"evaluating with {args.jobs} worker processes")
    else:
        pool = None

    best = population[0].copy()
    best_fitness = -1e18
    print(f"training on '{args.difficulty}' - {args.seconds:g}s episodes, "
          f"{args.episodes} seeds each, population {args.pop}, {N_PARAMS} parameters")
    print("Ctrl+C stops and keeps the best\n")
    try:
        for generation in range(1, args.generations + 1):
            started = time.time()
            seeds = [int(rng.integers(1 << 30)) for _ in range(args.episodes)]
            jobs = [(genome, seeds, args.difficulty, args.seconds, args.lives)
                    for genome in population]
            results = pool.map(_score_job, jobs) if pool else [_score_job(j) for j in jobs]
            fitness = [r[0] for r in results]
            order = np.argsort(fitness)[::-1]
            champion = results[order[0]][1]
            if fitness[order[0]] > best_fitness:
                best_fitness = fitness[order[0]]
                best = population[order[0]].copy()
                save(best)
            print(f"gen {generation:3d}  best {fitness[order[0]]:7.1f} "
                  f"(avg {sum(fitness) / len(fitness):7.1f})  survived "
                  f"{champion['seconds']:5.1f}s  wave {champion['wave']:4.1f}  "
                  f"kills {champion['kills']:5.1f}  score {champion['score']:7.0f}  "
                  f"{time.time() - started:5.1f}s")
            if generation == args.generations:
                break

            elites = population[order[:max(2, args.pop // 10)]]
            parents = population[order[:max(2, args.pop // 2)]]
            children = []
            for _ in range(args.pop - len(elites)):
                parent = parents[rng.integers(len(parents))]
                mask = rng.random(N_PARAMS) < 0.3
                children.append(parent + rng.normal(0, args.sigma, N_PARAMS) * mask)
            population = np.vstack([elites, children])
    except KeyboardInterrupt:
        print("\nstopped early")
    finally:
        if pool is not None:
            pool.close()
            pool.join()

    save(best)
    print(f"\nsaved {MODEL_FILE} (best generation fitness {best_fitness:.1f})\n")
    print(f"test on 20 unseen {args.seconds:g}s episodes on '{args.difficulty}':")
    report(best, args.difficulty, args.seconds, lives=args.lives, label="network")
    report_baseline(args.difficulty, args.seconds, lives=args.lives)
    return 0


def report_baseline(difficulty: str, seconds: float, episodes: int = 20, lives: int = 3,
                    seed: int = 12345) -> dict:
    """The hand-written dodging bot, so the network has something to be measured against."""
    rng = np.random.default_rng(seed)
    seeds = [int(rng.integers(1 << 30)) for _ in range(episodes)]
    totals = {"seconds": 0.0, "wave": 0.0, "kills": 0.0, "score": 0.0,
              "grazes": 0.0, "accuracy": 0.0, "alive": 0.0, "fitness": 0.0}
    for seed_value in seeds:
        game = ShooterGame(difficulty, seed=seed_value, lives=lives, score_path=None)
        game.start()
        for _ in range(int(seconds / DT)):
            if game.over:
                break
            game.step(*bot_control(game))
        totals["fitness"] += fitness_of(game)
        totals["seconds"] += game.frame * DT
        totals["wave"] += game.wave
        totals["kills"] += game.kills
        totals["score"] += game.score
        totals["grazes"] += game.grazes
        totals["accuracy"] += game.accuracy
        totals["alive"] += 0.0 if game.over else 1.0
    n = max(1, len(seeds))
    stats = {key: value / n for key, value in totals.items()}
    print(f"  {'scripted':9s} fitness {stats['fitness']:6.1f} | survived {stats['seconds']:5.1f}s of "
          f"{seconds:g}s ({stats['alive'] * 100:3.0f}% still alive) | wave {stats['wave']:4.1f} "
          f"| kills {stats['kills']:5.1f} | score {stats['score']:7.0f} "
          f"| acc {stats['accuracy'] * 100:4.1f}% | graze {stats['grazes']:4.1f}")
    return stats


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

    game = ShooterGame("easy", seed=4)
    game.start()
    for _ in range(60):
        game.step()

    vector = observe(game)
    check(f"the state vector is {N_IN} numbers wide", vector.shape == (N_IN,))
    check("every input is a finite number", bool(np.all(np.isfinite(vector))))
    check("inputs stay in a sane range", float(np.max(np.abs(vector))) < 20.0)

    rng = np.random.default_rng(0)
    brain = Brain(rng.normal(0, 1, N_PARAMS))
    controls = brain.act(game)
    check("the network answers with four controls", len(controls) == 4)
    check("movement comes back inside -1..1",
          -1.0 <= controls[0] <= 1.0 and -1.0 <= controls[1] <= 1.0)
    check("dash and bomb are booleans",
          isinstance(controls[2], bool) and isinstance(controls[3], bool))
    check("controls() matches the step() signature", len(brain.controls(game)) == 5)
    check("the parameter count is what the layers add up to",
          N_PARAMS == (N_IN + 1) * N_HID + (N_HID + 1) * N_OUT)
    check("a brain with the wrong shape is rejected",
          "ValueError" in _raises(lambda: Brain(np.zeros(3))))
    check("the same genome behaves the same way twice",
          brain.act(game) == brain.act(game))

    zeros = np.zeros(N_PARAMS)
    check("a zero genome does not move the ship",
          Brain(zeros).act(game)[:2] == (0.0, 0.0))
    big = np.zeros(N_PARAMS)
    big[:N_IN * N_HID] = 50.0
    check("a saturated genome still stays inside -1..1",
          all(-1.0 <= value <= 1.0 for value in Brain(big).act(game)[:2]))

    idle = episode(zeros, seed=1, difficulty="easy", seconds=6.0)
    check("a zero genome survives the episode it was given",
          idle["seconds"] > 0.0 and idle["fitness"] > 0.0)
    check("an episode never runs longer than asked", idle["seconds"] <= 6.0 + DT)
    check("the episode report carries the numbers the trainer prints",
          {"wave", "kills", "score", "grazes", "accuracy"} <= set(idle))

    # the reward has to be shaped so evolution has a slope to climb
    short, long = ShooterGame("easy", seed=7), ShooterGame("easy", seed=7)
    short.start()
    long.start()
    for _ in range(60):
        short.step()
    for _ in range(240):
        long.step()
    check("surviving longer pays more", fitness_of(long) > fitness_of(short))
    before = fitness_of(long)
    long.kills += 1
    check("a kill pays", fitness_of(long) > before)
    before = fitness_of(long)
    long.wave += 1
    check("clearing a wave pays more than a kill", fitness_of(long) > before + W_KILL)
    before = fitness_of(long)
    long.score += 1000
    check("score pays a little", fitness_of(long) > before)
    check("a fresh run is worth nothing yet", fitness_of(ShooterGame("easy", seed=1)) == 0.0)

    fitness, stats = evaluate(zeros, [1, 2], "easy", 4.0)
    check("evaluate averages over seeds", fitness > 0.0 and stats["seconds"] > 0.0)
    check("evaluate reports the fraction that stayed alive", 0.0 <= stats["alive"] <= 1.0)

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "model.npz"
        genome = rng.normal(0, 1, N_PARAMS)
        save(genome, path)
        check("a saved genome loads back with the same answer",
              Brain.load(path).act(game) == Brain(genome).act(game))
        stored = np.load(path)
        check("the model file records its own shape",
              int(stored["n_in"]) == N_IN and int(stored["n_hid"]) == N_HID
              and int(stored["n_out"]) == N_OUT)

    print(f"\n{checks} checks passed.")
    return 0


def _raises(function) -> str:
    try:
        function()
    except Exception as exc:                     # noqa: BLE001 - the message is the point
        return type(exc).__name__
    return ""


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evolve a Barrage-playing neural network.")
    parser.add_argument("-d", "--difficulty", choices=["easy", "normal", "hard"], default="easy",
                        help="difficulty to train (and test) on")
    parser.add_argument("--generations", type=int, default=40)
    parser.add_argument("--pop", type=int, default=48, help="networks per generation")
    parser.add_argument("--episodes", type=int, default=2, help="seeds per network per generation")
    parser.add_argument("--seconds", type=float, default=25.0, help="length of one episode")
    parser.add_argument("--lives", type=int, default=3)
    parser.add_argument("--sigma", type=float, default=0.2, help="mutation size")
    parser.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1),
                        help="worker processes (1 = run in this process)")
    parser.add_argument("--seed", type=int, help="fix the random seed")
    parser.add_argument("--resume", action="store_true", help="continue from the saved model")
    parser.add_argument("--test", action="store_true",
                        help="only test the saved model against the scripted bot")
    parser.add_argument("--selftest", action="store_true", help="run the trainer's own tests")
    args = parser.parse_args(argv)

    if args.selftest:
        print("running Barrage trainer self tests...")
        return selftest()

    if args.test:
        try:
            params = np.load(MODEL_FILE)["params"]
        except FileNotFoundError:
            print(f"{MODEL_FILE} not found - run shooter_train.py first.", file=sys.stderr)
            return 1
        print(f"testing on 20 unseen {args.seconds:g}s episodes on '{args.difficulty}':")
        report(params, args.difficulty, args.seconds, lives=args.lives, label="network")
        report_baseline(args.difficulty, args.seconds, lives=args.lives)
        return 0

    return train(args)


if __name__ == "__main__":
    sys.exit(main())
