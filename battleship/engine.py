#!/usr/bin/env python3
"""Battleship rules engine — pure stdlib, no UI, no sockets.

Public API (frozen; the UI, AI and network server all depend on it):

    SIZE, FLEET, FLEET_BY_NAME, TOTAL_SHIP_CELLS
    ship_cells(size, r, c, horizontal) -> tuple[(r, c), ...]
    random_fleet(rng=None, allow_touch=True) -> list[{"name", "cells"}]
    validate_placement(placements, allow_touch=True) -> list[Ship]

    Ship:   .name .cells .hits .size .sunk .to_dict()
    Board:  .place_fleet(placements) .random_fleet() .fire(r, c)
            .all_sunk .remaining_cells .to_dict() .placed
    Game:   .place_fleet(index, placements) .fire(index, r, c)
            .start_battle(starter) .phase .turn .winner .boards .log

Rules
    * 10 x 10 grid, columns A-J, rows 1-10 (internally r, c = 0..9).
    * Classic fleet: carrier 5, battleship 4, cruiser 3, submarine 3, destroyer 2.
    * Ships may touch (no adjacency restriction); they may not overlap.
    * A hit lets the same player fire again; a miss passes the turn.
    * A ship is sunk when all its cells have been hit. Last fleet down loses.
"""

from __future__ import annotations

import random

SIZE = 10
FLEET = (
    ("carrier", 5),
    ("battleship", 4),
    ("cruiser", 3),
    ("submarine", 3),
    ("destroyer", 2),
)
FLEET_BY_NAME = dict(FLEET)
SHIP_NAMES = tuple(name for name, _ in FLEET)
TOTAL_SHIP_CELLS = sum(size for _, size in FLEET)  # 17


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

class BattleshipError(Exception):
    """Base class for every rule violation."""

    code = "error"

    def __init__(self, code: str | None = None, message: str = ""):
        if code is not None:
            self.code = code
        self.message = message or self.code
        super().__init__(self.message)

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


class PlacementError(BattleshipError):
    code = "bad_placement"


class ShotError(BattleshipError):
    code = "bad_shot"


class PhaseError(BattleshipError):
    code = "bad_phase"


# --------------------------------------------------------------------------- #
# Placement helpers
# --------------------------------------------------------------------------- #

def in_bounds(r: int, c: int) -> bool:
    return 0 <= r < SIZE and 0 <= c < SIZE


def ship_cells(size: int, r: int, c: int, horizontal: bool) -> tuple[tuple[int, int], ...]:
    """Cells occupied by a ship of *size* anchored at (r, c).

    Raises PlacementError("out_of_bounds") when the ship leaves the board.
    """
    if size <= 0:
        raise PlacementError("bad_size", f"ship size must be positive, got {size}")
    cells = []
    for i in range(size):
        rr, cc = (r, c + i) if horizontal else (r + i, c)
        if not in_bounds(rr, cc):
            raise PlacementError("out_of_bounds", f"ship leaves the board at ({rr}, {cc})")
        cells.append((rr, cc))
    return tuple(cells)


def _ship_from_item(item) -> Ship:
    """Accept Ship, {"name","cells"} or {"name","r","c","horizontal"}."""
    if isinstance(item, Ship):
        return Ship(item.name, tuple(item.cells))
    if not isinstance(item, dict):
        raise PlacementError("bad_shape", f"placement must be a dict or Ship, got {type(item).__name__}")
    name = item.get("name")
    if name not in FLEET_BY_NAME:
        raise PlacementError("unknown_ship", f"unknown ship: {name!r}")
    if "cells" in item:
        raw = item["cells"]
        try:
            cells = tuple((int(cell[0]), int(cell[1])) for cell in raw)
        except (TypeError, ValueError, IndexError) as exc:
            raise PlacementError("bad_shape", f"bad cells for {name}: {raw!r}") from exc
        return Ship(name, cells)
    r, c = item.get("r"), item.get("c")
    horizontal = bool(item.get("horizontal", True))
    if not isinstance(r, int) or not isinstance(c, int):
        raise PlacementError("bad_shape", f"placement for {name} needs cells or r/c")
    return Ship(name, ship_cells(FLEET_BY_NAME[name], r, c, horizontal))


def validate_placement(placements, allow_touch: bool = True) -> list[Ship]:
    """Validate a whole fleet and return normalised Ship objects.

    Codes: bad_shape, unknown_ship, wrong_fleet, bad_size, out_of_bounds,
    overlap (and touch when allow_touch is False).
    """
    if placements is None:
        raise PlacementError("bad_shape", "no placements")
    items = list(placements)
    ships = [_ship_from_item(item) for item in items]

    # Exactly the classic fleet: every name once.
    seen: dict[str, int] = {}
    for ship in ships:
        seen[ship.name] = seen.get(ship.name, 0) + 1
    for name, size in FLEET:
        count = seen.get(name, 0)
        if count != 1:
            raise PlacementError(
                "wrong_fleet",
                f"fleet needs exactly one {name} (size {size}), got {count}",
            )

    occupied: dict[tuple[int, int], str] = {}
    for ship in ships:
        expected = FLEET_BY_NAME[ship.name]
        if len(ship.cells) != expected:
            raise PlacementError(
                "bad_size", f"{ship.name} must be {expected} cells, got {len(ship.cells)}"
            )
        if len(set(ship.cells)) != len(ship.cells):
            raise PlacementError("overlap", f"{ship.name} repeats a cell")
        # Must be a straight line (horizontal or vertical, no wrapping).
        rows = {r for r, _ in ship.cells}
        cols = {c for _, c in ship.cells}
        if len(rows) != 1 and len(cols) != 1:
            raise PlacementError("bad_shape", f"{ship.name} is not in a straight line")
        for r, c in ship.cells:
            if not in_bounds(r, c):
                raise PlacementError("out_of_bounds", f"{ship.name} cell ({r}, {c}) outside board")
            if (r, c) in occupied:
                raise PlacementError(
                    "overlap", f"{ship.name} overlaps {occupied[(r, c)]} at ({r}, {c})"
                )
            occupied[(r, c)] = ship.name

    if not allow_touch:
        for r, c in occupied:
            for dr, dc in ((1, 0), (0, 1)):
                other = occupied.get((r + dr, c + dc))
                if other is not None and other != occupied[(r, c)]:
                    raise PlacementError("touch", f"ships touch at ({r}, {c})")

    return ships


# --------------------------------------------------------------------------- #
# Fleet generation
# --------------------------------------------------------------------------- #

def random_fleet(rng: random.Random | None = None,
                 allow_touch: bool = True,
                 attempts: int = 2000) -> list[dict]:
    """Return a random legal fleet as ``[{"name", "cells"}, ...]``."""
    rng = rng or random.Random()
    for _ in range(attempts):
        ships: list[dict] = []
        occupied: set[tuple[int, int]] = set()
        ok = True
        for name, size in FLEET:
            for _try in range(200):
                horizontal = rng.random() < 0.5
                r = rng.randrange(SIZE)
                c = rng.randrange(SIZE)
                try:
                    cells = ship_cells(size, r, c, horizontal)
                except PlacementError:
                    continue
                if any(cell in occupied for cell in cells):
                    continue
                if not allow_touch and _touches(cells, occupied):
                    continue
                ships.append({"name": name, "cells": [list(cell) for cell in cells]})
                occupied.update(cells)
                break
            else:
                ok = False
                break
        if ok:
            return ships
    raise PlacementError("generation_failed", "could not generate a random fleet")


def _touches(cells, occupied: set[tuple[int, int]]) -> bool:
    for r, c in cells:
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if (r + dr, c + dc) in occupied:
                    return True
    return False


# --------------------------------------------------------------------------- #
# Board / Ship
# --------------------------------------------------------------------------- #

class Ship:
    __slots__ = ("name", "cells", "hits")

    def __init__(self, name: str, cells):
        if name not in FLEET_BY_NAME:
            raise PlacementError("unknown_ship", f"unknown ship: {name!r}")
        self.name = name
        self.cells = tuple(tuple(cell) for cell in cells)
        self.hits: set[tuple[int, int]] = set()

    # -- identity ---------------------------------------------------------- #
    @property
    def size(self) -> int:
        return len(self.cells)

    @property
    def sunk(self) -> bool:
        return len(self.hits) >= self.size

    @property
    def afloat(self) -> bool:
        return not self.sunk

    def hit(self, r: int, c: int) -> None:
        self.hits.add((r, c))

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "cells": [list(cell) for cell in self.cells],
            "hits": [list(cell) for cell in sorted(self.hits)],
            "sunk": self.sunk,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Ship({self.name!r}, {self.cells!r})"


class Board:
    """One player's ocean: their fleet plus every shot ever fired at it."""

    def __init__(self) -> None:
        self.ships: list[Ship] = []
        self.shots: dict[tuple[int, int], str] = {}  # (r, c) -> "hit" | "miss"

    # -- placement ---------------------------------------------------------- #
    @property
    def placed(self) -> bool:
        return bool(self.ships)

    def place_fleet(self, placements, allow_touch: bool = True) -> None:
        ships = validate_placement(placements, allow_touch=allow_touch)
        self.ships = ships
        self.shots.clear()

    def random_fleet(self, rng: random.Random | None = None,
                     allow_touch: bool = True) -> list[dict]:
        fleet = random_fleet(rng, allow_touch=allow_touch)
        self.place_fleet(fleet, allow_touch=allow_touch)
        return fleet

    # -- queries ------------------------------------------------------------ #
    def ship_at(self, r: int, c: int) -> Ship | None:
        for ship in self.ships:
            if (r, c) in ship.cells:
                return ship
        return None

    @property
    def all_sunk(self) -> bool:
        return bool(self.ships) and all(ship.sunk for ship in self.ships)

    @property
    def sunk_ships(self) -> list[Ship]:
        return [ship for ship in self.ships if ship.sunk]

    @property
    def remaining_cells(self) -> int:
        return sum(ship.size - len(ship.hits) for ship in self.ships)

    def hit_cells(self) -> list[tuple[int, int]]:
        return [cell for cell, result in self.shots.items() if result == "hit"]

    def miss_cells(self) -> list[tuple[int, int]]:
        return [cell for cell, result in self.shots.items() if result == "miss"]

    def available_cells(self) -> list[tuple[int, int]]:
        """Every cell that can still be fired at (also used by the AI)."""
        return [
            (r, c)
            for r in range(SIZE)
            for c in range(SIZE)
            if (r, c) not in self.shots
        ]

    # -- firing -------------------------------------------------------------- #
    def fire(self, r: int, c: int) -> dict:
        if not in_bounds(r, c):
            raise ShotError("out_of_bounds", f"({r}, {c}) is outside the board")
        if (r, c) in self.shots:
            raise ShotError("already_shot", f"({r}, {c}) was already fired at")
        ship = self.ship_at(r, c)
        hit = ship is not None
        self.shots[(r, c)] = "hit" if hit else "miss"
        sunk = None
        if hit:
            ship.hit(r, c)
            if ship.sunk:
                sunk = ship
        return {
            "r": r,
            "c": c,
            "hit": hit,
            "sunk": sunk.name if sunk else None,
            "sunk_cells": [list(cell) for cell in sunk.cells] if sunk else [],
            "ship_size": sunk.size if sunk else (ship.size if ship else 0),
            "game_over": self.all_sunk,
        }

    # -- views --------------------------------------------------------------- #
    def to_dict(self) -> dict:
        return {
            "ships": [ship.to_dict() for ship in self.ships],
            "shots": [[r, c, result] for (r, c), result in sorted(self.shots.items())],
        }

    def enemy_view(self) -> dict:
        """What the opponent may know: shot results and fully sunk ships only."""
        return {
            "shots": [[r, c, result] for (r, c), result in sorted(self.shots.items())],
            "sunk": [
                {"name": ship.name, "cells": [list(cell) for cell in ship.cells]}
                for ship in self.sunk_ships
            ],
        }


# --------------------------------------------------------------------------- #
# Game (two boards, turn flow)
# --------------------------------------------------------------------------- #

class Game:
    """A single match. ``phase``: placing -> battle -> over."""

    def __init__(self) -> None:
        self.boards: list[Board] = [Board(), Board()]
        self.phase = "placing"
        self.turn: int | None = None
        self.winner: int | None = None
        self.forfeited = False
        self.starter = 0
        self.shots_fired = [0, 0]
        self.log: list[dict] = []
        self.last_result: dict | None = None

    # -- setup --------------------------------------------------------------- #
    def reset(self) -> None:
        self.boards = [Board(), Board()]
        self.phase = "placing"
        self.turn = None
        self.winner = None
        self.forfeited = False
        self.shots_fired = [0, 0]
        self.log = []
        self.last_result = None

    def place_fleet(self, index: int, placements, allow_touch: bool = True) -> None:
        if index not in (0, 1):
            raise PhaseError("bad_player", f"bad player index {index}")
        if self.phase != "placing":
            raise PhaseError("bad_phase", "placement is closed")
        self.boards[index].place_fleet(placements, allow_touch=allow_touch)

    def both_placed(self) -> bool:
        return all(board.placed for board in self.boards)

    def start_battle(self, starter: int = 0) -> None:
        if not self.both_placed():
            raise PhaseError("not_ready", "both players must place their fleet first")
        self.phase = "battle"
        self.starter = starter % 2
        self.turn = self.starter
        self.log.append({"ts": _now_ms(), "kind": "start", "first": self.turn})

    # -- battle -------------------------------------------------------------- #
    def fire(self, index: int, r: int, c: int) -> dict:
        if self.phase != "battle":
            raise PhaseError("bad_phase", f"cannot fire during {self.phase}")
        if index not in (0, 1):
            raise PhaseError("bad_player", f"bad player index {index}")
        if index != self.turn:
            raise ShotError("not_your_turn", "it is not your turn")

        target = self.boards[1 - index]
        result = target.fire(r, c)
        result["by"] = index
        result["ts"] = _now_ms()
        self.shots_fired[index] += 1
        self.log.append(
            {
                "ts": result["ts"],
                "kind": "shot",
                "by": index,
                "r": r,
                "c": c,
                "hit": result["hit"],
                "sunk": result["sunk"],
                "gun": self.shots_fired[index],
            }
        )
        if result["sunk"]:
            self.log.append(
                {
                    "ts": result["ts"],
                    "kind": "sunk",
                    "by": index,
                    "ship": result["sunk"],
                    "cells": result["sunk_cells"],
                }
            )

        if result["game_over"]:
            self.phase = "over"
            self.winner = index
            result["winner"] = index
            self.log.append({"ts": result["ts"], "kind": "over", "winner": index})
        elif not result["hit"]:
            self.turn = 1 - index
            result["turn"] = self.turn
        else:
            result["turn"] = index
        self.last_result = result
        return result

    def forfeit(self, loser: int) -> dict:
        """The player at *loser* left the match; the other one wins."""
        if self.phase in ("battle", "placing"):
            self.phase = "over"
            self.forfeited = True
            self.winner = 1 - loser
            entry = {
                "ts": _now_ms(),
                "kind": "forfeit",
                "loser": loser,
                "winner": self.winner,
            }
            self.log.append(entry)
            return entry
        return {}

    # -- views --------------------------------------------------------------- #
    def view_for(self, index: int) -> dict:
        own = self.boards[index]
        enemy = self.boards[1 - index]
        return {
            "your_board": own.to_dict(),
            "enemy": enemy.enemy_view(),
            "turn": self.turn,
            "phase": self.phase,
            "winner": self.winner,
            "forfeit": self.forfeited,
            "starter": self.starter,
        }


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


# --------------------------------------------------------------------------- #
# Coordinate helpers shared by the UI
# --------------------------------------------------------------------------- #

def cell_name(r: int, c: int) -> str:
    """Human coordinate, e.g. (0, 0) -> 'A1'."""
    return f"{chr(ord('A') + c)}{r + 1}"


def parse_cell(text: str) -> tuple[int, int]:
    """Parse 'A1' / 'j10' (case-insensitive) into (r, c)."""
    text = text.strip().upper()
    if len(text) < 2 or not text[0].isalpha() or not text[1:].isdigit():
        raise ValueError(f"bad coordinate: {text!r}")
    c = ord(text[0]) - ord("A")
    r = int(text[1:]) - 1
    if not in_bounds(r, c):
        raise ValueError(f"coordinate outside board: {text!r}")
    return r, c
