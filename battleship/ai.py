#!/usr/bin/env python3
"""Battleship AI opponent — three built-in bots plus a trained "learned" AI.

Usage
    from ai import BattleshipAI
    ai = BattleshipAI("hard", seed=42)
    fleet = ai.choose_placement()          # engine fleet format
    cell = ai.choose_attack(enemy_board)   # (r, c), always available

Fairness contract
    ``choose_attack`` only looks at *public* knowledge — ``board.shots``,
    ``board.sunk_ships``, ``board.available_cells()`` and ``board.hit_cells()``.
    It never inspects the positions of still-afloat ``board.ships``; reading
    those would be cheating.  Choices are deterministic for a fixed seed and
    the same public board state.

Difficulties
    easy    uniformly random available cell, with an occasional adjacent guess
            when a hit is unresolved.
    normal  hunt on a checkerboard/parity pattern; once a hit is unresolved,
            target its four neighbours, and extend along a line when two or
            more hits line up.
    hard    probability density: enumerate every legal placement of each
            remaining ship over cells that are not known misses and not known
            sunk ship cells, weight each placement by the unresolved hits it
            explains, and fire at the most probable cell (parity tie-break).
    learned a neural network trained by ai_train.py (imitation of ``hard`` and
            then self-play reinforcement).  It needs numpy and the model file
            ``battleship_model.npz`` next to this file; the three bots above
            stay pure standard library.  See ai_net.py for what it sees.
            Optional: with USE_LEARNED_PLACEMENT = True and a model from
            ``ai_train.py --place`` it places its own fleet too (off by default).
"""

from __future__ import annotations

import random

import engine

__all__ = ["BattleshipAI"]

# Adjacency used when targeting neighbours of a hit.
NEIGHBOURS: tuple[tuple[int, int], ...] = ((1, 0), (-1, 0), (0, 1), (0, -1))

#: Hidden-hit weighting used by the ``hard`` density map.  A placement that
#: explains *n* unresolved hits is worth ``1 + HIT_WEIGHT * n * n`` ordinary
#: placements, which makes the cells around hits (and line extensions) win.
HIT_WEIGHT = 8.0

#: Chance that ``easy`` takes a neighbour of a known hit instead of a random cell.
EASY_TARGET_CHANCE = 0.2

#: The learned AI places its fleet with the trained placement network only when
#: this is True (and battleship_place_model.npz exists).  False = random fleet,
#: like the bots.  The model file can stay in the folder; this switch ignores it.
USE_LEARNED_PLACEMENT = False


# --------------------------------------------------------------------------- #
# Placement enumeration (shared, immutable module constant)
# --------------------------------------------------------------------------- #

def _cell_bit(r: int, c: int) -> int:
    """Bit index for ``(r, c)`` in the 100-bit board mask."""
    return 1 << (r * engine.SIZE + c)


def _all_placements(size: int) -> tuple[tuple[int, tuple[tuple[int, int], ...]], ...]:
    """Every straight-line placement of a ship of *size* on an empty board.

    Each item is ``(bit_mask, cells)`` so the hot density loop can test a whole
    placement against the allowed-cell mask with one integer operation.
    """
    placements: list[tuple[int, tuple[tuple[int, int], ...]]] = []
    rows = [
        tuple((r, c + i) for i in range(size))
        for r in range(engine.SIZE)
        for c in range(engine.SIZE - size + 1)
    ]
    cols = [
        tuple((r + i, c) for i in range(size))
        for c in range(engine.SIZE)
        for r in range(engine.SIZE - size + 1)
    ]
    for cells in rows + cols:
        mask = 0
        for r, c in cells:
            mask |= _cell_bit(r, c)
        placements.append((mask, cells))
    return tuple(placements)


PLACEMENTS_BY_SIZE: dict[int, tuple[tuple[int, tuple[tuple[int, int], ...]], ...]] = {
    size: _all_placements(size) for _, size in engine.FLEET
}


# --------------------------------------------------------------------------- #
# Public-information helpers (the only board facts the AI may use)
# --------------------------------------------------------------------------- #

def _unresolved_hits(board) -> list[tuple[int, int]]:
    """Hit cells that do not belong to an already-sunk (announced) ship."""
    sunk_cells = {cell for ship in board.sunk_ships for cell in ship.cells}
    return [cell for cell in board.hit_cells() if cell not in sunk_cells]


def _remaining_ships(board) -> list[tuple[str, int]]:
    """``(name, size)`` for every ship that is not announced as sunk yet."""
    sunk_names = {ship.name for ship in board.sunk_ships}
    return [
        (name, engine.FLEET_BY_NAME[name])
        for name in engine.SHIP_NAMES
        if name not in sunk_names
    ]


def _public_signature(board) -> tuple:
    """Stable digest of the public board state, used to seed per-move RNGs.

    Two boards with identical shots and identical announced sunk ships produce
    an identical signature — that is what makes ``choose_attack`` fair and
    deterministic regardless of the hidden fleet layout.
    """
    shots = tuple(sorted((r, c, result) for (r, c), result in board.shots.items()))
    sunk = tuple(
        sorted((ship.name, tuple(sorted(ship.cells))) for ship in board.sunk_ships)
    )
    return (shots, sunk)


def _neighbour_cells(
    hits: list[tuple[int, int]], available: set[tuple[int, int]]
) -> list[tuple[int, int]]:
    """Available four-neighbours of any unresolved hit, sorted and deduped."""
    found: set[tuple[int, int]] = set()
    for r, c in hits:
        for dr, dc in NEIGHBOURS:
            cell = (r + dr, c + dc)
            if cell in available:
                found.add(cell)
    return sorted(found)


def _line_and_adjacent_targets(
    hits: list[tuple[int, int]], available: set[tuple[int, int]]
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Split candidate follow-up cells into line extensions and plain neighbours.

    Clusters of two or more unresolved hits that share a row or a column are
    assumed to be one ship, so the cells just past either end of the cluster are
    preferred.  Single hits (or diagonal clusters) use all four neighbours.
    """
    line_targets: set[tuple[int, int]] = set()
    adjacent: set[tuple[int, int]] = set()

    # Group unresolved hits into 4-connected clusters.
    unseen = set(hits)
    while unseen:
        start = unseen.pop()
        cluster = {start}
        stack = [start]
        while stack:
            r, c = stack.pop()
            for dr, dc in NEIGHBOURS:
                cell = (r + dr, c + dc)
                if cell in unseen:
                    unseen.discard(cell)
                    cluster.add(cell)
                    stack.append(cell)

        rows = {r for r, _ in cluster}
        cols = {c for _, c in cluster}
        extensions: list[tuple[int, int]] = []
        if len(cluster) >= 2 and len(rows) == 1:
            row = next(iter(rows))
            columns = [c for _, c in cluster]
            extensions = [(row, min(columns) - 1), (row, max(columns) + 1)]
        elif len(cluster) >= 2 and len(cols) == 1:
            col = next(iter(cols))
            rows_ = [r for r, _ in cluster]
            extensions = [(min(rows_) - 1, col), (max(rows_) + 1, col)]

        extensions = [cell for cell in extensions if cell in available]
        if extensions:
            line_targets.update(extensions)
        else:
            for r, c in cluster:
                for dr, dc in NEIGHBOURS:
                    cell = (r + dr, c + dc)
                    if cell in available:
                        adjacent.add(cell)

    return sorted(line_targets), sorted(adjacent)


def _parity_hunt(available: set[tuple[int, int]], rng: random.Random) -> tuple[int, int]:
    """Random unshot cell on the checkerboard parity pattern (fallback: any)."""
    parity = [cell for cell in sorted(available) if (cell[0] + cell[1]) % 2 == 0]
    return rng.choice(parity or sorted(available))


# --------------------------------------------------------------------------- #
# The AI
# --------------------------------------------------------------------------- #

class BattleshipAI:
    """A Battleship opponent with easy / normal / hard difficulty."""

    DIFFICULTIES = ("easy", "normal", "hard")
    #: every kind the constructor accepts (the three bots + the trained network)
    KINDS = DIFFICULTIES + ("learned",)

    def __init__(self, difficulty: str = "normal", seed: int | None = None):
        if difficulty not in self.KINDS:
            raise ValueError(
                f"unknown difficulty {difficulty!r}; chose one of {self.KINDS}"
            )
        self.difficulty = difficulty
        self.seed = seed
        self._policy = None
        self._placer = None
        if difficulty == "learned":
            # Raises ImportError (no numpy) or FileNotFoundError (not trained
            # yet); the UI turns both into a friendly message.
            from ai_net import PlacementPolicy, Policy, place_model_exists
            self._policy = Policy()
            # Learned placement is optional and off by default (USE_LEARNED_PLACEMENT
            # above); otherwise the learned AI places its fleet at random like the bots.
            if USE_LEARNED_PLACEMENT and place_model_exists():
                try:
                    self._placer = PlacementPolicy()
                except Exception:  # noqa: BLE001 - stale/incompatible file: ignore
                    self._placer = None
        # ``seed=None`` means "surprise me" once per instance; a fixed seed makes
        # the whole instance reproducible.
        self._seed_material = seed if seed is not None else random.Random().getrandbits(64)
        self._placement_count = 0

    # -- setup --------------------------------------------------------------- #
    @property
    def placement_learned(self) -> bool:
        """True when the fleet is placed by the trained placement network."""
        return self._placer is not None

    def choose_placement(self) -> list[dict]:
        """Return an always-legal classic fleet (engine fleet format).

        Random for the bots; the ``learned`` AI uses its placement network only if
        USE_LEARNED_PLACEMENT is True and ``battleship_place_model.npz`` exists.
        """
        rng = random.Random(
            f"placement|{self._seed_material}|{self._placement_count}"
        )
        self._placement_count += 1
        if self._placer is not None:
            try:
                fleet = self._placer.place(rng)
                engine.validate_placement(fleet)
                return fleet
            except Exception:  # noqa: BLE001 - never block a game on placement
                pass
        return engine.random_fleet(rng)

    # -- attack -------------------------------------------------------------- #
    def choose_attack(self, board: engine.Board) -> tuple[int, int]:
        """Pick the next cell to fire at, using only public information.

        Raises ``engine.ShotError("no_moves")`` when every cell has been shot.
        """
        available = board.available_cells()
        if not available:
            raise engine.ShotError("no_moves", "no available cells left to fire at")

        if self._policy is not None:
            return self._policy.choose(board)

        # Seed from the public state so equal public states give equal answers.
        rng = random.Random(
            f"attack|{self.difficulty}|{self._seed_material}|"
            f"{_public_signature(board)!r}"
        )
        available_set = set(available)

        if self.difficulty == "easy":
            return self._choose_easy(board, available, available_set, rng)
        if self.difficulty == "normal":
            return self._choose_normal(available, available_set, rng, board)
        return self._choose_hard(board, available_set, rng)

    # -- easy ---------------------------------------------------------------- #
    def _choose_easy(
        self,
        board,
        available: list[tuple[int, int]],
        available_set: set[tuple[int, int]],
        rng: random.Random,
    ) -> tuple[int, int]:
        hits = _unresolved_hits(board)
        targets = _neighbour_cells(hits, available_set)
        if targets and rng.random() < EASY_TARGET_CHANCE:
            return rng.choice(targets)
        return rng.choice(sorted(available))

    # -- normal -------------------------------------------------------------- #
    def _choose_normal(
        self,
        available: list[tuple[int, int]],
        available_set: set[tuple[int, int]],
        rng: random.Random,
        board,
    ) -> tuple[int, int]:
        hits = _unresolved_hits(board)
        if hits:
            line_targets, adjacent = _line_and_adjacent_targets(hits, available_set)
            if line_targets:
                return rng.choice(line_targets)
            if adjacent:
                return rng.choice(adjacent)
        return _parity_hunt(available_set, rng)

    # -- hard ---------------------------------------------------------------- #
    def _choose_hard(
        self,
        board,
        available_set: set[tuple[int, int]],
        rng: random.Random,
    ) -> tuple[int, int]:
        density = density_grid(board, available_set)

        # Highest density among cells we can still shoot; parity breaks ties.
        best = -1.0
        for r, c in available_set:
            if density[r][c] > best:
                best = density[r][c]
        tied = sorted(
            cell for cell in available_set if density[cell[0]][cell[1]] >= best - 1e-9
        )
        parity = [cell for cell in tied if (cell[0] + cell[1]) % 2 == 0]
        return rng.choice(parity or tied)


def density_grid(board, available_set: set[tuple[int, int]]) -> list[list[float]]:
    """The ``hard`` probability-density map (also the teacher for ai_train.py)."""
    hits = set(_unresolved_hits(board))
    available_mask = 0
    for r, c in available_set:
        available_mask |= _cell_bit(r, c)
    hits_mask = 0
    for r, c in hits:
        hits_mask |= _cell_bit(r, c)
    # Unshot cells may hide a ship; unresolved hits are known ship cells.
    allowed_mask = available_mask | hits_mask
    density = [[0.0] * engine.SIZE for _ in range(engine.SIZE)]

    for name, _size in _remaining_ships(board):
        for placement_mask, placement in PLACEMENTS_BY_SIZE[engine.FLEET_BY_NAME[name]]:
            if placement_mask & ~allowed_mask:
                continue
            covered = (placement_mask & hits_mask).bit_count()
            weight = 1.0 + HIT_WEIGHT * covered * covered
            for r, c in placement:
                density[r][c] += weight
    return density
