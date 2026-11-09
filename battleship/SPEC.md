# Battleship LAN — build spec (v1, frozen contracts)

A two-player Battleship game for the local network: Python 3.10 + Tkinter, **standard
library only** (no pip installs). One player hosts a room; the other joins with a 4-digit
room code that is discovered over UDP broadcast, or by typing the host IP. Also includes a
single-player AI practice mode.

Project root: `games-python/battleship/` (run everything from this directory).

```
battleship.py    entry point: menu / --host / --join / --ai / --selftest / --simulate   (Lead)
engine.py        rules engine — FROZEN, do not edit                                       (Lead)
i18n.py          bilingual strings — complete; uidev may only append new keys            (Lead/uidev)
ai.py            BattleshipAI                                                            (aidev)
net_server.py    GameServer + UDP discovery responder                                    (netdev)
net_client.py    NetClient + UDP discovery probe                                         (netdev)
ui.py            Tkinter app (all screens)                                               (uidev)
tests/           unittest files, one per module                                          (owner per file)
README.md        usage + troubleshooting                                                 (Lead)
```

Ownership rule: **only edit the files listed under your name.** Never edit another
module. If a contract is not enough, message the Lead instead of changing someone
else's file. `i18n.py` is Lead-owned but uidev may append missing keys (both languages)
at the end of `STRINGS`; do not rename or remove existing keys.

Python target: 3.10, `from __future__ import annotations` at the top of every module.
No third-party imports, no `asyncio`, no external assets. Threading is fine (stdlib).

---

## 1. Rules / engine API (already implemented in engine.py)

```python
SIZE = 10
FLEET = (("carrier",5), ("battleship",4), ("cruiser",3), ("submarine",3), ("destroyer",2))
TOTAL_SHIP_CELLS = 17

ship_cells(size, r, c, horizontal) -> tuple[(r,c), ...]      # raises PlacementError
random_fleet(rng=None, allow_touch=True) -> list[{"name": str, "cells": [[r,c],...]}]
validate_placement(placements, allow_touch=True) -> list[Ship]  # raises PlacementError

class Ship:  .name .cells .hits .size .sunk .afloat .to_dict()
class Board:
    .ships .shots .placed .all_sunk .sunk_ships .remaining_cells
    .place_fleet(placements) .random_fleet(rng=None) -> list[dict]
    .ship_at(r,c) .available_cells() .hit_cells() .miss_cells()
    .fire(r,c) -> {"r","c","hit","sunk","sunk_cells","ship_size","game_over"}
    .to_dict() -> {"ships":[...], "shots":[[r,c,result],...]}
    .enemy_view() -> {"shots":[...], "sunk":[{"name","cells"}]}
class Game:
    .boards .phase("placing"|"battle"|"over") .turn .winner .forfeited .starter .log .shots_fired
    .place_fleet(index, placements) .both_placed() .start_battle(starter=0)
    .fire(index, r, c) -> dict (result + "by","ts","turn", and "winner" when over)
    .forfeit(loser) -> dict     # loser index, sets phase over + winner
    .view_for(index) -> dict
cell_name(r,c) -> "A1" ; parse_cell("A1") -> (r,c)
```

Rule decisions locked in: 10×10; ships may touch but not overlap; a **hit grants another
shot**, a miss passes the turn; first fleet fully sunk loses. Coordinates: columns A–J
(left→right), rows 1–10 (top→bottom), internally `r`=row 0..9, `c`=column 0..9.
`engine.Game.shots_fired[i]` counts shots by player i.

## 2. Language strings (i18n.py)

`t(key, **fmt)`, `set_lang("zh"|"en")`, `get_lang()`, `toggle_lang()`,
`language_name()`, `ship_name(name)`, `ship_label(name)`, `err_text(code)`,
`load_saved_lang()`, `save_lang()`. All UI text must go through `t()`; no hard-coded
Chinese/English in widget labels. Add missing keys to **both** `zh` and `en` dicts.

## 3. Network protocol (netdev, consumed by uidev)

Authoritative server model: the host process runs `GameServer`, which owns the `Game`,
validates every action, and broadcasts a **full state snapshot** (`sync`) to both clients
after every change. Clients are thin: send intents, render the last snapshot.

### 3.1 TCP wire format
- TCP on `0.0.0.0`, port chosen by the OS unless a fixed port is requested.
- One JSON object per line, UTF-8 (no BOM), newline-terminated. Max 64 KiB per line;
  ignore/close on oversized or malformed input. Non-dict JSON is malformed.
- Missing `"t"` or unknown `"t"` → `error` with code `bad_message`.

### 3.2 Client → server

| message | payload | notes |
|---|---|---|
| `{"t":"hello","name":"Kyle","ver":1}` | first message; nickname 1–16 chars, trimmed | server replies `welcome`; errors: `room_full`, `already_started`, `name_too_long` |
| `{"t":"place","ships":[{"name":"carrier","cells":[[0,0],...]}, ...]}` | full fleet, exactly the 5 classic ships | validates via `engine.validate_placement`; sets that player's `ready=True`; errors: `bad_placement`, `wrong_fleet`, `overlap`, `bad_phase` |
| `{"t":"fire","r":3,"c":4}` | ints 0..9 | only when state `battle` and it is your turn; errors: `not_your_turn`, `already_shot`, `out_of_bounds`, `bad_phase` |
| `{"t":"chat","text":"gg"}` | ≤ 200 chars, trimmed; empty ignored | broadcast in next `sync.chat` |
| `{"t":"rematch"}` | records vote | when both players vote, server resets to `placing`; starter alternates |
| `{"t":"lobby"}` | only after `over` when the opponent is gone | clears the empty slot → state `lobby`, waiting for a new opponent |
| `{"t":"leave"}` | graceful exit | treated as disconnect |
| `{"t":"ping"}` | keepalive | server replies `{"t":"pong"}` |

### 3.3 Server → client

- `{"t":"welcome","you":0,"code":"4821","name":"Kyle","ver":1}` — sent once after `hello`;
  `you` is the player slot (0 or 1).
- `{"t":"sync", ...}` — **the complete view**, sent to each client after every accepted
  action and whenever the state changes. Schema:

```json
{
  "t": "sync",
  "state": "lobby | placing | battle | over",
  "you": 0,
  "code": "4821",
  "players": [
    {"name": "Kyle", "ready": false, "connected": true},
    null
  ],
  "wins": [0, 0],
  "starter": 0,
  "turn": 0,
  "your_board": {
    "ships": [{"name": "carrier", "cells": [[0,0], "..."], "hits": [[0,0]], "sunk": false}],
    "shots": [[3,4,"hit"]]
  },
  "enemy": {
    "shots": [[0,0,"miss"]],
    "sunk": [{"name": "destroyer", "cells": [[1,1],[1,2]]}]
  },
  "stats": {"shots": [12, 9], "hits": [5, 3]},
  "log": [{"kind": "..."}],
  "chat": [{"ts": 1699999999999, "from": "Kyle", "text": "hi"}],
  "winner": null,
  "forfeit": false,
  "rematch": [false, false],
  "notice": null
}
```

  * `players`: length-2 array, `null` = empty slot. `connected` false only in `over`
    after a forfeit.
  * `your_board.ships` is `[]` before you place. `your_board.shots` are shots **by the
    opponent onto your board**. `enemy.shots` are your shots. `enemy.sunk` lists enemy
    ships you have sunk (name + cells).
  * `stats.shots[i]` = shots fired by player i, `stats.hits[i]` = their hits.
  * `log` (newest last, keep ≤ 300): kinds
    `{"kind":"join","name":...}`, `{"kind":"leave","name":...}`,
    `{"kind":"start","first":0}`,
    `{"kind":"shot","by":0,"r":3,"c":4,"hit":true,"sunk":null,"gun":7}`,
    `{"kind":"sunk","by":0,"ship":"destroyer","cells":[[r,c],...]}`,
    `{"kind":"over","winner":0}`, `{"kind":"forfeit","loser":1,"winner":0}`,
    `{"kind":"rematch"}`, `{"kind":"system","code":"...","name":"..."}`.
  * `chat` newest last, keep ≤ 100, `from` = nickname or `null` for system messages.
  * `notice` is `null`, `"opponent_left"`, `"new_opponent"` or `"rematch"` and only
    changes between syncs (one-shot toast hint).
- `{"t":"error","code":"not_your_turn","message":"..."}` — `code` is what the UI localises
  via `i18n.err_text(code)`; `message` is an English developer hint.
- `{"t":"pong"}`
- `{"t":"bye","reason":"host_left"}` then close when the server shuts down.

### 3.4 Room lifecycle / state machine

- Room holds exactly **2** players. Join order → slot 0, slot 1. Host player uses a
  normal `NetClient` connected to `127.0.0.1:<port>`.
- `lobby`: 1 connected player. Second join → `placing`, `Game` reset, log `join`,
  `notice:"new_opponent"`.
- `placing`: both clients place; when both `ready` → `battle` (`Game.start_battle`,
  starter alternates every new match: match 1 → 0, match 2 → 1, …), `log start`.
- `battle`: shots resolve through `Game.fire`; broadcast `sync` after each.
- `over`: winner recorded; both connected → rematch votes; both voted → `placing` with
  fresh boards, starter alternates, wins incremented for winner, chat kept, log kept +
  `{"kind":"rematch"}`. If the opponent disconnected: remaining player may send `lobby`
  (slot cleared, state `lobby`, game reset, `wins` reset to `[0,0]` for the new match) or
  `leave`.
- Disconnect/`leave`:
  * during `placing` → opponent returns to `lobby` (game reset, `wins` reset), slot freed;
  * during `battle` → `Game.forfeit(loser)` → `over`; the disconnected slot stays listed
    with `connected:false`, `winner` set, `notice:"opponent_left"`;
  * during `lobby` → slot freed (already empty);
  * during `over` → slot freed; if game not yet cleared, remaining player stays on the
    over screen and can `lobby`/`rematch` (rematch with no opponent is rejected with
    `bad_phase`); server also auto-resets to `lobby` after a `leave` in `over`.
- Server must survive one client crashing; never crash the host process on bad input.
- Reject a third connection with `error code room_full` then close.

### 3.5 Discovery (UDP)

Shared constants in **net_client.py** (imported by net_server to avoid circular imports):

```python
DISCOVERY_PORT = 54321
DISCOVERY_PROBE = b'{"t":"battleship.discover","ver":1}'
```

- Server binds UDP `0.0.0.0:DISCOVERY_PORT` when `discovery=True`; on bind failure keep
  serving TCP with discovery disabled (log a warning, no crash).
- Probe (client broadcast) ➜ responder sends JSON to the probe's source address:
  `{"t":"battleship.room","code":"4821","port":54321,"host":"192.168.1.5",
    "names":["Kyle"],"state":"lobby","ver":1}`; `port` is the TCP port.
- `net_client.discover_rooms(timeout=1.2, udp_port=DISCOVERY_PORT)` broadcasts to
  `255.255.255.255`, to `127.0.0.1` (same machine) and to the subnet broadcast derived
  from the local LAN IP (assume /24: `a.b.c.255`), then collects responses for *timeout*
  seconds. Returns a de-duplicated list of dicts `{"code","host","port","names","state"}`.
  Errors/blocked UDP must return `[]`, never raise.

### 3.6 Public APIs

`net_client.py`

```python
class NetError(Exception):
    code: str          # e.g. "connect_failed", "room_full", "connect_timeout"

class NetClient:
    def __init__(self) -> None
    def connect(self, host: str, port: int, name: str, timeout: float = 5.0) -> None
        # connects, sends hello, waits for welcome (raises NetError on failure/timeout)
    @property
    def connected(self) -> bool
    code: str | None ; you: int | None
    def send_place(self, ships: list[dict]) -> None
    def send_fire(self, r: int, c: int) -> None
    def send_chat(self, text: str) -> None
    def send_rematch(self) -> None
    def send_lobby(self) -> None
    def leave(self) -> None        # send leave if connected, then close
    def close(self) -> None        # idempotent, joins reader thread
    def poll(self) -> list[dict]   # non-blocking drain; includes server msgs and
                                   # {"t":"closed","reason":"..."} when the reader ends
def discover_rooms(timeout: float = 1.2, udp_port: int = DISCOVERY_PORT) -> list[dict]
```

`net_server.py`

```python
class GameServer:
    def __init__(self, tcp_port: int = 0, discovery: bool = True, verbose: bool = False)
    code: str          # 4-digit string, stable for the room
    port: int          # actual TCP port (0 until start)
    lan_ip: str        # best-effort LAN IPv4, "" if unknown
    running: bool
    def start(self) -> tuple[str, int]   # (code, port); raises OSError if TCP bind fails
    def stop(self) -> None               # closes everything, joins threads ≤ 3 s, idempotent
    def client_count(self) -> int
    def __enter__(self); def __exit__(...)
```

`GameServer` must have no Tkinter import and no printing on the hot path (only when
`verbose=True`). Tests instantiate it with `discovery=False` and `127.0.0.1`.

## 4. AI (aidev)

```python
class BattleshipAI:
    DIFFICULTIES = ("easy", "normal", "hard")
    def __init__(self, difficulty: str = "normal", seed: int | None = None)
    def choose_placement(self) -> list[dict]      # engine fleet format, always legal
    def choose_attack(self, board: engine.Board) -> tuple[int, int]
```

- `choose_attack` receives the **opponent's board** and may only use public knowledge:
  `board.shots`, `board.sunk_ships` (sunk ships are announced in the real game),
  `board.available_cells()`, `board.hit_cells()`. **It must not read `board.ships`
  positions of afloat ships** — that is cheating.
- easy: uniformly random available cell (plus occasional adjacent guess).
- normal: hunt random/parity cells, then target the 4-neighbours of unresolved hits.
- hard: probability-density over all remaining legal ship placements weighted by shots,
  choose the most likely cell; tie-break with parity. Must answer in < 50 ms.
- No repeats, no out-of-bounds; raise `engine.ShotError("no_moves", ...)` only when the
  board is exhausted.
- `choose_placement` may use `engine.random_fleet`, optionally preferring spread-out
  layouts. Deterministic for a fixed seed.

## 5. Tkinter UI (uidev)

One module, one window. Public API:

```python
class BattleshipApp:
    def __init__(self, root=None, mode: str = "menu", code=None, ip=None,
                 port=None, lang=None, ai_difficulty="normal")
    def run(self) -> None

def launch(mode="menu", code=None, ip=None, port=None, lang=None,
           ai_difficulty="normal") -> None      # creates Tk root + app.run()
```

`mode` ∈ `"menu" | "host" | "join" | "ai"`. `launch()` is what `battleship.py` calls.
The UI owns a `GameServer` only in host mode (`GameServer(discovery=True).start()`, then
connect a `NetClient` to `127.0.0.1:<port>` as the host player). It must stop the server
on window close / cancel / back to menu, and on `bye`.

Screens (all bilingual, all strings via `t()`):
1. **MENU** — title + tagline, three big cards (Host / Join / AI practice), help button,
   language toggle top-right, quit. Nice dark-navy theme, not stock ttk grey.
2. **HOST** — big room code (4 digits, copy button), local IP + port + "same Wi-Fi" hint,
   waiting spinner/label; Cancel → back to menu (stops server).
3. **JOIN** — nickname, 4-digit code input, discover button with status line + list of
   found rooms (click to join), manual IP/port fallback toggle, Join / Back.
4. **AI SETUP** — difficulty (easy/normal/hard), "you fire first / AI first" info, Start.
5. **PLACEMENT** — your 10×10 canvas grid with coordinates; ship dock listing the 5 ships
   with sizes; drag ships from dock to grid with a ghost preview (valid/invalid tint) or
   click ship → click cell; **R / right-click rotates**; click a placed ship to pick it up;
   Random / Clear / Ready buttons; "waiting for opponent" state after Ready; opponent
   readiness indicator; chat panel. Placement must be validated with `engine` before send.
6. **BATTLE** — two canvas grids side by side: "Your waters" (your ships + enemy hits/misses)
   and "Enemy waters" (your shots; click to fire on your turn; sunk enemy ships filled dark
   red with their shape). Turn banner, remaining enemy ship cells, your fleet status,
   **chat panel** and **battle log** (localised from `sync.log`, newest at bottom,
   auto-scroll), rematch overlay on `over`.
7. **HELP** — modal with the rules from `i18n` help keys.

Behaviour requirements:
- Networking never blocks the Tk loop: create `NetClient`, poll with
  `root.after(50, ...)`, render from the latest `sync`. AI runs in a worker thread and
  posts its chosen cell through a `queue.Queue`.
- Idempotent rendering: any sync can be rendered; never assume the previous screen.
- Errors from `error` messages / `NetError` → localised toast/dialog; disconnect →
  back to menu with a message (host keeps serving until it closes).
- `root.protocol("WM_DELETE_WINDOW", ...)` cleans up server + client threads.
- Window 1200×780 default, min 1024×700; grids resize with the window (min cell ~34 px
  at 1024 wide); Canvas-drawn look (rounded panels, amber accents, water-blue cells).
- Language toggle re-renders the current screen live and persists via `save_lang()`.
- Keyboard: R rotate during placement, Esc back/cancel, Enter sends chat.
- No external assets/fonts; use Tk fonts (`TkDefaultFont`) and emoji sparingly.

Smoke test (uidev writes `tests/test_ui.py`): if `tkinter.Tk()` cannot start
(no display), `skipTest`; otherwise build the app in menu mode, call `root.update()`,
assert screens/widgets exist, then destroy. No modal dialogs.

## 6. Entry point (Lead)

`battleship.py` (in this directory) supports:

```
python3 battleship.py                 # GUI, main menu
python3 battleship.py --host [--port N]
python3 battleship.py --join CODE [--ip HOST] [--port N]
python3 battleship.py --ai [--difficulty easy|normal|hard]
python3 battleship.py --lang zh|en
python3 battleship.py --selftest      # all tests + headless network integration
python3 battleship.py --simulate 200  # AI-vs-AI stats, no GUI
```

`sys.path` bootstrapping so it also works when launched from the workspace root.

## 7. Testing & definition of done

- `tests/test_engine.py` (Lead), `tests/test_ai.py` (aidev), `tests/test_net.py` (netdev),
  `tests/test_ui.py` (uidev) — plain `unittest`; each file bootstraps `sys.path` to the
  project directory so `python3 tests/test_net.py` works from anywhere.
- Run from the project directory:
  `python3 -m unittest discover -s tests -t . -v`
  (or `python3 battleship.py --selftest` once the Lead wires the runner).
- Net tests must be self-contained: start `GameServer(tcp_port=0, discovery=False)` on
  127.0.0.1, connect real `NetClient`s, play a full game with short timeouts, always
  `stop()`/`close()` in `tearDown`. Target: whole suite < 30 s.
- Module done = tests pass + no files outside your scope modified + `python3 -m py_compile`
  clean for your module.
- Report to Lead (send_message) with: files written, test command + pass counts, any
  contract deviation, and anything the Lead must wire up.

## 8. Style

Match the rest of `games-python/`: module docstring explaining usage, section banners
(`# ---- #`), type hints with `from __future__ import annotations`, docstrings on public
functions, no dead code, no global mutable state beyond module constants, all UI text via
`i18n.t()`. Keep the code readable — this is a teaching-quality game project.
