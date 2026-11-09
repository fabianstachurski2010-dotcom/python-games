#!/usr/bin/env python3
"""Pong — you against the computer (or a friend), first to 5.

GUI controls
    Up / Down, W / S     move your paddle (left side)
    Mouse                move your paddle with the mouse
    Space                serve the ball / start the next point
    P                    pause
    N / F2               new match
    1 / 2 / 3            easy / normal / hard AI
    T                    toggle two-player mode

Two-player mode: left paddle = W / S, right paddle = Up / Down.
The AI is capped at a fixed speed (pixels per frame), so it stays beatable.

Other modes
    --selftest           run the physics/rule tests and exit
    --simulate SECONDS   run a headless AI-vs-AI match and print the result
"""

from __future__ import annotations

import argparse
import math
import random
import sys

# AI paddle speed, in pixels per frame at 60 frames per second
DIFFICULTIES = {"easy": 3.0, "normal": 4.2, "hard": 6.0}

try:  # tkinter is optional: --selftest and --simulate need no display
    import tkinter as tk

    TK_AVAILABLE = True
except ImportError:  # pragma: no cover
    TK_AVAILABLE = False


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# --------------------------------------------------------------------------- #
# Game logic — no UI code in here, so it is fully testable.
# --------------------------------------------------------------------------- #


class PongGame:
    """Frame-based physics: one step() is one 1/60 s frame, speeds are px/frame."""

    WIDTH, HEIGHT = 560, 360
    PADDLE_W, PADDLE_H = 10, 80
    BALL_SIZE = 8
    PADDLE_SPEED = 5.6
    BALL_SPEED = 5.6
    SPEEDUP = 1.07
    MAX_BALL_VX = 12.0
    MAX_BALL_VY = 7.0
    SERVE_VY = 2.5
    WIN_SCORE = 5

    def __init__(self, rng: random.Random | None = None, ai_speed: float = 4.2,
                 win_score: int | None = None, two_player: bool = False):
        self.rng = rng or random.Random()
        self.ai_speed = ai_speed
        self.win_score = win_score or self.WIN_SCORE
        self.two_player = two_player
        self.reset()

    # -- setup ------------------------------------------------------------- #

    def reset(self) -> None:
        self.left_y = self.HEIGHT / 2 - self.PADDLE_H / 2
        self.right_y = self.left_y
        self.ball_x = self.WIDTH / 2
        self.ball_y = self.HEIGHT / 2
        self.ball_vx = 0.0
        self.ball_vy = 0.0
        self.score_left = 0
        self.score_right = 0
        self.serving = True
        self.over = False
        self.winner: str | None = None
        self.rally = 0
        self.longest_rally = 0

    @property
    def ball_radius(self) -> float:
        return self.BALL_SIZE / 2

    @property
    def score_text(self) -> str:
        return f"{self.score_left} : {self.score_right}"

    # -- serving and scoring ----------------------------------------------- #

    def serve(self) -> list[str]:
        """Send the ball off the centre; serves toward whoever is behind."""
        if self.over or not self.serving:
            return []
        if self.score_left == self.score_right:
            direction = 1 if self.rng.random() < 0.5 else -1
        else:
            direction = 1 if self.score_left > self.score_right else -1
        self.ball_x, self.ball_y = self.WIDTH / 2, self.HEIGHT / 2
        self.ball_vx = direction * self.BALL_SPEED
        self.ball_vy = (self.rng.random() * 2 - 1) * self.SERVE_VY
        self.serving = False
        self.rally = 0
        return ["serve"]

    def score_point(self, side: str) -> list[str]:
        if side == "left":
            self.score_left += 1
        else:
            self.score_right += 1
        self.longest_rally = max(self.longest_rally, self.rally)
        self.ball_x, self.ball_y = self.WIDTH / 2, self.HEIGHT / 2
        self.ball_vx = self.ball_vy = 0.0
        self.serving = True
        self.rally = 0
        if self.score_left >= self.win_score or self.score_right >= self.win_score:
            self.over = True
            self.winner = "left" if self.score_left > self.score_right else "right"
            return [f"win:{self.winner}"]
        return [f"score:{side}"]

    # -- one frame --------------------------------------------------------- #

    def step(self, left_dir: int = 0, right_dir: int = 0) -> list[str]:
        """Advance one frame. ``*_dir`` is -1 up, 0 still, +1 down."""
        events: list[str] = []
        if self.over:
            return events
        self._move_paddle("left", left_dir)
        if self.two_player:
            self._move_paddle("right", right_dir)
        else:
            self._move_ai()
        if self.serving:
            return events

        # Sub-step the ball so a fast ball can never skip past a paddle.
        speed = max(abs(self.ball_vx), abs(self.ball_vy))
        sub_steps = max(1, int(math.ceil(speed / self.ball_radius)))
        for _ in range(sub_steps):
            events.extend(self._advance_ball(1.0 / sub_steps))
            if self.serving or self.over:
                break
        return events

    def _advance_ball(self, factor: float) -> list[str]:
        events: list[str] = []
        radius = self.ball_radius
        self.ball_x += self.ball_vx * factor
        self.ball_y += self.ball_vy * factor

        if self.ball_y - radius < 0:
            self.ball_y = radius
            self.ball_vy = abs(self.ball_vy)
            events.append("wall")
        elif self.ball_y + radius > self.HEIGHT:
            self.ball_y = self.HEIGHT - radius
            self.ball_vy = -abs(self.ball_vy)
            events.append("wall")

        if (self.ball_vx < 0 and self.ball_x - radius <= self.PADDLE_W and self.ball_x > 0
                and self.left_y <= self.ball_y <= self.left_y + self.PADDLE_H):
            self.ball_x = self.PADDLE_W + radius
            self.ball_vx = min(abs(self.ball_vx) * self.SPEEDUP, self.MAX_BALL_VX)
            offset = self.ball_y - (self.left_y + self.PADDLE_H / 2)
            self.ball_vy = clamp(self.ball_vy + offset * 0.1, -self.MAX_BALL_VY, self.MAX_BALL_VY)
            self.rally += 1
            events.append("paddle:left")
        elif (self.ball_vx > 0 and self.ball_x + radius >= self.WIDTH - self.PADDLE_W
                and self.ball_x < self.WIDTH
                and self.right_y <= self.ball_y <= self.right_y + self.PADDLE_H):
            self.ball_x = self.WIDTH - self.PADDLE_W - radius
            self.ball_vx = -min(abs(self.ball_vx) * self.SPEEDUP, self.MAX_BALL_VX)
            offset = self.ball_y - (self.right_y + self.PADDLE_H / 2)
            self.ball_vy = clamp(self.ball_vy + offset * 0.1, -self.MAX_BALL_VY, self.MAX_BALL_VY)
            self.rally += 1
            events.append("paddle:right")

        if self.ball_x + radius < 0:
            events.extend(self.score_point("right"))
        elif self.ball_x - radius > self.WIDTH:
            events.extend(self.score_point("left"))
        return events

    # -- paddles ----------------------------------------------------------- #

    def _move_paddle(self, side: str, direction: int) -> None:
        if not direction:
            return
        if side == "left":
            self.left_y = clamp(
                self.left_y + direction * self.PADDLE_SPEED, 0, self.HEIGHT - self.PADDLE_H
            )
        else:
            self.right_y = clamp(
                self.right_y + direction * self.PADDLE_SPEED, 0, self.HEIGHT - self.PADDLE_H
            )

    def _move_ai(self) -> None:
        center = self.right_y + self.PADDLE_H / 2
        if abs(center - self.ball_y) > 2:
            direction = 1 if center < self.ball_y else -1
            self.right_y = clamp(
                self.right_y + direction * self.ai_speed, 0, self.HEIGHT - self.PADDLE_H
            )

    def tracking_direction(self, side: str = "left") -> int:
        """Which way a perfect tracker would move that paddle this frame."""
        center = (self.left_y if side == "left" else self.right_y) + self.PADDLE_H / 2
        if abs(center - self.ball_y) <= 2:
            return 0
        return 1 if center < self.ball_y else -1

    def move_paddle_to(self, y: float, side: str = "left") -> None:
        """Place a paddle so its centre sits at ``y`` (used by the mouse)."""
        top = clamp(y - self.PADDLE_H / 2, 0, self.HEIGHT - self.PADDLE_H)
        if side == "left":
            self.left_y = top
        else:
            self.right_y = top


def run_simulate(seconds: float, seed: int | None = None, ai_speed: float = 4.2,
                 win_score: int = 5, verbose: bool = True) -> PongGame:
    """Headless AI-vs-AI match — used by --simulate and the self test."""
    game = PongGame(random.Random(seed), ai_speed=ai_speed, win_score=win_score)
    game.serve()
    for frame in range(int(seconds * 60)):
        if game.over:
            break
        if game.serving:
            game.serve()
        events = game.step(left_dir=game.tracking_direction("left"))
        if verbose:
            for event in events:
                print(f"frame {frame:5d}  {event:<12} score {game.score_text}")
    return game


# --------------------------------------------------------------------------- #
# Tkinter GUI
# --------------------------------------------------------------------------- #


class PongGUI:
    BG = "#16181d"
    FG = "#e8e8e8"
    NET = "#2f3239"
    LEFT = "#5ea1ff"
    RIGHT = "#ff6b6b"
    MUTED = "#8e8e93"
    LEFT_WINS = "你贏了"
    RIGHT_WINS = "你輸了"
    TICK_MS = 16  # ~60 frames per second

    def __init__(self, ai_speed: float = 4.2, win_score: int = 5,
                 seed: int | None = None, two_player: bool = False):
        self.seed = seed
        self.win_score = win_score
        self.game = PongGame(
            random.Random(seed), ai_speed=ai_speed, win_score=win_score, two_player=two_player
        )
        self.pressed: set[str] = set()
        self.release_tokens: dict[str, str] = {}
        self.paused = False
        self.after_id: str | None = None

        self.root = tk.Tk()
        self.root.title("Pong")
        self.root.resizable(False, False)
        self.root.configure(bg=self.BG)

        self.canvas = tk.Canvas(
            self.root, width=PongGame.WIDTH, height=PongGame.HEIGHT,
            bg=self.BG, highlightthickness=0,
        )
        self.canvas.grid(row=0, column=0)
        self.root.bind("<KeyPress>", self._on_key_press)
        self.root.bind("<KeyRelease>", self._on_key_release)
        self.root.bind("<F2>", lambda _e: self.new_match())
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Button-1>", lambda _e: self.serve_or_restart())
        self.canvas.focus_set()

        self._draw()
        self._tick()

    # -- input ------------------------------------------------------------- #

    def _on_key_press(self, event) -> None:
        key = event.keysym.lower()
        token = self.release_tokens.pop(key, None)
        if token is not None:  # macOS fires KeyRelease between auto-repeats
            self.root.after_cancel(token)
        if key in {"up", "down", "w", "s"}:
            self.pressed.add(key)
        elif key == "space":
            self.serve_or_restart()
        elif key == "p":
            self.paused = not self.paused
        elif key in {"n", "r"}:
            self.new_match()
        elif key in {"1", "2", "3"}:
            self.set_difficulty(list(DIFFICULTIES.values())[int(key) - 1])
        elif key == "t":
            self.game.two_player = not self.game.two_player
            self.new_match()
        elif key == "escape":
            self.root.destroy()

    def _on_key_release(self, event) -> None:
        key = event.keysym.lower()
        if key in {"up", "down", "w", "s"}:
            token = self.root.after(50, lambda k=key: self.pressed.discard(k))
            self.release_tokens[key] = token

    def _on_motion(self, event) -> None:
        self.game.move_paddle_to(event.y, "left")

    # -- game flow --------------------------------------------------------- #

    def serve_or_restart(self) -> None:
        if self.game.over:
            self.new_match()
        elif self.game.serving:
            self.game.serve()

    def new_match(self) -> None:
        self.game.reset()
        self.paused = False
        self._draw()

    def set_difficulty(self, ai_speed: float) -> None:
        self.game.ai_speed = ai_speed
        self.new_match()

    # -- loop -------------------------------------------------------------- #

    def _directions(self) -> tuple[int, int]:
        pressed = self.pressed
        if self.game.two_player:
            left = int("s" in pressed) - int("w" in pressed)
            right = int("down" in pressed) - int("up" in pressed)
        else:
            left = int("s" in pressed or "down" in pressed) - int("w" in pressed or "up" in pressed)
            right = 0
        return left, right

    def _tick(self) -> None:
        if not self.paused:
            left, right = self._directions()
            self.game.step(left, right)
            self._draw()
        self.after_id = self.root.after(self.TICK_MS, self._tick)

    # -- drawing ----------------------------------------------------------- #

    def _draw(self) -> None:
        game = self.game
        canvas = self.canvas
        canvas.delete("all")
        w, h = game.WIDTH, game.HEIGHT

        for y in range(0, h, 24):
            canvas.create_line(w / 2, y, w / 2, y + 12, fill=self.NET, width=2)

        canvas.create_text(w / 4, 44, text=str(game.score_left), fill=self.LEFT,
                           font=("Helvetica", 36, "bold"))
        canvas.create_text(w * 3 / 4, 44, text=str(game.score_right), fill=self.RIGHT,
                           font=("Helvetica", 36, "bold"))

        canvas.create_rectangle(0, game.left_y, game.PADDLE_W, game.left_y + game.PADDLE_H,
                                fill=self.FG, outline="")
        canvas.create_rectangle(w - game.PADDLE_W, game.right_y, w,
                                game.right_y + game.PADDLE_H, fill=self.FG, outline="")

        radius = game.ball_radius
        canvas.create_oval(game.ball_x - radius, game.ball_y - radius,
                           game.ball_x + radius, game.ball_y + radius,
                           fill=self.LEFT, outline="")

        if game.serving and not game.over:
            canvas.create_text(w / 2, h - 26, fill=self.MUTED,
                               font=("Helvetica", 12), text="按空白鍵發球")
        if game.over:
            if game.two_player:
                winner = "左邊贏了" if game.winner == "left" else "右邊贏了"
            else:
                winner = self.LEFT_WINS if game.winner == "left" else self.RIGHT_WINS
            canvas.create_text(w / 2, h / 2 - 12, fill=self.FG,
                               font=("Helvetica", 22, "bold"), text=winner)
            canvas.create_text(w / 2, h / 2 + 22, fill=self.MUTED,
                               font=("Helvetica", 12), text="按 N 或點擊重來")
        if self.paused:
            canvas.create_text(w / 2, h / 2, fill=self.FG,
                               font=("Helvetica", 24, "bold"), text="暫停")

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

    game = PongGame(random.Random(0))
    check("starts in the middle, waiting to serve",
          game.serving and game.ball_x == game.WIDTH / 2 and game.ball_vx == 0.0)

    events = game.serve()
    check("serve launches the ball", events == ["serve"] and abs(game.ball_vx) > 0)
    check("no double serve", game.serve() == [])

    # top and bottom walls reflect the ball
    game = PongGame(random.Random(0))
    game.serving = False
    game.ball_x, game.ball_y, game.ball_vx, game.ball_vy = 200, 2, 3, -5
    game.step()
    check("top wall bounces", game.ball_vy > 0 and game.ball_y >= game.ball_radius)

    game.ball_y, game.ball_vy = game.HEIGHT - 2, 5
    game.step()
    check("bottom wall bounces", game.ball_vy < 0
          and game.ball_y <= game.HEIGHT - game.ball_radius)

    # the paddle reflects the ball, speeds it up and keeps it in play
    game = PongGame(random.Random(0))
    game.serving = False
    game.left_y = 140.0
    game.ball_x, game.ball_y, game.ball_vy = 40, 180, 0.0
    game.ball_vx = -game.BALL_SPEED
    before = abs(game.ball_vx)
    bounced = False
    for _ in range(30):
        if any(e == "paddle:left" for e in game.step()):
            bounced = True
            break
    check("left paddle returns the ball", bounced and game.ball_vx > 0)
    check("each hit speeds the ball up", abs(game.ball_vx) > before)
    check("ball stays inside the table", 0 <= game.ball_x <= game.WIDTH)

    # off-centre hits change the angle, centred hits do not
    game = PongGame(random.Random(0))
    game.serving = False
    game.left_y = 140.0
    game.ball_x, game.ball_y, game.ball_vx, game.ball_vy = 40, 145, -5.6, 0.0
    for _ in range(30):
        if any(e == "paddle:left" for e in game.step()):
            break
    check("edge of the paddle adds angle", game.ball_vy < 0)

    # missing the paddle scores for the opponent
    game = PongGame(random.Random(0))
    game.serving = False
    game.left_y = 0.0
    game.ball_x, game.ball_y, game.ball_vx, game.ball_vy = 6, 300, -9.0, 0.0
    scored = []
    for _ in range(120):
        scored.extend(game.step())
        if game.serving:
            break
    check("miss scores for the right side", "score:right" in scored and game.score_right == 1)
    check("ball recentres after a point", game.serving and game.ball_x == game.WIDTH / 2)

    # the AI tracks the ball but is capped by ai_speed
    game = PongGame(random.Random(0), ai_speed=2.0)
    game.serving = False
    game.ball_x, game.ball_y = 300, 20
    start_y = game.right_y
    game.step()
    check("AI moves toward the ball", game.right_y < start_y)
    check("AI respects its speed cap", abs(game.right_y - start_y) <= 2.0 + 1e-9)

    game = PongGame(random.Random(0))
    start_y = game.right_y
    game.ball_x, game.ball_y = 300, 300
    game.step()
    check("AI moves down when the ball is below", game.right_y > start_y)
    check("paddles never leave the table",
          0 <= game.left_y <= game.HEIGHT - game.PADDLE_H
          and 0 <= game.right_y <= game.HEIGHT - game.PADDLE_H)

    # player input drives the left paddle
    game = PongGame(random.Random(0))
    game.step(left_dir=-1)
    check("holding up moves the paddle up", game.left_y < game.HEIGHT / 2 - game.PADDLE_H / 2)
    game.step(left_dir=1)
    game.step(left_dir=1)
    check("holding down moves the paddle down", game.left_y > 0)

    game.move_paddle_to(1000, "left")
    check("mouse input is clamped", game.left_y == game.HEIGHT - game.PADDLE_H)

    # two-player mode drives both paddles
    game = PongGame(random.Random(0), two_player=True)
    game.right_y = 100.0
    game.step(left_dir=0, right_dir=1)
    check("two-player mode moves the right paddle", game.right_y > 100.0)

    # a match ends exactly at the target score and then freezes
    game = PongGame(random.Random(0), win_score=2)
    game.score_point("left")
    events = game.score_point("left")
    check("match ends at the target score", game.over and events == ["win:left"])
    check("winner recorded", game.winner == "left" and game.score_text == "2 : 0")
    frozen = (game.ball_x, game.left_y, game.score_left)
    game.step(left_dir=1)
    check("no movement after the match",
          (game.ball_x, game.left_y, game.score_left) == frozen)
    check("no serve after the match", game.serve() == [])

    # a headless AI-vs-AI match plays to completion
    result = run_simulate(600, seed=7, verbose=False)
    check("simulated match finishes", result.over and result.winner in {"left", "right"})
    check("simulated score reaches the target",
          max(result.score_left, result.score_right) == result.win_score)
    check("rallies are tracked", result.longest_rally >= 1)

    # a perfect tracker beats the capped AI, which is what makes it beatable
    result = run_simulate(600, seed=3, ai_speed=DIFFICULTIES["easy"], verbose=False)
    check("a tracker can beat the easy AI", result.winner == "left")

    print(f"\n{checks} checks passed.")
    return 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pong (Tkinter GUI). Use --selftest or --simulate without a display.",
    )
    parser.add_argument("-d", "--difficulty", choices=DIFFICULTIES, default="normal",
                        help="AI paddle speed preset (default: normal)")
    parser.add_argument("--ai-speed", type=float, help="AI pixels per frame (overrides -d)")
    parser.add_argument("--win-score", type=int, default=PongGame.WIN_SCORE,
                        help="points needed to win the match (default: 5)")
    parser.add_argument("--two-player", action="store_true", help="start in two-player mode")
    parser.add_argument("--seed", type=int, help="fixed random seed (repeatable serves)")
    parser.add_argument("--simulate", type=float, metavar="SECONDS",
                        help="run a headless AI-vs-AI match for this many seconds")
    parser.add_argument("--selftest", action="store_true", help="run the logic tests and exit")
    args = parser.parse_args(argv)

    if args.selftest:
        print("running Pong self tests...")
        return selftest()

    ai_speed = args.ai_speed if args.ai_speed is not None else DIFFICULTIES[args.difficulty]

    if args.simulate is not None:
        game = run_simulate(args.simulate, args.seed, ai_speed, args.win_score)
        winner = {"left": "left (tracker)", "right": "right (AI)"}[game.winner or "left"]
        print(f"\nfinal score {game.score_text} — {winner} wins, "
              f"longest rally {game.longest_rally} hits")
        return 0

    if not TK_AVAILABLE:
        print("tkinter is unavailable — try --simulate or --selftest.", file=sys.stderr)
        return 1
    try:
        PongGUI(ai_speed, args.win_score, args.seed, args.two_player).run()
    except tk.TclError as exc:  # no display
        print(f"cannot open a window ({exc}); try --simulate instead.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
