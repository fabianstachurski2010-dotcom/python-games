#!/usr/bin/env python3
"""Battleship LAN — entry point.

A two-player Battleship game for the local network (Python + Tkinter, stdlib
only).  One player hosts a room; the other joins with a 4-digit room code that
is discovered by UDP broadcast on the LAN (or by typing the host IP manually).
A single-player AI practice mode is included too.

Usage
    python3 battleship.py                     # GUI, main menu
    python3 battleship.py --host [--port N]   # open a room right away
    python3 battleship.py --join CODE [--ip HOST] [--port N]
    python3 battleship.py --ai [--difficulty easy|normal|hard|learned]
    python3 battleship.py --watch             # watch AI vs AI / AI vs bot
    python3 battleship.py --lang zh|en
    python3 battleship.py --selftest          # run every test + headless LAN check
    python3 battleship.py --simulate 200      # AI vs AI statistics, no GUI
    python3 battleship.py --simulate 200 --difficulty learned --opponent hard

Everything runs from this directory; the module bootstrap below also makes
``python3 games-python/battleship/battleship.py`` work from the repo root.
"""

from __future__ import annotations

import argparse
import gc
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import engine  # noqa: E402
import i18n  # noqa: E402


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="battleship.py",
        description="Battleship over the local network (Tkinter, stdlib only).",
    )
    parser.add_argument("--host", action="store_true", help="open a room and wait for an opponent")
    parser.add_argument("--join", metavar="CODE", help="join a room by its 4-digit code")
    parser.add_argument("--ai", action="store_true", help="play against the computer")
    parser.add_argument("--watch", action="store_true",
                        help="watch two computer players (AI vs AI, AI vs bot)")
    parser.add_argument("--ip", metavar="HOST", help="host IP when joining manually")
    parser.add_argument("--port", type=int, metavar="PORT", help="fixed TCP port (host) / port to dial (join)")
    parser.add_argument(
        "--difficulty",
        choices=("easy", "normal", "hard", "learned"),
        default="normal",
        help="AI difficulty for --ai / --simulate (default: normal; "
             "learned needs numpy + battleship_model.npz from ai_train.py)",
    )
    parser.add_argument(
        "--opponent",
        choices=("easy", "normal", "hard", "learned"),
        default="easy",
        help="the other side in --simulate (default: easy)",
    )
    parser.add_argument("--lang", choices=("zh", "en"), help="UI language (default: last used)")
    parser.add_argument("--selftest", action="store_true", help="run the test suite and a headless LAN game")
    parser.add_argument(
        "--simulate",
        type=int,
        metavar="N",
        nargs="?",
        const=100,
        help="play N seeded AI-vs-AI games and print stats (default 100)",
    )
    parser.add_argument("--version", action="version", version="battleship 1.0")
    return parser


# --------------------------------------------------------------------------- #
# AI vs AI simulation
# --------------------------------------------------------------------------- #

def _available_cells_strategy(board: engine.Board) -> tuple[int, int]:
    """Trivial fallback strategy used when ai.py is unavailable."""
    cells = board.available_cells()
    return cells[0] if cells else (0, 0)


def simulate(games: int, difficulty: str = "normal", opponent: str = "easy") -> int:
    """Play seeded AI-vs-AI matches and print a win-rate table."""
    try:
        from ai import BattleshipAI
    except Exception as exc:  # pragma: no cover - only during partial builds
        print(f"ai.py is not ready: {exc}")
        return 1
    if games <= 0:
        print("nothing to simulate")
        return 1
    for kind in {difficulty, opponent}:
        try:
            BattleshipAI(kind)
        except Exception as exc:  # noqa: BLE001 - e.g. learned model not trained yet
            print(f"cannot create the {kind} AI: {exc}")
            return 1

    mirror = difficulty == opponent
    wins = {difficulty: 0, "draw": 0} if mirror else {difficulty: 0, opponent: 0, "draw": 0}
    shots_total = 0
    start = time.time()
    for seed in range(games):
        game = engine.Game()
        game.place_fleet(0, engine.random_fleet())
        game.place_fleet(1, engine.random_fleet())
        game.start_battle(starter=seed % 2)
        ais = [
            BattleshipAI(difficulty if seed % 2 == 0 else opponent, seed=seed * 2 + 1),
            BattleshipAI(opponent if seed % 2 == 0 else difficulty, seed=seed * 2 + 2),
        ]
        # ais[0] plays the difficulty under test when seed is even; track who is who.
        who = [difficulty, opponent] if seed % 2 == 0 else [opponent, difficulty]
        guard = 0
        while game.phase == "battle" and guard < 500:
            guard += 1
            player = game.turn
            if player is None:
                break
            board = game.boards[1 - player]
            try:
                r, c = ais[player].choose_attack(board)
            except engine.ShotError:
                r, c = _available_cells_strategy(board)
            game.fire(player, r, c)
            shots_total += 1
        if game.phase != "over":
            wins["draw"] += 1
            continue
        winner_name = who[game.winner] if game.winner is not None else "draw"
        if winner_name in wins:
            wins[winner_name] += 1
        else:
            wins["draw"] += 1

    elapsed = time.time() - start
    print(f"AI vs AI — {games} games, {difficulty} vs {opponent}")
    if mirror:
        print("  (same AI on both sides: wins are shared, see the draw/avg lines)")
    else:
        print(f"  {difficulty:<7} wins: {wins[difficulty]:>4}  ({wins[difficulty] / games:.1%})")
        print(f"  {opponent:<7} wins: {wins[opponent]:>4}  ({wins[opponent] / games:.1%})")
    print(f"  draws:          {wins['draw']:>4}")
    print(f"  avg shots/game: {shots_total / games:6.1f}   ({elapsed:.1f}s)")
    return 0


# --------------------------------------------------------------------------- #
# Headless LAN integration check (used by --selftest)
# --------------------------------------------------------------------------- #

def _wait_for(client, predicate, timeout: float = 8.0) -> dict:
    """Drain *client* until predicate(sync) holds; return the matching sync."""
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        for message in client.poll():
            if message.get("t") == "sync":
                last = message
                if predicate(message):
                    return message
            elif message.get("t") == "closed":
                raise RuntimeError(f"connection closed: {message.get('reason')}")
        time.sleep(0.005)
    raise TimeoutError(f"timed out waiting for sync (last state: {last.get('state')!r})")


def _pump_views(clients, views, duration: float = 0.3) -> bool:
    """Drain *clients* for up to *duration* seconds, recording their latest syncs."""
    deadline = time.time() + duration
    seen = False
    while time.time() < deadline:
        for client in clients:
            for message in client.poll():
                kind = message.get("t")
                if kind == "sync":
                    views[client] = message
                    seen = True
                elif kind == "closed":
                    raise RuntimeError(f"connection closed: {message.get('reason')}")
                elif kind == "error":
                    raise RuntimeError(f"server error: {message.get('code')}")
        if seen:
            return True
        time.sleep(0.005)
    return seen


def _wait_condition(clients, views, predicate, timeout: float = 3.0) -> None:
    """Pump until predicate(views) is true (views maps client -> latest sync)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate(views):
            return
        _pump_views(clients, views, 0.1)
    if predicate(views):
        return
    raise TimeoutError("timed out waiting for game state")


def selftest_network(verbose: bool = True) -> bool:
    """Start a real server + two clients on 127.0.0.1 and play a full game."""
    import net_client
    import net_server

    server = net_server.GameServer(tcp_port=0, discovery=False)
    a = net_client.NetClient()
    b = net_client.NetClient()
    clients = (a, b)
    views: dict = {}
    try:
        code, port = server.start()
        if verbose:
            print(f"  loopback room {code} on 127.0.0.1:{port}")
        a.connect("127.0.0.1", port, "Tester-A")
        b.connect("127.0.0.1", port, "Tester-B")
        if verbose:
            print(f"  handshake ok (slots {a.you}/{b.you}, {server.client_count()} clients)")

        a.send_place(engine.random_fleet())
        b.send_place(engine.random_fleet())
        _wait_condition(clients, views,
                        lambda v: a in v and v[a].get("state") == "battle", 10.0)
        sync = views[a]
        if not sync.get("your_board", {}).get("ships"):
            raise AssertionError("battle started without a fleet")
        if verbose:
            print("  placement -> battle ok")

        # Drive the game from slot a's snapshot only: both players' shot sets
        # live in it (your_board.shots = opponent's shots, enemy.shots = ours),
        # so a slow reader thread on b can never stall the self-test.
        def _taken(view: dict, player: int) -> set:
            if player == 0:
                return {(int(r), int(c)) for r, c, _ in view["enemy"]["shots"]}
            return {(int(r), int(c)) for r, c, _ in view["your_board"]["shots"]}

        def _confirmed(v: dict, turn: int, before: int) -> bool:
            latest = v.get(a)
            if not latest:
                return False
            if latest.get("state") == "over":
                return True
            shots = (latest.get("stats") or {}).get("shots") or [0, 0]
            return latest.get("state") == "battle" and shots[turn] > before

        rng = random.Random(0xB4771E)
        for _ in range(400):
            _pump_views(clients, views, 0.05)
            sa = views.get(a)
            if not sa:
                raise AssertionError("no sync received from the server")
            state = sa.get("state")
            if state == "over":
                break
            if state != "battle":
                raise AssertionError(f"unexpected state {state!r}")
            turn = sa.get("turn")
            if turn not in (0, 1):
                raise AssertionError("battle without a turn")
            client = a if turn == 0 else b
            open_cells = [
                (r, c)
                for r in range(engine.SIZE)
                for c in range(engine.SIZE)
                if (r, c) not in _taken(sa, turn)
            ]
            if not open_cells:
                raise AssertionError("no cells left to fire at")
            cell = rng.choice(open_cells) if len(open_cells) > 20 else open_cells[0]
            before = sa["stats"]["shots"][turn]
            client.send_fire(cell[0], cell[1])
            _wait_condition(
                clients, views,
                lambda v, n=before, t=turn: _confirmed(v, t, n),
                12.0,
            )

        _wait_condition(clients, views,
                        lambda v: a in v and v[a].get("state") == "over", 10.0)
        winner = views[a].get("winner")
        deadline = time.time() + 10.0
        while time.time() < deadline:
            vb = views.get(b)
            if vb and vb.get("state") == "over" and vb.get("winner") == winner:
                break
            _pump_views(clients, views, 0.05)
        sync = views[a]
        winner = sync.get("winner")
        if winner not in (0, 1):
            raise AssertionError(f"game over without winner: {winner!r}")
        if b not in views or views[b].get("winner") != winner:
            raise AssertionError("clients disagree on the winner")
        if verbose:
            print(f"  full game ok — winner is player {winner} after "
                  f"{sync['stats']['shots']} shots")
        return True
    finally:
        for client in (a, b):
            try:
                client.leave()
            except Exception:
                pass
            try:
                client.close()
            except Exception:
                pass
        try:
            server.stop()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# --selftest
# --------------------------------------------------------------------------- #

def selftest() -> int:
    print("Battleship self-test\n" + "=" * 60)
    failures = 0

    print("\n[1/2] unit tests")
    try:
        import unittest

        loader = unittest.TestLoader()
        suite = loader.discover(os.path.join(HERE, "tests"), top_level_dir=HERE)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        if not result.wasSuccessful():
            failures += 1
    except Exception as exc:
        print(f"  test discovery failed: {exc}")
        failures += 1

    # Tkinter fonts/frames are Tcl-backed and finalise through Tcl.  The UI
    # tests above leave some of them to be collected later; if a *network
    # reader thread* happens to trigger that finalisation the Tcl lock can
    # deadlock on macOS.  Collect them here, on the main thread, first.
    for _ in range(3):
        gc.collect()

    print("\n[2/2] headless LAN integration")
    try:
        if not selftest_network():
            failures += 1
    except Exception as exc:
        print(f"  LAN integration failed: {exc}")
        failures += 1

    print("\n" + "=" * 60)
    if failures:
        print(f"SELFTEST FAILED ({failures} problem(s))")
        return 1
    print("SELFTEST PASSED")
    return 0


# --------------------------------------------------------------------------- #
# GUI launcher
# --------------------------------------------------------------------------- #

def run_gui(args: argparse.Namespace) -> int:
    if args.lang:
        i18n.set_lang(args.lang)
        i18n.save_lang()
    else:
        i18n.set_lang(i18n.load_saved_lang())

    try:
        import ui
    except Exception as exc:
        print(f"could not load the UI: {exc}")
        print("If Tkinter is missing, install your distribution's python3-tk package.")
        return 1

    mode = "menu"
    if args.host:
        mode = "host"
    elif args.join:
        mode = "join"
    elif args.ai:
        mode = "ai"
    elif args.watch:
        mode = "watch"

    ui.launch(
        mode=mode,
        code=args.join,
        ip=args.ip,
        port=args.port,
        ai_difficulty=args.difficulty,
    )
    return 0


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()
    if args.simulate is not None:
        return simulate(args.simulate, args.difficulty, args.opponent)
    if args.host and args.join:
        parser.error("--host and --join are mutually exclusive")
    return run_gui(args)


if __name__ == "__main__":
    sys.exit(main())
