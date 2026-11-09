#!/usr/bin/env python3
"""Train a Tic-Tac-Toe player by evolution (numpy only, no other installs).

    python3 tictactoe_train.py            train, saves tictactoe_model.npz
    python3 tictactoe_train.py --test     check the saved model

A small neural network looks at the board and names the square it wants.
There is NO guard: if it names a square that is already taken, it loses on
the spot, so it has to learn the rules by itself. It plays as X and as O
against three kinds of opponent: a random one, a perfect one (random choice
among equally perfect moves) and a mix of the two.

"Done" is not a lucky streak. The best network is checked against EVERY
possible opponent (all legal replies, both sides): zero losses, zero illegal
moves. Ctrl+C stops and keeps the best.
"""

from __future__ import annotations

import argparse
import random
import sys
import time

import numpy as np

from tic_tac_toe import TicTacToe

N_IN, N_HID, N_OUT = 18, 48, 9
N_PARAMS = N_HID * N_IN + N_HID + N_OUT * N_HID + N_OUT + N_OUT * N_IN
MODEL_FILE = "tictactoe_model.npz"
other = TicTacToe.other


# --------------------------------------------------------------------------- #
# The brain
# --------------------------------------------------------------------------- #


def observe(board: list, mark: str) -> np.ndarray:
    """18 numbers: which squares are mine, which are the opponent's (empty = both 0)."""
    foe = other(mark)
    return np.array([float(b == mark) for b in board] + [float(b == foe) for b in board])


class Brain:
    """18 inputs -> 48 tanh units -> 9 outputs, one per square. Highest output wins.

    Each output also has a direct line from the inputs (w3), so a square can
    say "I am taken, don't pick me" without going through the hidden units.
    """

    def __init__(self, params: np.ndarray):
        a = N_HID * N_IN
        self.w1 = params[:a].reshape(N_HID, N_IN)
        self.b1 = params[a:a + N_HID]
        b = a + N_HID
        self.w2 = params[b:b + N_OUT * N_HID].reshape(N_OUT, N_HID)
        c = b + N_OUT * N_HID
        self.b2 = params[c:c + N_OUT]
        self.w3 = params[c + N_OUT:].reshape(N_OUT, N_IN)

    def act(self, board: list, mark: str) -> int:
        x = observe(board, mark)
        hidden = np.tanh(self.w1 @ x + self.b1)
        return int(np.argmax(self.w2 @ hidden + self.b2 + self.w3 @ x))  # no mask: may pick a taken square

    @staticmethod
    def load(path: str = MODEL_FILE) -> "Brain":
        return Brain(np.load(path)["params"])


# --------------------------------------------------------------------------- #
# Opponents and games
# --------------------------------------------------------------------------- #


def random_opponent(rng: random.Random):
    return lambda board, mark: rng.choice(TicTacToe.available_in(board))


def perfect_opponent(rng: random.Random):
    """Never makes a mistake, but picks randomly among equally perfect moves."""
    def move(board, mark):
        scored = []
        for index in TicTacToe.available_in(board):
            child = list(board)
            child[index] = mark
            scored.append((-TicTacToe.minimax(child, other(mark)), index))
        best = max(score for score, _ in scored)
        return rng.choice([index for score, index in scored if score == best])
    return move


def mixed_opponent(rng: random.Random):
    perfect, wild = perfect_opponent(rng), random_opponent(rng)
    return lambda board, mark: perfect(board, mark) if rng.random() < 0.5 else wild(board, mark)


OPPONENTS = {"random": random_opponent, "perfect": perfect_opponent, "mixed": mixed_opponent}


def play_game(act, opponent, net_mark: str) -> str:
    """One game. act(board, mark) is the network. Returns win / draw / loss / illegal."""
    board: list = [None] * 9
    turn = "X"
    while True:
        if turn == net_mark:
            move = act(board, net_mark)
            if board[move] is not None:
                return "illegal"
        else:
            move = opponent(board, turn)
        board[move] = turn
        winner = TicTacToe.winner_of(board)
        if winner is not None:
            return "win" if winner == net_mark else "loss"
        if TicTacToe.is_full(board):
            return "draw"
        turn = other(turn)


def exhaustive(act) -> tuple[int, int, int]:
    """Play against EVERY possible opponent, on both sides.

    Returns (games, losses, illegal moves). A network that never loses and
    never cheats has losses == illegal == 0, whatever the opponent does.
    """
    totals = [0, 0, 0]

    def walk(board: list, turn: str, net_mark: str) -> None:
        winner = TicTacToe.winner_of(board)
        if winner is not None or TicTacToe.is_full(board):
            totals[0] += 1
            totals[1] += winner is not None and winner != net_mark
            return
        if turn == net_mark:
            move = act(board, net_mark)
            if board[move] is not None:
                totals[0] += 1
                totals[2] += 1
                return
            board[move] = turn
            walk(board, other(turn), net_mark)
            board[move] = None
        else:
            for move in TicTacToe.available_in(board):
                board[move] = turn
                walk(board, other(turn), net_mark)
                board[move] = None

    for mark in ("X", "O"):
        walk([None] * 9, "X", mark)
    return tuple(totals)


# --------------------------------------------------------------------------- #
# Evolution
# --------------------------------------------------------------------------- #

SCORE = {"win": 1.0, "draw": 0.5, "loss": 0.0, "illegal": -0.5}


def evaluate(params: np.ndarray, gen_seed: int, games: int) -> float:
    """Average score per game. Every network faces the same opponent seeds."""
    brain = Brain(params)
    total = n = 0
    for i, make in enumerate(OPPONENTS.values()):
        for j, mark in enumerate(("X", "O")):
            for k in range(games):
                seed = gen_seed * 1000 + i * 200 + j * 100 + k
                total += SCORE[play_game(brain.act, make(random.Random(seed)), mark)]
                n += 1
    return total / n


def tally(act, make, games: int, seed: int) -> dict[str, int]:
    counts = {"win": 0, "draw": 0, "loss": 0, "illegal": 0}
    for mark in ("X", "O"):
        for k in range(games):
            counts[play_game(act, make(random.Random(seed + k)), mark)] += 1
    return counts


def report(params: np.ndarray) -> None:
    brain = Brain(params)
    games, losses, illegal = exhaustive(brain.act)
    print(f"\nevery possible opponent ({games} games, both sides): "
          f"{losses} losses, {illegal} illegal moves")
    wild = random.Random(0)
    players = (("AI    ", brain.act),
               ("random", lambda board, mark: wild.randrange(9)))
    for label, act in players:
        for name in ("random", "perfect"):
            c = tally(act, OPPONENTS[name], 500, 50_000)
            print(f"  {label}  vs {name:<7} (1000 games)  "
                  f"won {c['win']:4d}  drew {c['draw']:4d}  lost {c['loss']:4d}  "
                  f"illegal {c['illegal']:4d}")


def save(params: np.ndarray) -> None:
    np.savez(MODEL_FILE, params=params)


def start_population(rng, size: int) -> np.ndarray:
    """Random networks whose direct lines (w3) lean towards "avoid taken squares"."""
    pop = rng.normal(0, 1, (size, N_PARAMS))
    lean = np.zeros((N_OUT, N_IN))
    for i in range(N_OUT):
        lean[i, i] = lean[i, N_OUT + i] = -25.0  # my mark / foe's mark on square i
    pop[:, -N_OUT * N_IN:] = lean.ravel() + rng.normal(0, 0.3, (size, N_OUT * N_IN))
    return pop


def train(args) -> int:
    rng = np.random.default_rng(args.seed)
    pop = start_population(rng, args.pop)
    if args.resume:
        champion = np.load(MODEL_FILE)["params"]
        pop[0] = champion
        for i in range(1, args.pop):
            pop[i] = champion + rng.normal(0, 0.1, N_PARAMS)

    print(f"training - population {args.pop}, Ctrl+C to stop\n")
    best = pop[0]
    try:
        for gen in range(1, args.generations + 1):
            start = time.time()
            gen_seed = int(rng.integers(1 << 30))
            fitness = np.array([evaluate(p, gen_seed, args.games) for p in pop])
            order = np.argsort(-fitness)
            # The mixed score hides rare mistakes, so the top networks are re-ranked by
            # the number of games they lose or cheat in against EVERY possible opponent.
            top = order[:args.top]
            flaws = [sum(exhaustive(Brain(pop[i]).act)[1:]) for i in top]
            top = top[np.argsort(flaws, kind="stable")]
            order = np.concatenate([top, order[args.top:]])
            best = pop[order[0]].copy()
            save(best)

            games, losses, illegal = exhaustive(Brain(best).act)
            print(f"gen {gen:3d}   score {fitness[order[0]]:.2f} of 1.00   "
                  f"vs every opponent: {losses:3d} losses, {illegal:3d} illegal "
                  f"(of {games})   {time.time() - start:4.1f}s")

            if losses == 0 and illegal == 0:
                wins = tally(Brain(best).act, random_opponent, 100, 90_000)["win"]
                if wins >= 170:  # 200 games against a random opponent, both sides
                    print(f"\nnever loses, never cheats, wins {wins}/200 against a random opponent - done.")
                    break

            elites = pop[order[:max(2, args.pop // 10)]]
            top_half = pop[order[:args.pop // 2]]
            children = []
            for _ in range(args.pop - len(elites)):
                parent = top_half[rng.integers(len(top_half))]
                sigma = rng.choice([0.05, 0.15, 0.4])
                mask = rng.random(N_PARAMS) < 0.3
                children.append(parent + rng.normal(0, sigma, N_PARAMS) * mask)
            pop = np.vstack([elites, children])
    except KeyboardInterrupt:
        print("\nstopped.")

    save(best)
    print(f"saved {MODEL_FILE}")
    report(best)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evolve a Tic-Tac-Toe neural network.")
    parser.add_argument("--generations", type=int, default=500)
    parser.add_argument("--pop", type=int, default=200, help="networks per generation")
    parser.add_argument("--games", type=int, default=6,
                        help="games per opponent type per side per network per generation")
    parser.add_argument("--top", type=int, default=20,
                        help="how many top networks get re-ranked by their mistakes against every opponent")
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
