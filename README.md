# Python Games

Eight small, self-contained games — one folder each, no build step, no assets to
download. Everything is **Python 3.10+** and either **Tkinter** (standard library)
or **pygame**. Every game has a `--selftest` mode that runs its logic tests
headlessly, and CI runs all eight on every push.

| Folder | Game | What it is | Needs |
|---|---|---|---|
| [`battleship/`](battleship/) | Battleship LAN 海戰棋 | 10×10 battleship — a friend on the same Wi-Fi (UDP discovery) or a three-level AI | Tkinter |
| [`chomp/`](chomp/) | Chomp 迷宮追擊 | Maze-chase arcade: clear the maze, four ghosts with distinct AI, power pellets and fruit | Tkinter |
| [`connect-four/`](connect-four/) | Connect Four | Four in a row over the LAN or against a three-level AI (one level is a trained net) | pygame |
| [`flappy/`](flappy/) | Flappy | Flap through the pipes; three difficulty presets | Tkinter |
| [`pong/`](pong/) | Pong | Classic Pong against the computer, or two players on one keyboard | Tkinter |
| [`shooter/`](shooter/) | Barrage 彈幕突擊 | Vertical bullet-hell: the turret fires itself, you dodge, graze bullets, fight bosses | Tkinter (optional pygame sound) |
| [`snake/`](snake/) | Neon Serpent 霓虹貪食蛇 | Neon snake with combo multipliers, six fruits, screen shake, three modes | pygame, numpy |
| [`tic-tac-toe/`](tic-tac-toe/) | Tic-Tac-Toe | Unbeatable minimax, an easy random mode, or two players | Tkinter |

## Quick start

```bash
git clone https://github.com/kyle2241345145351324321/python-games.git
cd python-games/shooter
python3 shooter.py
```

Each folder has its own `README.md` with controls, difficulty presets and
keyboard shortcuts — open `README.md` next to the game you are playing.

## Requirements

- **Python 3.10+**
- **Tkinter** — included in the python.org builds for macOS/Windows; on
  Debian/Ubuntu install it with `sudo apt install python3-tk`
- **pygame** — only `snake/` and `connect-four/` need it (`python3 -m pip install pygame`)
- **numpy** — only `snake/` needs it (`python3 -m pip install numpy`)

Everything else is the standard library. There is nothing to compile and no
server to start; the two LAN games (`battleship`, `connect-four`) let one player
host and the other join automatically on the same network.

## Run the tests

Every game can test itself without opening a window:

```bash
cd pong   && python3 pong.py   --selftest
cd snake  && python3 snake.py  --selftest
cd chomp  && python3 chomp.py  --selftest
```

The other five work the same way (`battleship.py`, `connect_four.py`,
`flappy.py`, `shooter.py`, `tic_tac_toe.py`). `connect-four` and `battleship`
also ship conventional unittest suites, which is what their `--selftest` runs.

## How the games are built

The Tkinter games share the same three-layer shape, which is why they can test
themselves headlessly:

1. **Pure logic** — a plain class (`PongGame`, `ChompGame`, `ShooterGame`, …)
   that owns the physics and rules and never touches a widget.
2. **GUI** — a thin `*GUI` class that draws that state onto one Canvas and feeds
   key presses back in.
3. **Bot / simulation** — a built-in player plus `--simulate SECONDS` /
   `--text`, so the game can play itself with no window at all.

Several games also ship small trained models (`*_model.npz`): `*_train.py` trains
them and `*_play.py` replays or benchmarks them. Nothing at play time needs a
training stack.

## Repository layout

```text
battleship/     battleship.py      engine, AI, LAN server/client, unittest suite
chomp/          chomp.py           one-file game + selftest
connect-four/   connect_four.py    engine, AI, LAN, records, unittest suite
flappy/         flappy.py          one-file game + train/play helpers
pong/           pong.py            one-file game + train/play helpers
shooter/        shooter.py         one-file game + train/play helpers
snake/          snake.py           one-file game (pygame) + selftest
tic-tac-toe/    tic_tac_toe.py     one-file game + train/play helpers
```

## License

MIT — see [LICENSE](LICENSE).
