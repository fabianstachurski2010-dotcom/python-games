#!/usr/bin/env python3
"""Tkinter front-end for Battleship — one window, every screen, bilingual.

Usage
    python3 battleship.py                 # main menu (what ``launch()`` does)
    python3 battleship.py --host          # host a LAN room
    python3 battleship.py --join 4821     # join a discovered room by code
    python3 battleship.py --ai            # practice against the computer

Programmatic use
    import ui
    app = ui.BattleshipApp(root=None, mode="menu", lang="en")
    app.run()                             # blocking Tk main loop
    # or simply:  ui.launch(mode="host")

Design
    * The app owns at most one ``GameServer`` (host mode) and one ``NetClient``.
      All networking happens on worker threads; the Tk thread drains a queue and
      ``NetClient.poll()`` every 50 ms, so the UI never blocks.
    * AI practice runs ``engine.Game`` in-process; the AI's move is computed on
      a worker thread and posted back through the same queue.
    * Every screen is rebuilt from scratch when the language changes; the app
      keeps the transient state (placement, nickname, sync snapshot) so the
      rebuild is lossless.
"""

from __future__ import annotations

import getpass
import ipaddress
import json
import os
import queue
import random
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox

import engine
import i18n
from i18n import t

# --------------------------------------------------------------------------- #
# Palette / layout constants
# --------------------------------------------------------------------------- #

BG = "#0b1626"          # window background
PANEL = "#12243a"       # panels / cards
WATER = "#14304d"       # ocean cells
HOVER = "#1c466e"       # hover / selected fill
ACCENT = "#ffc857"      # amber accent
TEXT = "#e8f1f8"        # primary text
MUTED = "#8fa3b8"       # secondary text
HIT = "#ff5a5f"         # hit marker
MISS = "#8fa3b8"        # miss marker
OWN_SHIP = "#5b7fbf"    # your ships
WIN = "#4ec9a0"         # victory / valid
LINE = "#1d3552"        # hairlines and outlines
DOCK = "#0e1d30"        # inset surfaces
SUNK_RED = "#6e1a22"    # sunk enemy hull
INVALID = "#5c1f27"     # invalid ghost hull

WINDOW_W, WINDOW_H = 1200, 780
MIN_W, MIN_H = 1024, 700
POLL_MS = 50

YOU = 0                 # human slot in local (AI) games
AI_SLOT = 1

AI_KINDS = ("easy", "normal", "hard", "learned")        # everything BattleshipAI accepts
WATCH_KINDS = ("learned", "hard", "normal", "easy")     # order of the watch-setup buttons
WATCH_SPEEDS = (("slow", 900), ("normal", 450), ("fast", 150), ("turbo", 30))  # key, ms/shot

_UI_PREFS = os.path.join(os.path.expanduser("~"), ".battleship_ui.json")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _load_saved_name() -> str:
    try:
        with open(_UI_PREFS, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return str(data.get("name") or "").strip()[:16]
    except (OSError, ValueError, AttributeError):
        return ""


def _save_name(name: str) -> None:
    try:
        with open(_UI_PREFS, "w", encoding="utf-8") as handle:
            json.dump({"name": name}, handle)
    except OSError:
        pass


def _os_username() -> str:
    try:
        name = getpass.getuser().strip()
    except Exception:  # noqa: BLE001 - getuser is best effort
        name = ""
    return (name or "Player")[:16]


def _rounded(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float,
             radius: float = 14, **kwargs):
    """Draw a smooth rounded rectangle polygon on *canvas*."""
    radius = max(0.0, min(radius, abs(x2 - x1) / 2.0, abs(y2 - y1) / 2.0))
    points = [
        x1 + radius, y1, x2 - radius, y1, x2, y1,
        x2, y1 + radius, x2, y2 - radius, x2, y2,
        x2 - radius, y2, x1 + radius, y2, x1, y2,
        x1, y2 - radius, x1, y1 + radius, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


def _canvas_size(widget: tk.Canvas, fallback_w: int = 300,
                 fallback_h: int = 200) -> tuple[int, int]:
    width = widget.winfo_width()
    height = widget.winfo_height()
    if width <= 2:
        try:
            width = int(widget.cget("width"))
        except (tk.TclError, ValueError):
            width = fallback_w
    if height <= 2:
        try:
            height = int(widget.cget("height"))
        except (tk.TclError, ValueError):
            height = fallback_h
    return max(width, 2), max(height, 2)


def _set_text(widget: tk.Text, text: str) -> None:
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    widget.insert("1.0", text)
    widget.configure(state="disabled")
    widget.see("end")


def learned_model_problem() -> str | None:
    """Why the learned AI cannot be used right now (None = it is ready)."""
    try:
        import ai_net
    except ImportError:
        return t("ai.no_numpy")
    if not ai_net.model_exists():
        return t("ai.no_model")
    return None


def _valid_ip(text: str) -> bool:
    text = (text or "").strip()
    if text.lower() in ("localhost",):
        return True
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# Canvas widgets
# --------------------------------------------------------------------------- #

class Card(tk.Canvas):
    """Rounded panel that hosts ordinary widgets in an inner frame."""

    def __init__(self, master, app: "BattleshipApp", fill: str = PANEL,
                 radius: int = 18, padx: int = 18, pady: int = 16,
                 bg: str = BG, outline: str = LINE, **kwargs):
        kwargs.setdefault("width", 10)
        kwargs.setdefault("height", 10)
        super().__init__(master, bg=bg, highlightthickness=0, bd=0, **kwargs)
        self.app = app
        self.fill = fill
        self.radius = radius
        self.padx = padx
        self.pady = pady
        self.outline = outline
        self.inner = tk.Frame(self, bg=fill)
        self.inner.place(x=padx, y=pady, relwidth=1, relheight=1,
                         width=-2 * padx, height=-2 * pady)
        self.bind("<Configure>", self._redraw)

    def _redraw(self, _event=None) -> None:
        width, height = _canvas_size(self)
        self.delete("card")
        _rounded(self, 1, 1, width - 1, height - 1, self.radius,
                 fill=self.fill, outline=self.outline, width=1, tags="card")
        self.tag_lower("card")


class TextButton(tk.Canvas):
    """Canvas-drawn button with hover, press and disabled states."""

    _STYLES = {
        "primary": (ACCENT, "#0b1626", "#ffd88a", ""),
        "ghost": (PANEL, TEXT, HOVER, LINE),
        "danger": ("#3a1420", TEXT, "#521c2b", HIT),
        "subtle": (DOCK, TEXT, HOVER, LINE),
    }

    def __init__(self, master, app: "BattleshipApp", text: str,
                 command=None, width: int = 170, height: int = 44,
                 kind: str = "primary", font_key: str = "f_button",
                 **kwargs):
        kwargs.setdefault("bg", BG)
        super().__init__(master, width=width, height=height,
                         highlightthickness=0, bd=0, cursor="hand2", **kwargs)
        self.app = app
        self.text = text
        self.command = command
        self.kind = kind
        self.font_key = font_key
        self.enabled = True
        self._hover = False
        self._pressed = False
        self.bind("<Configure>", lambda _e: self.redraw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    # -- state ------------------------------------------------------------- #
    def set_text(self, text: str) -> None:
        self.text = text
        self.redraw()

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        self.configure(cursor="hand2" if self.enabled else "arrow")
        self.redraw()

    def set_kind(self, kind: str) -> None:
        self.kind = kind
        self.redraw()

    # -- drawing ----------------------------------------------------------- #
    def _fill_color(self) -> tuple[str, str, str]:
        fill, fg, hover_fill, outline = self._STYLES.get(self.kind, self._STYLES["ghost"])
        if not self.enabled:
            return DOCK, MUTED, DOCK, LINE
        if self._pressed:
            return outline or HOVER, fg, hover_fill, outline
        if self._hover:
            return hover_fill, fg, hover_fill, outline
        return fill, fg, hover_fill, outline

    def redraw(self) -> None:
        width, height = _canvas_size(self)
        self.delete("all")
        fill, fg, _hover, outline = self._fill_color()
        _rounded(self, 1, 1, width - 1, height - 1, min(12, height / 2 - 1),
                 fill=fill, outline=outline or fill, width=1)
        self.create_text(width / 2, height / 2, text=self.text,
                         fill=fg, font=getattr(self.app, self.font_key))

    # -- events ------------------------------------------------------------ #
    def _on_enter(self, _event=None) -> None:
        self._hover = True
        if self.enabled:
            self.redraw()

    def _on_leave(self, _event=None) -> None:
        self._hover = False
        self._pressed = False
        self.redraw()

    def _on_press(self, _event=None) -> None:
        if not self.enabled:
            return
        self._pressed = True
        self.redraw()

    def _on_release(self, _event=None) -> None:
        was_pressed = self._pressed
        self._pressed = False
        self.redraw()
        if was_pressed and self.enabled and self.command is not None:
            self.command()


class MenuCard(tk.Canvas):
    """Large clickable menu tile drawn entirely on a canvas."""

    def __init__(self, master, app: "BattleshipApp", number: str, title: str,
                 desc: str, command, width: int = 292, height: int = 226):
        super().__init__(master, width=width, height=height, bg=BG,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.app = app
        self.number = number
        self.title = title
        self.desc = desc
        self.command = command
        self._hover = False
        self.bind("<Configure>", lambda _e: self.redraw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
        self.redraw()

    def _on_enter(self, _event=None) -> None:
        self._hover = True
        self.redraw()

    def _on_leave(self, _event=None) -> None:
        self._hover = False
        self.redraw()

    def _on_click(self, _event=None) -> None:
        if self.command is not None:
            self.command()

    def redraw(self) -> None:
        width, height = _canvas_size(self)
        self.delete("all")
        fill = HOVER if self._hover else PANEL
        outline = ACCENT if self._hover else LINE
        _rounded(self, 2, 2, width - 2, height - 2, 20,
                 fill=fill, outline=outline, width=2 if self._hover else 1)
        self.create_text(28, 34, text=self.number, anchor="w",
                         fill=ACCENT, font=self.app.f_h2)
        self.create_text(28, 74, text=self.title, anchor="w",
                         fill=TEXT, font=self.app.f_h1)
        self.create_text(28, 116, text=self.desc, anchor="nw", width=width - 56,
                         fill=MUTED, font=self.app.f_small, justify="left")
        self.create_text(width - 26, height - 26, text="→", anchor="e",
                         fill=ACCENT, font=self.app.f_h2)


class ShipChip(tk.Canvas):
    """One draggable ship entry in the placement dock."""

    def __init__(self, master, app: "BattleshipApp", name: str, size: int,
                 on_press, on_motion, on_release):
        super().__init__(master, height=42, bg=PANEL, highlightthickness=0,
                         bd=0, cursor="hand2", width=250)
        self.app = app
        self.name = name
        self.size = size
        self.on_press = on_press
        self.on_motion = on_motion
        self.on_release = on_release
        self.hovered = False
        self.placed = False
        self.selected = False
        self.bind("<Configure>", lambda _e: self.redraw())
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<Button-1>", lambda e: self.on_press(self.name))
        self.bind("<B1-Motion>", lambda e: self.on_motion(self.name))
        self.bind("<ButtonRelease-1>", lambda e: self.on_release(self.name))
        self.redraw()

    def _enter(self, _event=None) -> None:
        self.hovered = True
        self.redraw()

    def _leave(self, _event=None) -> None:
        self.hovered = False
        self.redraw()

    def redraw(self) -> None:
        width, height = _canvas_size(self, fallback_w=250, fallback_h=42)
        self.delete("all")
        if self.selected:
            fill, outline = HOVER, ACCENT
        elif self.hovered:
            fill, outline = HOVER, LINE
        else:
            fill, outline = DOCK, LINE
        _rounded(self, 1, 1, width - 1, height - 1, 10,
                 fill=fill, outline=outline, width=1)
        # Miniature hull made of one square per deck.
        cell = 9
        start_x = 12
        y = height / 2
        for index in range(self.size):
            x = start_x + index * (cell + 2)
            color = OWN_SHIP if not self.placed else "#3d5677"
            self.create_rectangle(x, y - cell / 2, x + cell, y + cell / 2,
                                  fill=color, outline="")
        label = i18n.ship_label(self.name)
        self.create_text(start_x + self.size * (cell + 2) + 8, y, text=label,
                         anchor="w", fill=MUTED if self.placed else TEXT,
                         font=self.app.f_chip)


class BoardCanvas(tk.Canvas):
    """A 10x10 ocean grid: coordinates, ships, shots and ghost preview."""

    def __init__(self, master, app: "BattleshipApp", kind: str = "own",
                 interactive: bool = False, on_cell=None,
                 on_hover=None, on_rotate=None, **kwargs):
        kwargs.setdefault("width", 380)
        kwargs.setdefault("height", 380)
        super().__init__(master, bg=BG, highlightthickness=0, bd=0, **kwargs)
        self.app = app
        self.kind = kind
        self.interactive = interactive
        self.on_cell = on_cell
        self.on_hover = on_hover
        self.on_rotate = on_rotate
        self.ships: list[dict] = []
        self.shots: dict[tuple[int, int], str] = {}
        self.sunk_ships: list[dict] = []
        self.ghost_cells: list[tuple[int, int]] = []
        self.ghost_valid = True
        self.hover_cell: tuple[int, int] | None = None
        self._geo: tuple[float, float, float] | None = None
        self.bind("<Configure>", lambda _e: self.redraw())
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", self._leave)
        self.bind("<Button-1>", self._click)
        self.bind("<Button-3>", self._right_click)
        self.bind("<Button-2>", self._right_click)
        self.set_interactive(interactive)

    # -- data -------------------------------------------------------------- #
    def set_ships(self, ships: list[dict]) -> None:
        self.ships = list(ships or [])
        self.redraw()

    def set_shots(self, shots) -> None:
        mapping: dict[tuple[int, int], str] = {}
        for item in shots or []:
            try:
                r, c, result = int(item[0]), int(item[1]), str(item[2])
            except (TypeError, ValueError, IndexError):
                continue
            mapping[(r, c)] = result
        self.shots = mapping
        self.redraw()

    def set_sunk_ships(self, sunk: list[dict]) -> None:
        self.sunk_ships = list(sunk or [])
        self.redraw()

    def set_ghost(self, cells, valid: bool = True) -> None:
        self.ghost_cells = [tuple(cell) for cell in (cells or [])]
        self.ghost_valid = bool(valid)
        self.redraw()

    def clear_ghost(self) -> None:
        if self.ghost_cells:
            self.ghost_cells = []
            self.redraw()

    def set_interactive(self, interactive: bool) -> None:
        self.interactive = bool(interactive)
        self.configure(cursor="crosshair" if self.interactive else "")

    # -- geometry ---------------------------------------------------------- #
    def _compute(self, width: int, height: int) -> tuple[float, float, float]:
        margin_l, margin_t = 24.0, 20.0
        avail_w = max(40.0, width - margin_l - 8.0)
        avail_h = max(40.0, height - margin_t - 8.0)
        cell = max(8.0, min(avail_w, avail_h) / engine.SIZE)
        board = cell * engine.SIZE
        x0 = margin_l + (avail_w - board) / 2.0
        y0 = margin_t + (avail_h - board) / 2.0
        return x0, y0, cell

    def point_cell(self, x: float, y: float) -> tuple[int, int] | None:
        if self._geo is None:
            return None
        x0, y0, cell = self._geo
        if cell <= 0:
            return None
        c = int((x - x0) // cell)
        r = int((y - y0) // cell)
        if 0 <= r < engine.SIZE and 0 <= c < engine.SIZE:
            return r, c
        return None

    # -- drawing ----------------------------------------------------------- #
    def redraw(self) -> None:
        width, height = _canvas_size(self, fallback_w=380, fallback_h=380)
        self.delete("all")
        x0, y0, cell = self._compute(width, height)
        self._geo = (x0, y0, cell)
        board = cell * engine.SIZE
        _rounded(self, x0 - 3, y0 - 3, x0 + board + 3, y0 + board + 3, 10,
                 fill=WATER, outline=LINE, width=1)
        for index in range(engine.SIZE + 1):
            pos = index * cell
            self.create_line(x0 + pos, y0, x0 + pos, y0 + board, fill=LINE)
            self.create_line(x0, y0 + pos, x0 + board, y0 + pos, fill=LINE)
        for col in range(engine.SIZE):
            self.create_text(x0 + (col + 0.5) * cell, y0 - 12,
                             text=chr(ord("A") + col), fill=MUTED,
                             font=self.app.f_tiny)
        for row in range(engine.SIZE):
            self.create_text(x0 - 13, y0 + (row + 0.5) * cell,
                             text=str(row + 1), fill=MUTED,
                             font=self.app.f_tiny)

        # Your hulls (placement board and own waters).
        if self.kind in ("own", "placement"):
            for ship in self.ships:
                sunk = bool(ship.get("sunk"))
                for cell_coord in ship.get("cells") or []:
                    r, c = int(cell_coord[0]), int(cell_coord[1])
                    px, py = x0 + c * cell, y0 + r * cell
                    fill = "#3d5677" if sunk else OWN_SHIP
                    _rounded(self, px + 2.5, py + 2.5,
                             px + cell - 2.5, py + cell - 2.5, 4,
                             fill=fill, outline="#7fa4e0", width=1)

        # Enemy hulls that are already sunk.
        if self.kind == "enemy":
            for ship in self.sunk_ships:
                for cell_coord in ship.get("cells") or []:
                    r, c = int(cell_coord[0]), int(cell_coord[1])
                    px, py = x0 + c * cell, y0 + r * cell
                    _rounded(self, px + 2.5, py + 2.5,
                             px + cell - 2.5, py + cell - 2.5, 4,
                             fill=SUNK_RED, outline=HIT, width=1)
                    self.create_line(px + 6, py + 6, px + cell - 6, py + cell - 6,
                                     fill=HIT, width=1)
                    self.create_line(px + cell - 6, py + 6, px + 6, py + cell - 6,
                                     fill=HIT, width=1)

        # Shot markers.
        for (r, c), result in self.shots.items():
            cx = x0 + (c + 0.5) * cell
            cy = y0 + (r + 0.5) * cell
            if result == "hit":
                radius = cell * 0.27
                self.create_oval(cx - radius, cy - radius, cx + radius, cy + radius,
                                 fill=HIT, outline="")
                self.create_line(cx - radius * 0.55, cy - radius * 0.55,
                                 cx + radius * 0.55, cy + radius * 0.55,
                                 fill="#7a1d24", width=2)
                self.create_line(cx + radius * 0.55, cy - radius * 0.55,
                                 cx - radius * 0.55, cy + radius * 0.55,
                                 fill="#7a1d24", width=2)
            else:
                radius = max(2.0, cell * 0.15)
                self.create_oval(cx - radius, cy - radius, cx + radius, cy + radius,
                                 fill=MISS, outline="")

        # Ghost preview while placing.
        for (r, c) in self.ghost_cells:
            if not (0 <= r < engine.SIZE and 0 <= c < engine.SIZE):
                continue
            px, py = x0 + c * cell, y0 + r * cell
            fill = "#1f5c4d" if self.ghost_valid else INVALID
            outline = WIN if self.ghost_valid else HIT
            _rounded(self, px + 2, py + 2, px + cell - 2, py + cell - 2, 4,
                     fill=fill, outline=outline, width=2)

        # Hover highlight.
        if self.hover_cell is not None and self.interactive:
            r, c = self.hover_cell
            px, py = x0 + c * cell, y0 + r * cell
            self.create_rectangle(px + 1, py + 1, px + cell - 1, py + cell - 1,
                                  outline=ACCENT, width=2)

    # -- events ------------------------------------------------------------ #
    def _motion(self, event) -> None:
        cell = self.point_cell(event.x, event.y)
        if cell != self.hover_cell:
            self.hover_cell = cell
            self.redraw()
            if self.on_hover is not None:
                if cell is None:
                    self.on_hover(None, None)
                else:
                    self.on_hover(cell[0], cell[1])

    def _leave(self, _event=None) -> None:
        if self.hover_cell is not None:
            self.hover_cell = None
            self.redraw()
        if self.on_hover is not None:
            self.on_hover(None, None)

    def _click(self, event) -> None:
        if not self.interactive:
            return
        cell = self.point_cell(event.x, event.y)
        if cell is not None and self.on_cell is not None:
            self.on_cell(cell[0], cell[1])

    def _right_click(self, _event=None) -> None:
        if self.interactive and self.on_rotate is not None:
            self.on_rotate()


class LogPanel(tk.Frame):
    """Title + read-only scrolling text area on the dark panel surface."""

    def __init__(self, master, app: "BattleshipApp", title: str, height: int = 6):
        super().__init__(master, bg=PANEL)
        self.app = app
        tk.Label(self, text=title, font=app.f_h2, fg=TEXT, bg=PANEL,
                 anchor="w").pack(fill="x")
        self.text = tk.Text(self, height=height, bg=DOCK, fg=TEXT, bd=0,
                            highlightthickness=0, wrap="word", font=app.f_small,
                            insertbackground=TEXT, padx=8, pady=6,
                            selectbackground=HOVER)
        self.text.pack(fill="both", expand=True, pady=(6, 0))
        self.text.configure(state="disabled")

    def set_lines(self, lines: list[str]) -> None:
        _set_text(self.text, "\n".join(lines))


class ChatBox(LogPanel):
    """Chat history plus an input row (Enter or the Send button)."""

    def __init__(self, master, app: "BattleshipApp", height: int = 5):
        super().__init__(master, app, t("battle.chat_title"), height=height)
        row = tk.Frame(self, bg=PANEL)
        row.pack(fill="x", pady=(6, 0))
        self.entry = tk.Entry(row, bg=DOCK, fg=TEXT, insertbackground=ACCENT,
                              relief="flat", highlightthickness=1,
                              highlightbackground=LINE, highlightcolor=ACCENT,
                              font=app.f_body)
        self.entry.pack(side="left", fill="x", expand=True, ipady=4)
        self.entry.bind("<Return>", lambda _e: self.send())
        self.send_button = TextButton(row, app, t("common.send"), self.send,
                                      width=74, height=32, kind="ghost",
                                      font_key="f_small")
        self.send_button.pack(side="left", padx=(8, 0))
        self.app = app

    def send(self) -> None:
        text = self.entry.get().strip()
        if not text:
            return
        if self.app.send_chat(text):
            self.entry.delete(0, "end")

    def set_messages(self, chat: list[dict]) -> None:
        lines = []
        for message in (chat or [])[-100:]:
            who = message.get("from") or t("chat.system")
            text = str(message.get("text") or "")
            lines.append(f"{who}: {text}")
        self.set_lines(lines or [t("battle.chat_empty")])


# --------------------------------------------------------------------------- #
# Screens
# --------------------------------------------------------------------------- #

class MenuScreen(tk.Frame):
    def __init__(self, app: "BattleshipApp"):
        super().__init__(app.root, bg=BG)
        self.app = app
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=30, pady=(18, 0))
        tk.Label(top, text=t("app.title"), font=app.f_h2, fg=MUTED,
                 bg=BG).pack(side="left")
        app.make_lang_button(top).pack(side="right")

        hero = tk.Frame(self, bg=BG)
        hero.pack(pady=(30, 0))
        self.title_label = tk.Label(hero, text=t("app.title"), font=app.f_hero,
                                    fg=TEXT, bg=BG)
        self.title_label.pack()
        tk.Label(hero, text=t("app.subtitle"), font=app.f_h1, fg=ACCENT,
                 bg=BG).pack(pady=(2, 0))
        tk.Label(hero, text=t("app.tagline"), font=app.f_body, fg=MUTED,
                 bg=BG).pack(pady=(8, 0))

        cards_row = tk.Frame(self, bg=BG)
        cards_row.pack(pady=(26, 0))
        specs = (
            ("01", t("menu.host"), t("menu.host_desc"), app.begin_host),
            ("02", t("menu.join"), t("menu.join_desc"), app.show_join),
            ("03", t("menu.ai"), t("menu.ai_desc"), app.show_ai_setup),
        )
        self.cards: list[MenuCard] = []
        for number, title, desc, command in specs:
            card = MenuCard(cards_row, app, number, title, desc, command)
            card.pack(side="left", padx=11)
            self.cards.append(card)
        app.menu_cards = self.cards

        footer = tk.Frame(self, bg=BG)
        footer.pack(side="bottom", fill="x", pady=(0, 22))
        tk.Label(footer, text=t("menu.net_hint"), font=app.f_small, fg=MUTED,
                 bg=BG).pack(pady=(0, 10))
        row = tk.Frame(footer, bg=BG)
        row.pack()
        self.help_button = TextButton(row, app, t("menu.help"),
                                      app.show_help, width=160, height=42,
                                      kind="ghost")
        self.help_button.pack(side="left", padx=8)
        self.watch_button = TextButton(row, app, t("menu.watch"),
                                       app.show_watch_setup, width=160, height=42,
                                       kind="ghost")
        self.watch_button.pack(side="left", padx=8)
        self.quit_button = TextButton(row, app, t("menu.quit"), app.close,
                                      width=160, height=42, kind="danger")
        self.quit_button.pack(side="left", padx=8)


class HostScreen(tk.Frame):
    """Waiting room: big room code, address hints and a live spinner."""

    def __init__(self, app: "BattleshipApp"):
        super().__init__(app.root, bg=BG)
        self.app = app
        self._anim_job = None
        self._dots = 0
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=30, pady=(18, 0))
        tk.Label(top, text=t("app.title"), font=app.f_h2, fg=MUTED,
                 bg=BG).pack(side="left")
        app.make_lang_button(top).pack(side="right")

        card = Card(self, app, width=680, height=470)
        card.place(relx=0.5, rely=0.46, anchor="center")
        inner = card.inner
        title_key = "host.title" if app.mode == "host" else "lobby.waiting_player"
        tk.Label(inner, text=t(title_key), font=app.f_h1, fg=TEXT,
                 bg=PANEL).pack(pady=(6, 4))
        tk.Label(inner, text=t("host.code_hint"), font=app.f_body, fg=MUTED,
                 bg=PANEL, wraplength=600, justify="center").pack()

        self.code_label = tk.Label(inner, text=self._spaced_code(), fg=ACCENT,
                                   bg=PANEL, font=app.f_code)
        self.code_label.pack(pady=(10, 6))
        copy_row = tk.Frame(inner, bg=PANEL)
        copy_row.pack()
        TextButton(copy_row, app, t("common.copy"), app.copy_code,
                   width=130, height=36, kind="ghost").pack()

        tk.Frame(inner, bg=LINE, height=1).pack(fill="x", pady=14)
        address = tk.Frame(inner, bg=PANEL)
        address.pack()
        tk.Label(address, text=f"{t('host.local_ip')}:  {app.room_ip}",
                 font=app.f_body, fg=TEXT, bg=PANEL).pack()
        tk.Label(address, text=f"{t('common.port')}:  {app.room_port}",
                 font=app.f_body, fg=TEXT, bg=PANEL).pack(pady=(4, 0))
        self.manual_hint = tk.Label(inner, text=t("host.manual_hint"),
                                    font=app.f_small, fg=MUTED, bg=PANEL,
                                    wraplength=600, justify="center")
        self.manual_hint.pack(pady=(10, 0))
        self.set_discovery(getattr(app, "discovery_active", True))
        tk.Label(inner, text=t("host.same_network"), font=app.f_small, fg=MUTED,
                 bg=PANEL).pack(pady=(2, 0))

        self.spinner = tk.Label(inner, text=t("common.waiting"), font=app.f_body,
                                fg=WIN, bg=PANEL)
        self.spinner.pack(pady=(12, 6))
        self.players_label = tk.Label(inner, text="", font=app.f_small, fg=MUTED,
                                      bg=PANEL)
        self.players_label.pack()
        TextButton(inner, app, t("common.cancel"), app.leave_to_menu,
                   width=150, height=40, kind="ghost").pack(pady=(12, 0))
        self._tick()

    def _spaced_code(self) -> str:
        code = str(self.app.room_code or "----")
        return "  ".join(code)

    def _tick(self) -> None:
        self._dots = (self._dots + 1) % 4
        try:
            self.spinner.configure(text=f"{t('common.waiting')}{'.' * self._dots}")
        except tk.TclError:
            return
        self._anim_job = self.after(420, self._tick)

    def set_discovery(self, active: bool) -> None:
        """Tell the host whether LAN auto-discovery actually bound UDP."""
        if active:
            self.manual_hint.configure(text=t("host.manual_hint"), fg=MUTED)
        else:
            self.manual_hint.configure(text=t("host.manual_only"), fg=ACCENT)

    def update_view(self, view: dict | None) -> None:
        if not view:
            return
        self.code_label.configure(text=self._spaced_code())
        players = view.get("players") or []
        connected = sum(1 for player in players if player)
        self.players_label.configure(
            text=f"{t('common.players')}  {connected}/2")

    def destroy(self) -> None:
        if self._anim_job is not None:
            try:
                self.after_cancel(self._anim_job)
            except tk.TclError:
                pass
            self._anim_job = None
        super().destroy()


class JoinScreen(tk.Frame):
    """Nickname + code, discovery list, and a manual IP fallback."""

    def __init__(self, app: "BattleshipApp"):
        super().__init__(app.root, bg=BG)
        self.app = app
        self.manual = bool(app.manual)
        app.lang_button = None
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=30, pady=(18, 0))
        tk.Label(top, text=t("app.title"), font=app.f_h2, fg=MUTED,
                 bg=BG).pack(side="left")
        app.make_lang_button(top).pack(side="right")

        card = Card(self, app, width=760, height=610)
        card.place(relx=0.5, rely=0.46, anchor="center")
        inner = card.inner
        tk.Label(inner, text=t("join.title"), font=app.f_h1, fg=TEXT,
                 bg=PANEL, anchor="w").pack(fill="x", pady=(0, 8))

        # -- nickname ------------------------------------------------------- #
        name_row = tk.Frame(inner, bg=PANEL)
        name_row.pack(fill="x", pady=(0, 10))
        tk.Label(name_row, text=t("common.name"), width=10, anchor="w",
                 font=app.f_body, fg=MUTED, bg=PANEL).pack(side="left")
        self.name_var = tk.StringVar(value=app.nickname)
        self.name_entry = self._entry(name_row, self.name_var, app.f_body)
        self.name_entry.pack(side="left", fill="x", expand=True, ipady=5)
        self.name_var.trace_add("write", self._on_name)

        # -- room code ------------------------------------------------------ #
        code_row = tk.Frame(inner, bg=PANEL)
        code_row.pack(fill="x", pady=(0, 8))
        tk.Label(code_row, text=t("join.code_label"), width=10, anchor="w",
                 font=app.f_body, fg=MUTED, bg=PANEL).pack(side="left")
        self.code_var = tk.StringVar(value=app.join_code)
        self.code_entry = self._entry(code_row, self.code_var, app.f_h1,
                                      justify="center", width=7)
        self.code_entry.pack(side="left", ipady=5)
        tk.Label(code_row, text=t("join.code_placeholder"), font=app.f_small,
                 fg=MUTED, bg=PANEL).pack(side="left", padx=(10, 0))
        self.code_var.trace_add("write", self._on_code)
        # The discover action gets its own labelled button under the field.

        self.status_label = tk.Label(inner, text="",
                                     font=app.f_small, fg=MUTED, bg=PANEL,
                                     anchor="w")
        self.status_label.pack(fill="x", pady=(2, 4))

        tk.Label(inner, text=t("join.rooms_title"), font=app.f_small, fg=MUTED,
                 bg=PANEL, anchor="w").pack(fill="x")
        self.rooms_frame = tk.Frame(inner, bg=PANEL, height=112)
        self.rooms_frame.pack(fill="x", pady=(2, 8))
        self.rooms_frame.pack_propagate(False)

        # -- manual fallback ------------------------------------------------ #
        toggle_row = tk.Frame(inner, bg=PANEL)
        toggle_row.pack(fill="x", pady=(0, 4))
        self.manual_button = TextButton(toggle_row, app, t("join.manual"),
                                        self._toggle_manual, width=170, height=34,
                                        kind="subtle", font_key="f_small")
        self.manual_button.pack(side="left")
        self.manual_frame = tk.Frame(inner, bg=PANEL)
        ip_row = tk.Frame(self.manual_frame, bg=PANEL)
        ip_row.pack(fill="x", pady=(4, 0))
        tk.Label(ip_row, text=t("common.ip"), width=10, anchor="w",
                 font=app.f_body, fg=MUTED, bg=PANEL).pack(side="left")
        self.ip_var = tk.StringVar(value=app.join_ip)
        self.ip_entry = self._entry(ip_row, self.ip_var, app.f_body)
        self.ip_entry.pack(side="left", fill="x", expand=True, ipady=5)
        self.ip_var.trace_add("write", self._on_ip)
        port_row = tk.Frame(self.manual_frame, bg=PANEL)
        port_row.pack(fill="x", pady=(6, 0))
        tk.Label(port_row, text=t("common.port"), width=10, anchor="w",
                 font=app.f_body, fg=MUTED, bg=PANEL).pack(side="left")
        self.port_var = tk.StringVar(value=app.join_port)
        self.port_entry = self._entry(port_row, self.port_var, app.f_body, width=10)
        self.port_entry.pack(side="left", ipady=5)
        self.port_var.trace_add("write", self._on_port)

        # -- actions -------------------------------------------------------- #
        actions = tk.Frame(inner, bg=PANEL)
        actions.pack(fill="x", side="bottom", pady=(10, 0))
        self.back_button = TextButton(actions, app, t("common.back"),
                                      app.leave_to_menu, width=150, height=44,
                                      kind="ghost")
        self.back_button.pack(side="left")
        self.join_button = TextButton(actions, app, t("join.button"), self._join,
                                      width=190, height=44, kind="primary")
        self.join_button.pack(side="right")
        self.search_button = TextButton(actions, app, t("join.search"),
                                        self._discover, width=170, height=44,
                                        kind="subtle")
        self.search_button.pack(side="right", padx=(0, 10))

        self._apply_manual()
        self.set_rooms(app.rooms)
        self.bind("<Return>", lambda _e: self._join())

    # -- field helpers ------------------------------------------------------ #
    def _entry(self, parent, var: tk.StringVar, font, justify: str = "left",
               width: int = 24) -> tk.Entry:
        return tk.Entry(parent, textvariable=var, font=font, bg=DOCK, fg=TEXT,
                        insertbackground=ACCENT, relief="flat",
                        highlightthickness=1, highlightbackground=LINE,
                        highlightcolor=ACCENT, justify=justify, width=width)

    def _on_name(self, *_args) -> None:
        self.app.nickname = self.name_var.get()

    def _on_code(self, *_args) -> None:
        self.app.join_code = self.code_var.get()

    def _on_ip(self, *_args) -> None:
        self.app.join_ip = self.ip_var.get()

    def _on_port(self, *_args) -> None:
        self.app.join_port = self.port_var.get()

    def _toggle_manual(self) -> None:
        self.manual = not self.manual
        self.app.manual = self.manual
        self._apply_manual()

    def _apply_manual(self) -> None:
        if self.manual:
            self.manual_frame.pack(fill="x", pady=(2, 0), before=self.search_button)
        else:
            self.manual_frame.pack_forget()

    # -- actions ------------------------------------------------------------ #
    def _join(self) -> None:
        self.app.attempt_join(self.name_var.get(), self.code_var.get(),
                              self.ip_var.get(), self.port_var.get())

    def _discover(self) -> None:
        self.set_status(t("net.discovery"))
        self.search_button.set_enabled(False)
        self.app.discover_rooms_async(self._on_rooms)

    def _on_rooms(self, rooms: list[dict]) -> None:
        self.search_button.set_enabled(True)
        self.set_rooms(rooms)
        self.set_status(t("join.found") if rooms else t("join.not_found"))

    def set_status(self, text: str) -> None:
        try:
            self.status_label.configure(text=text)
        except tk.TclError:
            pass

    def connect_failed(self) -> None:
        self.join_button.set_enabled(True)
        self.search_button.set_enabled(True)
        self.set_status(t("err.connect_failed"))

    def set_rooms(self, rooms: list[dict]) -> None:
        for child in self.rooms_frame.winfo_children():
            child.destroy()
        rooms = [room for room in (rooms or []) if isinstance(room, dict)][:4]
        if not rooms:
            tk.Label(self.rooms_frame, text=t("join.no_rooms"), anchor="w",
                     font=self.app.f_small, fg=MUTED, bg=PANEL).pack(fill="x")
            return
        for room in rooms:
            self._room_row(room).pack(fill="x", pady=2)

    def _room_row(self, room: dict) -> tk.Frame:
        row = tk.Frame(self.rooms_frame, bg=DOCK, cursor="hand2")
        code = str(room.get("code") or "----")
        names = ", ".join(name for name in (room.get("names") or []) if name)
        state = str(room.get("state") or "lobby")
        state_text = t(f"room.state.{state}") if f"room.state.{state}" in i18n.STRINGS else state
        code_label = tk.Label(row, text=code, font=self.app.f_h2, fg=ACCENT,
                              bg=DOCK, width=6, anchor="w")
        code_label.pack(side="left", padx=(10, 6), pady=5)
        meta = t("join.room_meta", host=room.get("host", "?"),
                 port=room.get("port", "?"), names=names)
        meta_label = tk.Label(row, text=meta, font=self.app.f_small, fg=TEXT,
                              bg=DOCK, anchor="w")
        meta_label.pack(side="left")
        state_label = tk.Label(row, text=state_text, font=self.app.f_small,
                               fg=MUTED, bg=DOCK, anchor="e")
        state_label.pack(side="right", padx=10)
        children = (row, code_label, meta_label, state_label)
        for widget in children:
            widget.bind("<Button-1>", lambda _e, r=room: self.app.join_discovered(r))
            widget.bind("<Enter>", lambda _e: self._paint(children, HOVER))
            widget.bind("<Leave>", lambda _e: self._paint(children, DOCK))
        return row

    @staticmethod
    def _paint(widgets, color: str) -> None:
        for widget in widgets:
            try:
                widget.configure(bg=color)
            except tk.TclError:
                pass

    def auto_join(self) -> None:
        if not self.app.join_code and not self.app.join_ip:
            return
        self.app.attempt_join(self.name_var.get(), self.code_var.get(),
                              self.ip_var.get(), self.port_var.get())


class AiSetupScreen(tk.Frame):
    def __init__(self, app: "BattleshipApp"):
        super().__init__(app.root, bg=BG)
        self.app = app
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=30, pady=(18, 0))
        tk.Label(top, text=t("app.title"), font=app.f_h2, fg=MUTED,
                 bg=BG).pack(side="left")
        app.make_lang_button(top).pack(side="right")

        card = Card(self, app, width=620, height=440)
        card.place(relx=0.5, rely=0.46, anchor="center")
        inner = card.inner
        tk.Label(inner, text=t("ai.title"), font=app.f_h1, fg=TEXT, bg=PANEL,
                 anchor="w").pack(fill="x")
        tk.Label(inner, text=t("ai.difficulty"), font=app.f_body, fg=MUTED,
                 bg=PANEL, anchor="w").pack(fill="x", pady=(18, 8))
        row = tk.Frame(inner, bg=PANEL)
        row.pack(fill="x")
        self.diff_buttons: dict[str, TextButton] = {}
        for name in AI_KINDS:
            button = TextButton(row, app, t(f"ai.{name}"),
                                lambda n=name: self._pick(n), width=128, height=46,
                                kind="ghost")
            button.pack(side="left", padx=(0, 10))
            self.diff_buttons[name] = button
        self.model_hint = tk.Label(inner, text="", font=app.f_small, fg=HIT,
                                   bg=PANEL, anchor="w", justify="left",
                                   wraplength=540)
        self.model_hint.pack(fill="x", pady=(10, 0))
        self.turn_label = tk.Label(inner, text=t("placement.turn_first"),
                                   font=app.f_body, fg=WIN, bg=PANEL, anchor="w")
        self.turn_label.pack(fill="x", pady=(22, 0))
        tk.Label(inner, text=t("app.tagline"), font=app.f_small, fg=MUTED,
                 bg=PANEL, anchor="w").pack(fill="x", pady=(6, 0))
        actions = tk.Frame(inner, bg=PANEL)
        actions.pack(fill="x", side="bottom", pady=(18, 0))
        TextButton(actions, app, t("common.back"), app.leave_to_menu,
                   width=140, height=44, kind="ghost").pack(side="left")
        TextButton(actions, app, t("ai.start"), self._start, width=190,
                   height=44, kind="primary").pack(side="right")
        self._pick(app.difficulty)

    def _pick(self, name: str) -> None:
        self.app.difficulty = name
        for key, button in self.diff_buttons.items():
            button.set_kind("primary" if key == name else "ghost")
        self.model_hint.configure(
            text=(learned_model_problem() or "") if name == "learned" else "")

    def _start(self) -> None:
        self.app.start_ai(self.app.difficulty)


class WatchSetupScreen(tk.Frame):
    """Pick two computer players (and the speed) and watch them play."""

    def __init__(self, app: "BattleshipApp"):
        super().__init__(app.root, bg=BG)
        self.app = app
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=30, pady=(18, 0))
        tk.Label(top, text=t("app.title"), font=app.f_h2, fg=MUTED,
                 bg=BG).pack(side="left")
        app.make_lang_button(top).pack(side="right")

        card = Card(self, app, width=700, height=510)
        card.place(relx=0.5, rely=0.48, anchor="center")
        inner = card.inner
        tk.Label(inner, text=t("watch.title"), font=app.f_h1, fg=TEXT, bg=PANEL,
                 anchor="w").pack(fill="x")
        tk.Label(inner, text=t("watch.desc"), font=app.f_small, fg=MUTED,
                 bg=PANEL, anchor="w", justify="left",
                 wraplength=640).pack(fill="x", pady=(6, 0))

        self.kind_buttons: list[dict[str, TextButton]] = [{}, {}]
        for side, key in ((0, "watch.left"), (1, "watch.right")):
            tk.Label(inner, text=t(key), font=app.f_body, fg=MUTED, bg=PANEL,
                     anchor="w").pack(fill="x", pady=(16, 6))
            row = tk.Frame(inner, bg=PANEL)
            row.pack(fill="x")
            for kind in WATCH_KINDS:
                button = TextButton(row, app, t(f"ai.{kind}"),
                                    lambda s_=side, k=kind: self._pick(s_, k),
                                    width=128, height=42, kind="ghost")
                button.pack(side="left", padx=(0, 10))
                self.kind_buttons[side][kind] = button

        tk.Label(inner, text=t("watch.speed"), font=app.f_body, fg=MUTED,
                 bg=PANEL, anchor="w").pack(fill="x", pady=(16, 6))
        speed_row = tk.Frame(inner, bg=PANEL)
        speed_row.pack(fill="x")
        self.speed_buttons: dict[str, TextButton] = {}
        for key, _ms in WATCH_SPEEDS:
            button = TextButton(speed_row, app, t(f"watch.{key}"),
                                lambda k=key: self._pick_speed(k),
                                width=96, height=38, kind="ghost")
            button.pack(side="left", padx=(0, 10))
            self.speed_buttons[key] = button

        self.hint = tk.Label(inner, text="", font=app.f_small, fg=HIT, bg=PANEL,
                             anchor="w", justify="left", wraplength=640)
        self.hint.pack(fill="x", pady=(12, 0))

        actions = tk.Frame(inner, bg=PANEL)
        actions.pack(fill="x", side="bottom", pady=(14, 0))
        TextButton(actions, app, t("common.back"), app.leave_to_menu,
                   width=140, height=44, kind="ghost").pack(side="left")
        TextButton(actions, app, t("watch.start"), self._start, width=200,
                   height=44, kind="primary").pack(side="right")
        self._refresh()

    def _pick(self, side: int, kind: str) -> None:
        self.app.watch_kinds[side] = kind
        self._refresh()

    def _pick_speed(self, key: str) -> None:
        self.app.watch_speed = key
        self._refresh()

    def _refresh(self) -> None:
        for side in (0, 1):
            for kind, button in self.kind_buttons[side].items():
                button.set_kind("primary" if self.app.watch_kinds[side] == kind
                                else "ghost")
        for key, button in self.speed_buttons.items():
            button.set_kind("primary" if self.app.watch_speed == key else "ghost")
        problem = learned_model_problem() if "learned" in self.app.watch_kinds else None
        self.hint.configure(text=problem or "")

    def _start(self) -> None:
        self.app.start_watch(self.app.watch_kinds[0], self.app.watch_kinds[1])


class PlacementScreen(tk.Frame):
    """Deploy your fleet: canvas board, ship dock and optional chat."""

    def __init__(self, app: "BattleshipApp"):
        super().__init__(app.root, bg=BG)
        self.app = app
        self.view = app.current_view()
        self._drag: str | None = None
        self._hover_cell: tuple[int, int] | None = None
        self.ghost_cells: list[tuple[int, int]] = []
        self.ghost_valid = True

        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=30, pady=(16, 4))
        left = tk.Frame(top, bg=BG)
        left.pack(side="left")
        tk.Label(left, text=t("placement.title"), font=app.f_h1, fg=TEXT,
                 bg=BG, anchor="w").pack(anchor="w")
        tk.Label(left, text=t("placement.subtitle"), font=app.f_small, fg=MUTED,
                 bg=BG, anchor="w").pack(anchor="w")
        app.make_lang_button(top).pack(side="right")

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=22, pady=(6, 14))
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, minsize=302)
        network = not app.local
        body.grid_columnconfigure(2, minsize=264 if network else 0, weight=0)

        # -- board ---------------------------------------------------------- #
        board_col = tk.Frame(body, bg=BG)
        board_col.grid(row=0, column=0, sticky="nsew")
        tk.Label(board_col, text=t("placement.your_waters"), font=app.f_h2,
                 fg=TEXT, bg=BG, anchor="w").pack(fill="x")
        self.board = BoardCanvas(board_col, app, kind="placement",
                                 interactive=True, on_cell=self._board_click,
                                 on_hover=self._board_hover,
                                 on_rotate=self._rotate)
        self.board.pack(fill="both", expand=True, pady=(4, 4))
        self.hint_label = tk.Label(board_col, text=t("placement.drag_hint"),
                                   font=app.f_small, fg=MUTED, bg=BG, anchor="w")
        self.hint_label.pack(fill="x")

        # -- dock ----------------------------------------------------------- #
        dock_card = Card(body, app)
        dock_card.grid(row=0, column=1, sticky="nsew", padx=(12, 0))
        dock = dock_card.inner
        self.dock_title = tk.Label(dock, text=t("placement.remaining"),
                                   font=app.f_h2, fg=TEXT, bg=PANEL, anchor="w")
        self.dock_title.pack(fill="x")
        tk.Label(dock, text=t("placement.rotate_hint"), font=app.f_small,
                 fg=MUTED, bg=PANEL, anchor="w").pack(fill="x", pady=(2, 8))
        chips = tk.Frame(dock, bg=PANEL)
        chips.pack(fill="x")
        self.chips: dict[str, ShipChip] = {}
        for name, size in engine.FLEET:
            chip = ShipChip(chips, app, name, size, self._chip_press,
                            self._chip_motion, self._chip_release)
            chip.pack(fill="x", pady=2)
            self.chips[name] = chip

        tk.Frame(dock, bg=LINE, height=1).pack(fill="x", pady=10)
        btn_row = tk.Frame(dock, bg=PANEL)
        btn_row.pack(fill="x")
        self.random_button = TextButton(btn_row, app, t("placement.random"),
                                        self._random, width=92, height=38,
                                        kind="ghost", font_key="f_small")
        self.random_button.pack(side="left", padx=(0, 6))
        self.clear_button = TextButton(btn_row, app, t("placement.clear"),
                                       self._clear, width=78, height=38,
                                       kind="ghost", font_key="f_small")
        self.clear_button.pack(side="left", padx=(0, 6))
        self.ready_button = TextButton(dock, app, t("placement.ready_btn"),
                                       self._ready, width=210, height=44,
                                       kind="primary")
        self.ready_button.pack(fill="x", pady=(10, 6))
        self.status_label = tk.Label(dock, text="", font=app.f_small, fg=MUTED,
                                     bg=PANEL, anchor="w", justify="left",
                                     wraplength=270)
        self.status_label.pack(fill="x")
        self.turn_label = tk.Label(dock, text="", font=app.f_small, fg=WIN,
                                   bg=PANEL, anchor="w")
        self.turn_label.pack(fill="x", pady=(4, 0))

        # -- chat (network only) -------------------------------------------- #
        self.chat: ChatBox | None = None
        if network:
            chat_card = Card(body, app)
            chat_card.grid(row=0, column=2, sticky="nsew", padx=(12, 0))
            self.chat = ChatBox(chat_card.inner, app, height=9)
            self.chat.pack(fill="both", expand=True)

        self.refresh()

    # -- placement state ---------------------------------------------------- #
    def _in_bounds_cells(self) -> list[tuple[int, int]]:
        name = self.app.placing_name
        if name is None:
            return []
        size = engine.FLEET_BY_NAME.get(name, 0)
        cell = self._hover_cell
        if cell is None:
            return []
        r, c = cell
        return [(r, c + i) if self.app.placing_h else (r + i, c)
                for i in range(size)]

    def _can_place(self, name: str, cells: list[tuple[int, int]]) -> bool:
        if any(not engine.in_bounds(r, c) for r, c in cells):
            return False
        occupied: set[tuple[int, int]] = set()
        for other, other_cells in self.app.placed.items():
            if other != name:
                occupied.update(other_cells)
        return not any(cell in occupied for cell in cells)

    def _update_ghost(self, r: int, c: int) -> None:
        if self.app.placing_name is None or self.app.placement_ready:
            self.ghost_cells = []
            self.board.set_ghost([])
            return
        self._hover_cell = (r, c)
        size = engine.FLEET_BY_NAME[self.app.placing_name]
        raw = [(r, c + i) if self.app.placing_h else (r + i, c)
               for i in range(size)]
        inside = [(rr, cc) for rr, cc in raw if engine.in_bounds(rr, cc)]
        valid = self._can_place(self.app.placing_name, raw)
        self.ghost_cells = inside
        self.ghost_valid = valid
        self.board.set_ghost(inside, valid)

    def _try_place(self, r: int, c: int) -> None:
        name = self.app.placing_name
        if name is None or self.app.placement_ready:
            return
        size = engine.FLEET_BY_NAME[name]
        cells = [(r, c + i) if self.app.placing_h else (r + i, c)
                 for i in range(size)]
        if not self._can_place(name, cells):
            self.app.toast(t("placement.invalid"), "error")
            return
        self.app.placed[name] = tuple(cells)
        self.app.placing_name = None
        self.ghost_cells = []
        self.board.set_ghost([])
        self.refresh()

    def _pick_up(self, name: str) -> None:
        cells = self.app.placed.pop(name, None)
        if cells:
            self.app.placing_h = len({cell[0] for cell in cells}) == 1
        self.app.placing_name = name
        self.ghost_cells = []
        self.board.set_ghost([])
        self.refresh()

    # -- board callbacks ---------------------------------------------------- #
    def _board_click(self, r: int, c: int) -> None:
        if self.app.placement_ready:
            return
        if self.app.placing_name is not None:
            self._try_place(r, c)
            return
        ship_name = None
        for name, cells in self.app.placed.items():
            if (r, c) in cells:
                ship_name = name
                break
        if ship_name is not None:
            self._pick_up(ship_name)

    def _board_hover(self, r, c) -> None:
        if r is None or self.app.placing_name is None:
            if self.ghost_cells:
                self.ghost_cells = []
                self.board.set_ghost([])
            return
        self._update_ghost(int(r), int(c))

    def _rotate(self) -> None:
        if self.app.placement_ready or self.app.placing_name is None:
            return
        self.app.placing_h = not self.app.placing_h
        if self._hover_cell is not None:
            self._update_ghost(*self._hover_cell)

    # -- dock callbacks ----------------------------------------------------- #
    def _chip_press(self, name: str) -> None:
        if self.app.placement_ready:
            return
        self._drag = name
        if name in self.app.placed:
            cells = self.app.placed.pop(name)
            self.app.placing_h = len({cell[0] for cell in cells}) == 1
        self.app.placing_name = name
        self.ghost_cells = []
        self.board.set_ghost([])
        self.refresh()

    def _chip_motion(self, name: str) -> None:
        if self._drag != name:
            return
        cell = self._pointer_cell()
        if cell is not None:
            self._update_ghost(*cell)
        elif self.ghost_cells:
            self.ghost_cells = []
            self.board.set_ghost([])

    def _chip_release(self, name: str) -> None:
        if self._drag != name:
            return
        self._drag = None
        cell = self._pointer_cell()
        if cell is not None:
            self._try_place(*cell)

    def _pointer_cell(self) -> tuple[int, int] | None:
        board = self.board
        if not board.winfo_ismapped():
            return None
        x = board.winfo_pointerx() - board.winfo_rootx()
        y = board.winfo_pointery() - board.winfo_rooty()
        if 0 <= x < board.winfo_width() and 0 <= y < board.winfo_height():
            return board.point_cell(x, y)
        return None

    # -- actions ------------------------------------------------------------ #
    def _random(self) -> None:
        if self.app.placement_ready:
            return
        fleet = engine.random_fleet()
        self.app.placed = {
            ship["name"]: tuple(tuple(cell) for cell in ship["cells"])
            for ship in fleet
        }
        self.app.placing_name = None
        self.ghost_cells = []
        self.board.set_ghost([])
        self.refresh()

    def _clear(self) -> None:
        if self.app.placement_ready:
            return
        self.app.placed.clear()
        self.app.placing_name = None
        self.ghost_cells = []
        self.board.set_ghost([])
        self.refresh()

    def _fleet_payload(self) -> list[dict] | None:
        payload = []
        for name, _size in engine.FLEET:
            cells = self.app.placed.get(name)
            if cells is None:
                return None
            payload.append({"name": name,
                            "cells": [[int(r), int(c)] for r, c in cells]})
        return payload

    def _ready(self) -> None:
        if self.app.placement_ready:
            return
        payload = self._fleet_payload()
        if payload is None:
            self.app.toast(t("placement.need_all"), "error")
            return
        try:
            engine.validate_placement(payload)
        except engine.PlacementError as exc:
            self.app.toast(i18n.err_text(exc.code), "error")
            return
        self.app.placement_ready = True
        self.board.set_interactive(False)
        self.refresh()
        if self.app.local:
            self.app.start_local_battle(payload)
        else:
            self.app.send_place(payload)

    def rotate(self) -> None:
        self._rotate()

    def set_ready_false(self) -> None:
        self.app.placement_ready = False
        self.board.set_interactive(True)
        self.refresh()

    # -- rendering ---------------------------------------------------------- #
    def refresh(self) -> None:
        ships = [{"name": name, "cells": [list(cell) for cell in cells]}
                 for name, cells in self.app.placed.items()]
        self.board.set_ships(ships)
        self.board.set_ghost(self.ghost_cells, self.ghost_valid)
        self.board.set_interactive(not self.app.placement_ready)
        for name, chip in self.chips.items():
            chip.placed = name in self.app.placed
            chip.selected = name == self.app.placing_name
            chip.redraw()
        remaining = max(0, len(engine.FLEET) - len(self.app.placed))
        self.dock_title.configure(
            text=t("placement.all_placed") if remaining == 0
            else f"{t('placement.remaining')}  {remaining}")
        ready = self.app.placement_ready
        self.ready_button.set_enabled(remaining == 0 and not ready)
        for button in (self.random_button, self.clear_button):
            button.set_enabled(not ready)
        self.hint_label.configure(
            text=t("placement.drag_hint") if not ready
            else t("placement.waiting"))
        self._refresh_ready_labels()
        self._refresh_chat()

    def _refresh_ready_labels(self) -> None:
        view = self.view or {}
        players = view.get("players") or []
        you = int(view.get("you") or 0)

        def ready_of(index: int) -> str:
            if 0 <= index < len(players) and players[index]:
                return t("common.ready") if players[index].get("ready") else t("common.not_ready")
            return t("common.not_ready")

        ours = t("common.ready") if self.app.placement_ready else ready_of(you)
        theirs = ready_of(1 - you)
        self.status_label.configure(
            text=t("placement.ready_status", you=ours, them=theirs))
        starter = view.get("starter")
        if starter is None:
            self.turn_label.configure(text="")
        elif int(starter) == you:
            self.turn_label.configure(text=t("placement.turn_first"))
        else:
            self.turn_label.configure(text=t("placement.turn_second"))
        if self.app.placement_ready:
            self.status_label.configure(
                text=f"{t('placement.waiting')}  {t('placement.ready_status', you=ours, them=theirs)}")

    def _refresh_chat(self) -> None:
        if self.chat is not None and self.view is not None:
            self.chat.set_messages(self.view.get("chat") or [])

    def update_view(self, view: dict) -> None:
        self.view = view
        players = view.get("players") or []
        you = int(view.get("you") or 0)
        if 0 <= you < len(players) and players[you] and players[you].get("ready"):
            self.app.placement_ready = True
        self.refresh()


class BattleScreen(tk.Frame):
    """Two oceans side by side plus fleet status, battle log and chat."""

    def __init__(self, app: "BattleshipApp", view: dict):
        super().__init__(app.root, bg=BG)
        self.app = app
        self.view = view or {}
        self.spectate = bool(self.view.get("spectate"))   # AI-vs-AI viewer
        self.pause_button: TextButton | None = None
        self.speed_buttons: dict[str, TextButton] = {}
        self._pending_shot = False
        self.overlay: tk.Frame | None = None
        self._log_key = None
        self._chat_key = None

        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=30, pady=(14, 4))
        head_left = tk.Frame(top, bg=BG)
        head_left.pack(side="left")
        tk.Label(head_left, text=t("battle.title"), font=app.f_small, fg=MUTED,
                 bg=BG, anchor="w").pack(anchor="w")
        self.banner = tk.Label(head_left, text="", font=app.f_h1, fg=ACCENT,
                               bg=BG, anchor="w")
        self.banner.pack(anchor="w")
        self.round_label = tk.Label(head_left, text="", font=app.f_small,
                                    fg=MUTED, bg=BG, anchor="w")
        self.round_label.pack(anchor="w")
        head_right = tk.Frame(top, bg=BG)
        head_right.pack(side="right")
        self.score_label = tk.Label(head_right, text="", font=app.f_body,
                                    fg=ACCENT, bg=BG, anchor="e")
        self.score_label.pack(anchor="e", pady=(0, 4))
        if self.spectate:
            controls = tk.Frame(head_right, bg=BG)
            controls.pack(anchor="e")
            self.pause_button = TextButton(controls, app, t("watch.pause"),
                                           app.watch_toggle_pause, width=84,
                                           height=34, kind="ghost",
                                           font_key="f_small")
            self.pause_button.pack(side="left", padx=(0, 6))
            for key, _ms in WATCH_SPEEDS:
                button = TextButton(controls, app, t(f"watch.{key}"),
                                    lambda k=key: app.watch_set_speed(k),
                                    width=58, height=34, kind="ghost",
                                    font_key="f_small")
                button.pack(side="left", padx=(0, 4))
                self.speed_buttons[key] = button
            app.make_lang_button(controls).pack(side="left", padx=(8, 0))
        else:
            app.make_lang_button(head_right).pack(anchor="e")

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=20, pady=(4, 12))
        body.grid_rowconfigure(0, weight=1)
        for column in (0, 1):
            body.grid_columnconfigure(column, weight=1)
        body.grid_columnconfigure(2, minsize=274)

        own_col = tk.Frame(body, bg=BG)
        own_col.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        tk.Label(own_col, text=self._waters_title(0), font=app.f_h2, fg=TEXT,
                 bg=BG, anchor="w").pack(fill="x")
        self.own_board = BoardCanvas(own_col, app, kind="own",
                                     interactive=False)
        self.own_board.pack(fill="both", expand=True, pady=(4, 0))

        enemy_col = tk.Frame(body, bg=BG)
        enemy_col.grid(row=0, column=1, sticky="nsew", padx=(6, 6))
        tk.Label(enemy_col, text=self._waters_title(1), font=app.f_h2,
                 fg=TEXT, bg=BG, anchor="w").pack(fill="x")
        # In the AI viewer both oceans show their hulls, so both boards are "own".
        self.enemy_board = BoardCanvas(enemy_col, app,
                                       kind="own" if self.spectate else "enemy",
                                       interactive=not self.spectate,
                                       on_cell=self._fire)
        self.enemy_board.pack(fill="both", expand=True, pady=(4, 0))

        side = tk.Frame(body, bg=BG)
        side.grid(row=0, column=2, sticky="nsew")
        side.grid_columnconfigure(0, weight=1)
        side.grid_rowconfigure(1, weight=1)

        fleet_card = Card(side, app, width=10, height=238)
        fleet_card.grid(row=0, column=0, sticky="ew")
        fleet = fleet_card.inner
        tk.Label(fleet, text=t("watch.fleet_title" if self.spectate
                               else "battle.fleet_title"),
                 font=app.f_h2, fg=TEXT, bg=PANEL, anchor="w").pack(fill="x")
        self.fleet_labels: dict[str, tk.Label] = {}
        for name, _size in engine.FLEET:
            label = tk.Label(fleet, text="", font=app.f_small, fg=TEXT, bg=PANEL,
                             anchor="w")
            label.pack(fill="x", pady=1)
            self.fleet_labels[name] = label
        self.enemy_left_label = tk.Label(fleet, text="", font=app.f_body,
                                         fg=ACCENT, bg=PANEL, anchor="w")
        self.enemy_left_label.pack(fill="x", pady=(6, 0))
        self.stats_label = tk.Label(fleet, text="", font=app.f_small, fg=MUTED,
                                    bg=PANEL, anchor="w")
        self.stats_label.pack(fill="x")

        log_card = Card(side, app, width=10, height=10)
        log_card.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        self.log_panel = LogPanel(log_card.inner, app, t("battle.log_title"),
                                  height=7)
        self.log_panel.pack(fill="both", expand=True)

        self.chat: ChatBox | None = None
        if not app.local and not self.spectate:
            chat_card = Card(side, app, width=10, height=214)
            chat_card.grid(row=2, column=0, sticky="ew", pady=(10, 0))
            self.chat = ChatBox(chat_card.inner, app, height=5)
            self.chat.pack(fill="both", expand=True)

        self.update(view)

    # -- helpers ------------------------------------------------------------ #
    def is_over(self) -> bool:
        return (self.view or {}).get("state") == "over"

    def _waters_title(self, index: int) -> str:
        if self.spectate:
            return t("watch.waters", name=self._player_name(index))
        return t("battle.your_waters" if index == 0 else "battle.enemy_waters")

    def refresh_watch_controls(self) -> None:
        if not self.spectate or self.pause_button is None:
            return
        self.pause_button.set_text(
            t("watch.resume") if self.app.watch_paused else t("watch.pause"))
        self.pause_button.set_enabled(not self.is_over())
        for key, button in self.speed_buttons.items():
            button.set_kind("primary" if key == self.app.watch_speed else "ghost")

    def _update_spectate(self) -> None:
        """Render an AI-vs-AI snapshot: both fleets visible, no input."""
        view = self.view
        players = view.get("players") or []
        over = self.is_over()
        if over:
            self.banner.configure(
                text=t("watch.winner", name=self._player_name(view.get("winner"))),
                fg=WIN)
        else:
            self.banner.configure(
                text=t("watch.turn", name=self._player_name(view.get("turn"))),
                fg=ACCENT)
        stats = view.get("stats") or {"shots": [0, 0], "hits": [0, 0]}
        shots = stats.get("shots") or [0, 0]
        self.round_label.configure(
            text=t("battle.turn_banner", n=int(sum(shots)) + 1))
        wins = view.get("wins") or [0, 0]
        self.score_label.configure(text=t("watch.score", a=wins[0], b=wins[1]))

        left = view.get("your_board") or {}
        right = view.get("enemy_full") or {}
        left_ships = left.get("ships") or []
        right_ships = right.get("ships") or []
        self.own_board.set_ships(left_ships)
        self.own_board.set_shots(left.get("shots") or [])
        self.enemy_board.set_ships(right_ships)
        self.enemy_board.set_shots(right.get("shots") or [])

        by_left = {ship.get("name"): ship for ship in left_ships}
        by_right = {ship.get("name"): ship for ship in right_ships}

        def status(ship, name) -> str:
            if ship is None:
                return "–"
            if ship.get("sunk"):
                return "✕"
            return f"{len(ship.get('hits') or [])}/{engine.FLEET_BY_NAME.get(name, 0)}"

        for name, label in self.fleet_labels.items():
            label.configure(
                text=f"{i18n.ship_label(name)}   "
                     f"{status(by_left.get(name), name)}  |  "
                     f"{status(by_right.get(name), name)}",
                fg=TEXT)

        def cells_left(ships: list[dict]) -> int:
            return sum(len(s.get("cells") or []) - len(s.get("hits") or [])
                       for s in ships)

        self.enemy_left_label.configure(
            text=t("watch.remaining", a=cells_left(left_ships),
                   b=cells_left(right_ships)))
        self.stats_label.configure(
            text=t("watch.stats", a=int(shots[0]), b=int(shots[1])))
        self._update_log(players)
        self.refresh_watch_controls()
        if over:
            self._show_overlay(0)
        else:
            self._drop_overlay()

    def _fill_spectate_overlay(self) -> None:
        view = self.view
        self.over_result.configure(
            text=t("watch.winner", name=self._player_name(view.get("winner"))),
            fg=WIN)
        shots = (view.get("stats") or {}).get("shots") or [0, 0]
        self.over_detail.configure(
            text=t("watch.over_detail", sa=int(shots[0]), sb=int(shots[1])))
        wins = view.get("wins") or [0, 0]
        self.over_score.configure(text=t("watch.score", a=wins[0], b=wins[1]))
        self.rematch_button.set_enabled(True)
        self.over_status.configure(text="", fg=MUTED)

    def _player_name(self, index) -> str:
        players = (self.view or {}).get("players") or []
        if isinstance(index, int) and 0 <= index < len(players) and players[index]:
            return players[index].get("name") or t("common.opponent")
        return t("common.opponent")

    def _fire(self, r: int, c: int) -> None:
        if self._pending_shot or self.is_over():
            return
        self._pending_shot = True
        if not self.app.fire_at(r, c):
            self._pending_shot = False

    # -- rendering ---------------------------------------------------------- #
    def update(self, view: dict) -> None:
        self.view = view or {}
        self._pending_shot = False
        if self.spectate:
            self._update_spectate()
            return
        you = int(self.view.get("you") or 0)
        players = self.view.get("players") or []
        over = self.is_over()
        your_turn = self.view.get("turn") == you and not over

        if over:
            winner = self.view.get("winner")
            if winner == you:
                text = t("over.win")
                color = WIN
            else:
                text = t("over.lose")
                color = HIT
            self.banner.configure(text=text, fg=color)
        else:
            self.banner.configure(
                text=t("battle.your_turn") if your_turn else t("battle.enemy_turn"),
                fg=ACCENT if your_turn else MUTED)

        stats = self.view.get("stats") or {"shots": [0, 0], "hits": [0, 0]}
        shots = stats.get("shots") or [0, 0]
        self.round_label.configure(text=t("battle.turn_banner",
                                          n=int(sum(shots)) + 1))
        wins = self.view.get("wins") or [0, 0]
        if len(wins) >= 2:
            self.score_label.configure(
                text=t("over.wins_record", you=wins[you], them=wins[1 - you]))
        else:
            self.score_label.configure(text="")

        your_board = self.view.get("your_board") or {}
        self.own_board.set_ships(your_board.get("ships") or [])
        self.own_board.set_shots(your_board.get("shots") or [])
        enemy = self.view.get("enemy") or {}
        enemy_shots = enemy.get("shots") or []
        self.enemy_board.set_shots(enemy_shots)
        self.enemy_board.set_sunk_ships(enemy.get("sunk") or [])
        self.enemy_board.set_interactive(your_turn and not over)

        self._update_fleet(your_board.get("ships") or [], enemy, you, stats)
        self._update_log(players)
        self._update_chat()

        if over:
            self._show_overlay(you)
        else:
            self._drop_overlay()

    def _update_fleet(self, ships: list[dict], enemy: dict, you: int,
                      stats: dict) -> None:
        by_name = {ship.get("name"): ship for ship in ships}
        for name in self.fleet_labels:
            ship = by_name.get(name)
            if ship is None:
                self.fleet_labels[name].configure(text=i18n.ship_label(name),
                                                  fg=MUTED)
                continue
            hits = len(ship.get("hits") or [])
            size = engine.FLEET_BY_NAME.get(name, 0)
            if ship.get("sunk"):
                self.fleet_labels[name].configure(
                    text=f"{i18n.ship_label(name)}  ✕", fg=HIT)
            else:
                self.fleet_labels[name].configure(
                    text=f"{i18n.ship_label(name)}  {hits}/{size}", fg=TEXT)
        sunk_cells = sum(len(ship.get("cells") or [])
                         for ship in enemy.get("sunk") or [])
        remaining = max(0, engine.TOTAL_SHIP_CELLS - sunk_cells)
        self.enemy_left_label.configure(
            text=t("battle.remaining_enemy", n=remaining))
        shots = (stats.get("shots") or [0, 0])[you]
        hits = (stats.get("hits") or [0, 0])[you]
        self.stats_label.configure(text=t("over.stats", shots=shots, hits=hits))

    def _log_lines(self) -> list[str]:
        entries = self.view.get("log") or []
        lines = []
        for entry in entries:
            line = self.app.format_log(entry, self.view)
            if line:
                lines.append(line)
        return lines or [t("battle.log_empty")]

    def _update_log(self, players: list[dict]) -> None:
        entries = self.view.get("log") or []
        key = (len(entries), self.app.local)
        if key == self._log_key:
            return
        self._log_key = key
        self.log_panel.set_lines(self._log_lines())

    def _update_chat(self) -> None:
        if self.chat is None:
            return
        chat = self.view.get("chat") or []
        key = len(chat)
        if key == self._chat_key:
            return
        self._chat_key = key
        self.chat.set_messages(chat)

    # -- game-over overlay -------------------------------------------------- #
    def _show_overlay(self, you: int) -> None:
        if self.overlay is None:
            self.overlay = tk.Frame(self, bg=BG)
            self.overlay.place(x=0, y=0, relwidth=1, relheight=1)
            card = Card(self.overlay, self.app, width=540, height=392)
            card.place(relx=0.5, rely=0.5, anchor="center")
            inner = card.inner
            self.over_result = tk.Label(inner, text="", font=self.app.f_hero,
                                        fg=WIN, bg=PANEL)
            self.over_result.pack(pady=(4, 6))
            self.over_detail = tk.Label(inner, text="", font=self.app.f_body,
                                        fg=TEXT, bg=PANEL)
            self.over_detail.pack(pady=(0, 4))
            self.over_score = tk.Label(inner, text="", font=self.app.f_h2,
                                       fg=ACCENT, bg=PANEL)
            self.over_score.pack(pady=(0, 12))
            self.over_status = tk.Label(inner, text="", font=self.app.f_small,
                                        fg=MUTED, bg=PANEL)
            self.over_status.pack(pady=(0, 10))
            actions = tk.Frame(inner, bg=PANEL)
            actions.pack(side="bottom")
            self.rematch_button = TextButton(actions, self.app,
                                             t("watch.next" if self.spectate
                                               else "over.rematch_btn"),
                                             self.app.request_rematch, width=180,
                                             height=44, kind="primary")
            self.rematch_button.pack(side="left", padx=6)
            TextButton(actions, self.app, t("over.new_game"),
                       self.app.leave_to_menu, width=180, height=44,
                       kind="ghost").pack(side="left", padx=6)
            self.overlay.lift()

        if self.spectate:
            self._fill_spectate_overlay()
            return

        winner = self.view.get("winner")
        forfeit = bool(self.view.get("forfeit"))
        if winner == you:
            result = t("over.win")
            color = WIN
        else:
            result = t("over.lose")
            color = HIT
        self.over_result.configure(text=result, fg=color)

        if forfeit:
            detail = t("over.forfeit_win") if winner == you else t("over.forfeit_lose")
        else:
            stats = self.view.get("stats") or {}
            shots = (stats.get("shots") or [0, 0])[you]
            hits = (stats.get("hits") or [0, 0])[you]
            detail = t("over.stats", shots=shots, hits=hits)
        self.over_detail.configure(text=detail)

        wins = self.view.get("wins") or [0, 0]
        if len(wins) >= 2:
            self.over_score.configure(
                text=t("over.wins_record", you=wins[you], them=wins[1 - you]))

        rematch = self.view.get("rematch") or [False, False]
        mine = bool(rematch[you]) if you < len(rematch) else False
        theirs = bool(rematch[1 - you]) if 1 - you < len(rematch) else False
        if mine:
            self.rematch_button.set_enabled(False)
            self.over_status.configure(text=t("over.rematch_wait"), fg=MUTED)
        elif theirs:
            self.rematch_button.set_enabled(True)
            self.over_status.configure(text=t("over.rematch_ready"), fg=ACCENT)
        else:
            self.rematch_button.set_enabled(True)
            self.over_status.configure(text="", fg=MUTED)

    def _drop_overlay(self) -> None:
        if self.overlay is not None:
            try:
                self.overlay.destroy()
            except tk.TclError:
                pass
            self.overlay = None


# --------------------------------------------------------------------------- #
# Help modal
# --------------------------------------------------------------------------- #

class HelpDialog(tk.Toplevel):
    def __init__(self, app: "BattleshipApp"):
        super().__init__(app.root)
        self.app = app
        self.configure(bg=BG)
        self.title(t("help.title"))
        self.resizable(False, False)
        self.transient(app.root)
        width, height = 640, 560
        root_x = app.root.winfo_rootx()
        root_y = app.root.winfo_rooty()
        self.geometry(f"{width}x{height}+{root_x + 120}+{root_y + 80}")

        card = Card(self, app, width=width - 36, height=height - 36)
        card.place(relx=0.5, rely=0.5, anchor="center")
        inner = card.inner
        tk.Label(inner, text=t("help.title"), font=app.f_h1, fg=ACCENT,
                 bg=PANEL, anchor="w").pack(fill="x", pady=(0, 10))
        for key in ("help.place", "help.fire", "help.extra", "help.win",
                    "help.net", "help.keys"):
            tk.Label(inner, text=t(key), font=app.f_body, fg=TEXT, bg=PANEL,
                     anchor="w", justify="left", wraplength=520).pack(
                         fill="x", pady=3)
        actions = tk.Frame(inner, bg=PANEL)
        actions.pack(side="bottom", pady=(10, 0))
        TextButton(actions, app, t("common.close"), self.destroy, width=150,
                   height=40, kind="primary").pack()
        self.bind("<Escape>", lambda _e: self.destroy())
        try:
            self.grab_set()
        except tk.TclError:
            pass


# --------------------------------------------------------------------------- #
# Application controller
# --------------------------------------------------------------------------- #

class BattleshipApp:
    """Owns the Tk root, one screen at a time, the network and the local AI."""

    def __init__(self, root=None, mode: str = "menu", code=None, ip=None,
                 port=None, lang=None, ai_difficulty: str = "normal"):
        self.root = root if root is not None else tk.Tk()
        self._owns_root = root is None
        if lang in i18n.LANGUAGES:
            i18n.set_lang(lang)
        elif lang is None:
            i18n.load_saved_lang()

        self.root.title(t("app.title"))
        self.root.configure(bg=BG)
        self.root.geometry(f"{WINDOW_W}x{WINDOW_H}")
        self.root.minsize(MIN_W, MIN_H)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self._build_fonts()

        # -- runtime state -------------------------------------------------- #
        self.mode = "menu"
        self.difficulty = ai_difficulty if ai_difficulty in AI_KINDS else "normal"
        self.nickname = _load_saved_name() or _os_username()
        self.join_code = str(code or "")
        self.join_ip = str(ip or "")
        self.join_port = str(port or "")
        self.manual = bool(ip or port)
        self.rooms: list[dict] = []

        self.server = None
        self.net = None
        self.you: int | None = None
        self.sync: dict | None = None
        self.room_code: str | None = None
        self.discovery_active = True
        self.room_ip: str = "127.0.0.1"
        self.room_port: int = 0

        self.screen: tk.Frame | None = None
        self.screen_name = ""
        self._last_view: dict | None = None
        self._last_notice = None
        self._closing = False
        self._poll_job = None
        self._discovering = False
        self._events: queue.Queue = queue.Queue()
        self._toast_label = None
        self._toast_job = None
        self.lang_button = None
        self.menu_cards: list[MenuCard] = []

        # -- local (AI) match state ----------------------------------------- #
        self.local = False
        self.local_game: engine.Game | None = None
        self.local_ai = None
        self.local_wins = [0, 0]
        self._ai_starter = 0
        self._ai_busy = False
        self._local_counted = False

        # -- AI-vs-AI viewer state --------------------------------------------- #
        self.watching = False
        self.watch_game: engine.Game | None = None
        self.watch_ais: list = [None, None]
        self.watch_kinds: list[str] = ["learned", "hard"]
        self.watch_speed = "normal"
        self.watch_paused = False
        self.watch_wins = [0, 0]
        self.watch_starter = 0
        self._watch_job = None
        self._watch_counted = False

        # -- placement state (persists across language rebuilds) ------------ #
        self.placed: dict[str, tuple[tuple[int, int], ...]] = {}
        self.placing_name: str | None = None
        self.placing_h = True
        self.placement_ready = False

        self.root.bind("<Escape>", self._on_escape)
        self.root.bind("<Key-r>", self._on_rotate_key)
        self.root.bind("<Key-R>", self._on_rotate_key)

        self.show_menu()
        if mode != "menu":
            self.root.after(80, lambda: self._dispatch_mode(mode))
        self._poll_job = self.root.after(POLL_MS, self._pump)

    # -- fonts -------------------------------------------------------------- #
    def _build_fonts(self) -> None:
        try:
            base = tkfont.nametofont("TkDefaultFont").actual("family")
        except tk.TclError:
            base = "Helvetica"

        def make(size: int, bold: bool = False) -> tkfont.Font:
            try:
                return tkfont.Font(root=self.root, family=base, size=size,
                                   weight="bold" if bold else "normal")
            except tk.TclError:  # pragma: no cover - exotic Tk builds
                return tkfont.Font(root=self.root, size=size,
                                   weight="bold" if bold else "normal")

        self.f_hero = make(38, True)
        self.f_h1 = make(23, True)
        self.f_h2 = make(16, True)
        self.f_body = make(13)
        self.f_small = make(11)
        self.f_tiny = make(9)
        self.f_chip = make(12)
        self.f_button = make(13, True)
        try:
            self.f_code = tkfont.Font(root=self.root, family="Courier", size=54,
                                      weight="bold")
        except tk.TclError:  # pragma: no cover
            self.f_code = self.f_hero

    # -- language ----------------------------------------------------------- #
    def lang_toggle_text(self) -> str:
        other = "en" if i18n.get_lang() == "zh" else "zh"
        return i18n.language_name(other)

    def make_lang_button(self, parent, width: int = 122,
                         height: int = 34) -> TextButton:
        button = TextButton(parent, self, self.lang_toggle_text(),
                            self.toggle_language, width=width, height=height,
                            kind="ghost", font_key="f_small")
        self.lang_button = button
        return button

    def toggle_language(self) -> None:
        i18n.toggle_lang()
        i18n.save_lang()
        self.root.title(t("app.title"))
        self._rebuild()

    def _rebuild(self) -> None:
        name = self.screen_name
        if name == "menu":
            self.show_menu()
        elif name == "host":
            self.show_host()
        elif name == "join":
            self.show_join()
        elif name == "ai_setup":
            self.show_ai_setup()
        elif name == "watch_setup":
            self.show_watch_setup()
        elif name == "placement":
            self.show_placement()
        elif name == "battle":
            if self.watching:
                self.show_battle(self.watch_view())
            else:
                self.show_battle()
        else:
            self.show_menu()

    # -- screen plumbing ---------------------------------------------------- #
    def _set_screen(self, screen: tk.Frame, name: str) -> None:
        if self.screen is not None:
            try:
                self.screen.destroy()
            except tk.TclError:
                pass
        self.screen = screen
        self.screen_name = name
        screen.pack(fill="both", expand=True)

    def show_menu(self) -> None:
        self.mode = "menu"
        self._set_screen(MenuScreen(self), "menu")

    def show_host(self) -> None:
        self._set_screen(HostScreen(self), "host")

    def show_join(self) -> None:
        self.mode = "join"
        self._set_screen(JoinScreen(self), "join")

    def show_ai_setup(self) -> None:
        self.mode = "ai"
        self._set_screen(AiSetupScreen(self), "ai_setup")

    def show_watch_setup(self) -> None:
        self.mode = "watch"
        self._set_screen(WatchSetupScreen(self), "watch_setup")

    def show_placement(self) -> None:
        self._set_screen(PlacementScreen(self), "placement")

    def show_battle(self, view: dict | None = None) -> None:
        if view is not None:
            self._last_view = view
        if self._last_view is None:
            self._last_view = self.current_view()
        if self._last_view is None:
            self.show_menu()
            return
        self._set_screen(BattleScreen(self, self._last_view), "battle")

    def show_help(self) -> None:
        try:
            HelpDialog(self)
        except tk.TclError:
            pass

    # -- dispatch from CLI ------------------------------------------------ #
    def _dispatch_mode(self, mode: str) -> None:
        if self._closing:
            return
        if mode == "host":
            self.begin_host()
        elif mode == "join":
            self.show_join()
            if self.join_code or self.join_ip:
                self.root.after(150, self._auto_join)
        elif mode == "ai":
            self.show_ai_setup()
        elif mode == "watch":
            self.show_watch_setup()

    def _auto_join(self) -> None:
        if self._closing or self.screen_name != "join":
            return
        screen = self.screen
        if isinstance(screen, JoinScreen):
            screen.auto_join()

    # -- queue / polling ---------------------------------------------------- #
    def post(self, callback) -> None:
        self._events.put(callback)

    def _pump(self) -> None:
        if self._closing:
            return
        try:
            while True:
                callback = self._events.get_nowait()
                try:
                    callback()
                except Exception:  # noqa: BLE001 - never kill the Tk loop
                    pass
        except queue.Empty:
            pass
        net = self.net
        if net is not None:
            try:
                messages = net.poll() or []
            except Exception:  # noqa: BLE001 - treat as disconnect
                messages = [{"t": "closed", "reason": "error"}]
            for message in messages:
                if isinstance(message, dict):
                    self._on_net_message(message)
                if self._closing:
                    return
        if not self._closing:
            self._poll_job = self.root.after(POLL_MS, self._pump)

    # -- host / join -------------------------------------------------------- #
    def begin_host(self, port=None) -> None:
        self._close_client()
        self._stop_server()
        try:
            from net_server import GameServer
            server = GameServer(tcp_port=int(port or 0), discovery=True)
            code, tcp_port = server.start()
        except Exception:  # noqa: BLE001 - import or bind failure
            self.toast(i18n.err_text("server_error"), "error")
            self.show_menu()
            return
        self.server = server
        self.discovery_active = bool(getattr(server, "discovery_active", True))
        self.mode = "host"
        self.room_code = str(code)
        self.room_ip = getattr(server, "lan_ip", "") or "127.0.0.1"
        self.room_port = int(tcp_port)
        self.show_host()
        self._connect_worker("127.0.0.1", self.room_port,
                             self._clean_name(self.nickname), retries=10)

    def attempt_join(self, name, code, ip, port) -> None:
        code = str(code or "").strip()
        name = self._clean_name(name)
        if not (len(code) == 4 and code.isdigit()):
            self.toast(t("join.need_code"), "error")
            return
        ip = str(ip or "").strip()
        port_text = str(port or "").strip()
        if self.manual or ip or port_text:
            if not _valid_ip(ip):
                self.toast(t("join.bad_ip"), "error")
                return
            try:
                port_number = int(port_text)
            except ValueError:
                self.toast(t("join.need_port"), "error")
                return
            if not 1 <= port_number <= 65535:
                self.toast(t("join.need_port"), "error")
                return
            self._begin_join(ip, port_number, name)
            return
        for room in self.rooms:
            if str(room.get("code")) == code:
                self.join_discovered(room)
                return
        if self._discovering:
            return
        if self.screen_name == "join" and isinstance(self.screen, JoinScreen):
            self.screen.set_status(t("net.discovery"))
            self.screen.search_button.set_enabled(False)
        self.discover_rooms_async(
            lambda rooms: self._join_after_discover(rooms, code, name))

    def _join_after_discover(self, rooms: list[dict], code: str,
                             name: str) -> None:
        if self.screen_name == "join" and isinstance(self.screen, JoinScreen):
            self.screen.search_button.set_enabled(True)
            self.screen.set_rooms(rooms)
            self.screen.set_status(t("join.found") if rooms else t("join.not_found"))
        for room in rooms:
            if str(room.get("code")) == code:
                self.join_discovered(room)
                return
        self.toast(t("join.not_found"), "error")

    def join_discovered(self, room: dict) -> None:
        try:
            host = str(room.get("host") or "")
            port = int(room.get("port") or 0)
        except (TypeError, ValueError):
            self.toast(i18n.err_text("connect_failed"), "error")
            return
        if not host or not port:
            self.toast(i18n.err_text("connect_failed"), "error")
            return
        self.join_code = str(room.get("code") or self.join_code)
        self._begin_join(host, port, self._clean_name(self.nickname))

    def _begin_join(self, host: str, port: int, name: str) -> None:
        self._close_client()
        self.join_ip = host
        self.join_port = str(port)
        if self.screen_name == "join" and isinstance(self.screen, JoinScreen):
            self.screen.set_status(t("common.connecting"))
            self.screen.join_button.set_enabled(False)
        self._connect_worker(host, port, name, retries=1)

    def _connect_worker(self, host: str, port: int, name: str,
                        retries: int = 1) -> None:
        def run() -> None:
            last_code = "connect_failed"
            for attempt in range(max(1, retries)):
                if self._closing:
                    return
                try:
                    from net_client import NetClient
                except Exception:  # noqa: BLE001 - module missing
                    last_code = "connect_failed"
                    break
                client = NetClient()
                try:
                    client.connect(host, int(port), name, timeout=4.0)
                    self.post(lambda c=client: self._on_connected(c))
                    return
                except Exception as exc:  # noqa: BLE001 - NetError or socket
                    last_code = getattr(exc, "code", None) or "connect_failed"
                    try:
                        client.close()
                    except Exception:  # noqa: BLE001
                        pass
                    if attempt + 1 < retries:
                        time.sleep(0.35)
            code = last_code
            self.post(lambda: self._on_connect_error(code))

        threading.Thread(target=run, daemon=True, name="bs-connect").start()

    def _on_connected(self, client) -> None:
        if self._closing:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
            return
        self.net = client
        if client.you is not None:
            self.you = int(client.you)
        if client.code:
            self.room_code = str(client.code)
        if self.screen_name != "host":
            self.show_host()
        self.toast(t("common.connected"))

    def _on_connect_error(self, code: str) -> None:
        self.toast(i18n.err_text(code), "error")
        if self.mode == "host":
            self._stop_server()
            self.show_menu()
            return
        if self.screen_name != "join":
            self.show_join()
        elif isinstance(self.screen, JoinScreen):
            self.screen.connect_failed()

    def discover_rooms_async(self, callback) -> None:
        if self._discovering:
            return
        self._discovering = True

        def run() -> None:
            try:
                from net_client import discover_rooms
                rooms = discover_rooms() or []
            except Exception:  # noqa: BLE001 - UDP may be blocked
                rooms = []
            self.post(lambda: self._discover_done(rooms, callback))

        threading.Thread(target=run, daemon=True, name="bs-discover").start()

    def _discover_done(self, rooms: list[dict], callback) -> None:
        self._discovering = False
        self.rooms = [room for room in rooms if isinstance(room, dict)]
        try:
            callback(self.rooms)
        except Exception:  # noqa: BLE001 - screen may be gone
            pass

    def _clean_name(self, raw) -> str:
        name = str(raw or "").strip()[:16]
        if not name:
            name = _load_saved_name() or _os_username()
        self.nickname = name
        _save_name(name)
        return name

    # -- network message handling ------------------------------------------ #
    def _on_net_message(self, message: dict) -> None:
        kind = message.get("t")
        if kind == "sync":
            self._apply_view(message)
        elif kind == "welcome":
            if message.get("you") is not None:
                self.you = int(message["you"])
            if message.get("code"):
                self.room_code = str(message["code"])
        elif kind == "error":
            code = str(message.get("code") or "server_error")
            self.toast(i18n.err_text(code), "error")
            if (self.screen_name == "placement"
                    and code in ("bad_placement", "wrong_fleet", "overlap",
                                 "bad_phase", "not_ready", "bad_size",
                                 "out_of_bounds", "unknown_ship", "bad_shape")):
                if isinstance(self.screen, PlacementScreen):
                    self.screen.set_ready_false()
        elif kind == "bye":
            self.toast(t("sys.host_left"), "error")
            self.leave_to_menu()
        elif kind == "closed":
            self.toast(i18n.err_text("connection_lost"), "error")
            self.leave_to_menu()
        # pong: nothing to render.

    def _apply_view(self, view: dict) -> None:
        self.sync = view
        self._last_view = view
        if view.get("you") is not None:
            self.you = int(view["you"])
        if view.get("code"):
            self.room_code = str(view["code"])
        notice = view.get("notice")
        if notice and notice != self._last_notice:
            key = {
                "opponent_left": "sys.opponent_left",
                "new_opponent": "sys.opponent_joined",
                "rematch": "sys.rematch_start",
            }.get(str(notice))
            if key:
                self.toast(t(key))
        self._last_notice = notice

        state = str(view.get("state") or "lobby")
        if state == "placing":
            self._enter_placement(view)
        elif state in ("battle", "over"):
            self._enter_battle(view)
        else:
            self._enter_lobby(view)

    def _enter_placement(self, view: dict) -> None:
        if self.screen_name == "placement" and isinstance(self.screen, PlacementScreen):
            self.screen.update_view(view)
            return
        players = view.get("players") or []
        you = int(view.get("you") or 0)
        ready = bool(0 <= you < len(players) and players[you]
                     and players[you].get("ready"))
        ships = (view.get("your_board") or {}).get("ships") or []
        if ready and ships:
            self.placed = {
                str(ship["name"]): tuple((int(cell[0]), int(cell[1]))
                                         for cell in ship.get("cells") or [])
                for ship in ships
            }
            self.placing_name = None
            self.placement_ready = True
        else:
            self.placed.clear()
            self.placing_name = None
            self.placing_h = True
            self.placement_ready = False
        self.show_placement()

    def _enter_battle(self, view: dict) -> None:
        if self.screen_name == "battle" and isinstance(self.screen, BattleScreen):
            self.screen.update(view)
            return
        self.show_battle(view)

    def _enter_lobby(self, view: dict) -> None:
        if self.screen_name != "host":
            self.show_host()
        if isinstance(self.screen, HostScreen):
            self.screen.update_view(view)

    # -- battle intents ----------------------------------------------------- #
    def send_place(self, ships: list[dict]) -> None:
        self._send("send_place", ships)

    def send_chat(self, text: str) -> bool:
        text = str(text or "").strip()
        if not text:
            return False
        if self.local or self.net is None:
            return False
        return self._send("send_chat", text[:200])

    def fire_at(self, r: int, c: int) -> bool:
        if self.local:
            return self._fire_local(int(r), int(c))
        view = self.sync or {}
        if view.get("state") != "battle":
            return False
        if view.get("turn") != self.you:
            self.toast(t("err.not_your_turn"), "error")
            return False
        shots = (view.get("enemy") or {}).get("shots") or []
        if any(int(item[0]) == r and int(item[1]) == c for item in shots):
            self.toast(t("err.already_shot"), "error")
            return False
        return self._send("send_fire", int(r), int(c))

    def request_rematch(self) -> None:
        if self.watching:                       # "Next game" in the AI viewer
            self.watch_starter = 1 - self.watch_starter
            self._watch_new_match()
            return
        if self.local:
            self._local_rematch()
            return
        self._send("send_rematch")

    def _send(self, method: str, *args) -> bool:
        net = self.net
        if net is None:
            return False
        try:
            getattr(net, method)(*args)
            return True
        except Exception:  # noqa: BLE001 - never crash the UI on a send
            self.toast(i18n.err_text("connection_lost"), "error")
            return False

    # -- local AI match ----------------------------------------------------- #
    @staticmethod
    def _ai_error_text(exc: Exception) -> str:
        """Friendly message for a BattleshipAI that could not be created."""
        if isinstance(exc, ImportError):
            return t("ai.no_numpy")
        if isinstance(exc, (FileNotFoundError, ValueError)):
            return t("ai.no_model")
        return i18n.err_text("server_error")

    def start_ai(self, difficulty: str) -> None:
        try:
            from ai import BattleshipAI
        except Exception:  # noqa: BLE001 - ai.py missing
            self.toast(i18n.err_text("server_error"), "error")
            return
        self.difficulty = difficulty if difficulty in AI_KINDS else "normal"
        try:
            self.local_ai = BattleshipAI(self.difficulty)
        except Exception as exc:  # noqa: BLE001 - e.g. learned model not trained yet
            self.toast(self._ai_error_text(exc), "error")
            return
        self.local = True
        self.mode = "ai"
        self.local_wins = [0, 0]
        self._ai_starter = 0
        self._new_local_match()

    def _new_local_match(self) -> None:
        self.local_game = engine.Game()
        self.local_game.starter = self._ai_starter
        self._local_counted = False
        self._ai_busy = False
        self.placed.clear()
        self.placing_name = None
        self.placing_h = True
        self.placement_ready = False
        self._last_view = self.local_view()
        self.show_placement()

    def start_local_battle(self, payload: list[dict]) -> None:
        game = self.local_game
        if game is None or self.local_ai is None:
            return
        try:
            game.place_fleet(YOU, payload)
            game.place_fleet(AI_SLOT, self.local_ai.choose_placement())
            game.start_battle(self._ai_starter)
        except engine.BattleshipError as exc:
            self.toast(i18n.err_text(exc.code), "error")
            self.placement_ready = False
            return
        self._local_counted = False
        self._last_view = self.local_view()
        self.show_battle(self._last_view)
        self._maybe_ai_move(600)

    def local_view(self) -> dict | None:
        game = self.local_game
        if game is None:
            return None
        return {
            "t": "sync",
            "state": game.phase,
            "you": YOU,
            "code": "",
            "players": [
                {"name": self.nickname, "ready": game.boards[0].placed,
                 "connected": True},
                {"name": self.ai_label(), "ready": game.boards[1].placed,
                 "connected": True},
            ],
            "wins": list(self.local_wins),
            "starter": game.starter,
            "turn": game.turn,
            "your_board": game.boards[0].to_dict(),
            "enemy": game.boards[1].enemy_view(),
            "stats": {
                "shots": list(game.shots_fired),
                "hits": [len(game.boards[1].hit_cells()),
                         len(game.boards[0].hit_cells())],
            },
            "log": game.log,
            "chat": [],
            "winner": game.winner,
            "forfeit": game.forfeited,
            "rematch": [False, False],
            "notice": None,
        }

    def ai_label(self) -> str:
        return f"{t('ai.label')} · {t('ai.' + self.difficulty)}"

    def current_view(self) -> dict | None:
        if self.watching:
            return self.watch_view()
        if self.local:
            return self.local_view()
        return self.sync

    def _fire_local(self, r: int, c: int) -> bool:
        game = self.local_game
        if game is None or game.phase != "battle":
            return False
        if game.turn != YOU:
            self.toast(t("err.not_your_turn"), "error")
            return False
        if (r, c) in game.boards[1].shots:
            self.toast(t("err.already_shot"), "error")
            return False
        try:
            game.fire(YOU, r, c)
        except engine.ShotError as exc:
            self.toast(i18n.err_text(exc.code), "error")
            return False
        self._local_after_change()
        return True

    def _local_after_change(self, count: bool = True) -> None:
        view = self.local_view()
        if view is None:
            return
        if count and view["state"] == "over" and not self._local_counted:
            self._local_counted = True
            winner = view.get("winner")
            if winner in (0, 1):
                self.local_wins[winner] += 1
            view = self.local_view() or view
        self._last_view = view
        if self.screen_name == "battle" and isinstance(self.screen, BattleScreen):
            self.screen.update(view)
        else:
            self.show_battle(view)
        if view["state"] == "battle" and view.get("turn") == AI_SLOT:
            self._maybe_ai_move(550)

    def _maybe_ai_move(self, delay_ms: int = 550) -> None:
        game = self.local_game
        if (not self.local or self._closing or self._ai_busy or self.local_ai is None
                or game is None or game.phase != "battle" or game.turn != AI_SLOT):
            return
        self._ai_busy = True
        board = game.boards[YOU]

        def run() -> None:
            cell = None
            error = None
            try:
                cell = self.local_ai.choose_attack(board)
            except Exception as exc:  # noqa: BLE001 - fall back to a random shot
                error = exc
            self.post(lambda: self._ai_done(cell, error))

        def kick() -> None:
            threading.Thread(target=run, daemon=True, name="bs-ai").start()

        self.root.after(delay_ms, kick)

    def _ai_done(self, cell, error) -> None:
        self._ai_busy = False
        if self._closing or not self.local:
            return
        game = self.local_game
        if game is None or game.phase != "battle" or game.turn != AI_SLOT:
            return
        if cell is None:
            available = game.boards[YOU].available_cells()
            if not available:
                return
            cell = random.choice(available)
        try:
            game.fire(AI_SLOT, int(cell[0]), int(cell[1]))
        except engine.ShotError:
            available = game.boards[YOU].available_cells()
            if not available:
                return
            r, c = random.choice(available)
            game.fire(AI_SLOT, r, c)
        self._local_after_change()

    def _local_rematch(self) -> None:
        if not self.local:
            return
        self._ai_starter = 1 - self._ai_starter
        self._new_local_match()

    # -- AI-vs-AI viewer (AI vs AI, AI vs bot, bot vs bot) ------------------- #
    def start_watch(self, kind0: str, kind1: str) -> None:
        kinds = [k if k in AI_KINDS else "hard" for k in (kind0, kind1)]
        try:
            from ai import BattleshipAI
            ais = [BattleshipAI(kinds[0]), BattleshipAI(kinds[1])]
        except Exception as exc:  # noqa: BLE001 - e.g. learned model not trained yet
            self.toast(self._ai_error_text(exc), "error")
            return
        self._cancel_watch_job()
        self.watch_kinds = kinds
        self.watch_ais = ais
        self.watching = True
        self.local = False
        self.watch_wins = [0, 0]
        self.watch_starter = 0
        self.watch_paused = False
        self._watch_new_match()

    def watch_names(self) -> list[str]:
        return [f"{t('ai.' + kind)} {'AB'[index]}"
                for index, kind in enumerate(self.watch_kinds)]

    def watch_view(self) -> dict | None:
        game = self.watch_game
        if game is None:
            return None
        names = self.watch_names()
        return {
            "t": "sync",
            "spectate": True,
            "state": game.phase,
            "you": 0,
            "code": "",
            "players": [
                {"name": names[0], "ready": True, "connected": True},
                {"name": names[1], "ready": True, "connected": True},
            ],
            "wins": list(self.watch_wins),
            "starter": game.starter,
            "turn": game.turn,
            "your_board": game.boards[0].to_dict(),
            "enemy": game.boards[1].enemy_view(),
            "enemy_full": game.boards[1].to_dict(),
            "stats": {
                "shots": list(game.shots_fired),
                "hits": [len(game.boards[1].hit_cells()),
                         len(game.boards[0].hit_cells())],
            },
            "log": game.log,
            "chat": [],
            "winner": game.winner,
            "forfeit": game.forfeited,
            "rematch": [False, False],
            "notice": None,
        }

    def _watch_new_match(self) -> None:
        self._cancel_watch_job()
        game = engine.Game()
        try:
            game.place_fleet(0, self.watch_ais[0].choose_placement())
            game.place_fleet(1, self.watch_ais[1].choose_placement())
            game.start_battle(self.watch_starter)
        except engine.BattleshipError as exc:
            self.toast(i18n.err_text(exc.code), "error")
            return
        self.watch_game = game
        self._watch_counted = False
        self._last_view = self.watch_view()
        self.show_battle(self._last_view)
        self._watch_schedule(600)

    def _cancel_watch_job(self) -> None:
        if self._watch_job is not None:
            try:
                self.root.after_cancel(self._watch_job)
            except tk.TclError:
                pass
            self._watch_job = None

    def _stop_watch(self) -> None:
        self._cancel_watch_job()
        self.watching = False
        self.watch_game = None
        self.watch_ais = [None, None]
        self.watch_paused = False

    def _watch_delay(self) -> int:
        return dict(WATCH_SPEEDS).get(self.watch_speed, 450)

    def _watch_schedule(self, delay: int | None = None) -> None:
        if self._closing or not self.watching or self.watch_paused:
            return
        game = self.watch_game
        if game is None or game.phase != "battle":
            return
        self._cancel_watch_job()
        self._watch_job = self.root.after(
            self._watch_delay() if delay is None else delay, self._watch_step)

    def _watch_step(self) -> None:
        self._watch_job = None
        if self._closing or not self.watching or self.watch_paused:
            return
        game = self.watch_game
        if game is None or game.phase != "battle" or game.turn not in (0, 1):
            return
        player = game.turn
        board = game.boards[1 - player]
        try:
            cell = self.watch_ais[player].choose_attack(board)
            game.fire(player, int(cell[0]), int(cell[1]))
        except Exception:  # noqa: BLE001 - never stall the viewer: fire a random cell
            available = board.available_cells()
            if not available:
                return
            r, c = random.choice(available)
            try:
                game.fire(player, r, c)
            except engine.BattleshipError:
                return
        self._watch_after_change()

    def _watch_after_change(self) -> None:
        view = self.watch_view()
        if view is None:
            return
        if view["state"] == "over" and not self._watch_counted:
            self._watch_counted = True
            winner = view.get("winner")
            if winner in (0, 1):
                self.watch_wins[winner] += 1
            view = self.watch_view() or view
        self._last_view = view
        if self.screen_name == "battle" and isinstance(self.screen, BattleScreen):
            self.screen.update(view)
        else:
            self.show_battle(view)
        self._watch_schedule()

    def _refresh_watch_controls(self) -> None:
        if self.screen_name == "battle" and isinstance(self.screen, BattleScreen):
            self.screen.refresh_watch_controls()

    def watch_toggle_pause(self) -> None:
        if not self.watching:
            return
        self.watch_paused = not self.watch_paused
        if self.watch_paused:
            self._cancel_watch_job()
        else:
            self._watch_schedule(60)
        self._refresh_watch_controls()

    def watch_set_speed(self, key: str) -> None:
        if key not in dict(WATCH_SPEEDS):
            return
        self.watch_speed = key
        if self.watching and not self.watch_paused:
            self._watch_schedule()               # apply the new speed right away
        self._refresh_watch_controls()

    # -- battle-log formatting --------------------------------------------- #
    def format_log(self, entry: dict, view: dict) -> str:
        if not isinstance(entry, dict):
            return ""
        kind = entry.get("kind")

        def name(index) -> str:
            players = view.get("players") or []
            try:
                index = int(index)
            except (TypeError, ValueError):
                return t("common.opponent")
            if 0 <= index < len(players) and players[index]:
                return players[index].get("name") or t("common.opponent")
            return t("common.opponent")

        if kind == "start":
            return t("log.start", name=name(entry.get("first")))
        if kind == "shot":
            try:
                cell = engine.cell_name(int(entry.get("r", 0)),
                                        int(entry.get("c", 0)))
            except (TypeError, ValueError):
                cell = "?"
            key = "log.hit" if entry.get("hit") else "log.miss"
            return t(key, name=name(entry.get("by")), cell=cell)
        if kind == "sunk":
            return t("log.sunk", name=name(entry.get("by")),
                     ship=i18n.ship_label(str(entry.get("ship") or "")))
        if kind == "over":
            return t("log.over", name=name(entry.get("winner")))
        if kind == "forfeit":
            return t("log.forfeit", name=name(entry.get("loser")))
        if kind == "join":
            return t("log.join", name=str(entry.get("name") or "?"))
        if kind == "leave":
            return t("log.leave", name=str(entry.get("name") or "?"))
        if kind == "rematch":
            return t("log.rematch")
        if kind == "system":
            return f"{t('chat.system')}: {entry.get('name') or entry.get('code') or ''}"
        return ""

    # -- presentation helpers ----------------------------------------------- #
    def toast(self, text: str, kind: str = "info") -> None:
        if self._closing or not text:
            return
        self._dismiss_toast()
        fg = HIT if kind == "error" else TEXT
        border = HIT if kind == "error" else ACCENT
        try:
            label = tk.Label(self.root, text=text, font=self.f_body, fg=fg,
                             bg=PANEL, bd=0, padx=16, pady=9,
                             highlightthickness=1, highlightbackground=border)
            label.place(relx=0.5, y=18, anchor="n")
            label.lift()
        except tk.TclError:
            return
        self._toast_label = label
        self._toast_job = self.root.after(2800, self._dismiss_toast)

    def _dismiss_toast(self) -> None:
        if self._toast_job is not None:
            try:
                self.root.after_cancel(self._toast_job)
            except tk.TclError:
                pass
            self._toast_job = None
        if self._toast_label is not None:
            try:
                self._toast_label.destroy()
            except tk.TclError:
                pass
            self._toast_label = None

    def copy_code(self) -> None:
        if not self.room_code:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(str(self.room_code))
            self.root.update_idletasks()
        except tk.TclError:
            return
        self.toast(t("common.copied"))

    # -- keyboard shortcuts ------------------------------------------------- #
    def _typing(self) -> bool:
        try:
            widget = self.root.focus_get()
        except (KeyError, tk.TclError):
            return False
        return isinstance(widget, (tk.Entry, tk.Text))

    def _on_rotate_key(self, _event=None):
        if self._typing():
            return None
        if self.screen_name == "placement" and isinstance(self.screen, PlacementScreen):
            self.screen.rotate()
        return "break"

    def _on_escape(self, _event=None):
        if self._closing:
            return "break"
        if self.screen_name == "menu":
            return None
        if self.screen_name in ("host", "join", "ai_setup", "watch_setup"):
            self.leave_to_menu()
            return "break"
        if self.screen_name == "placement":
            if messagebox.askyesno(t("app.title"), t("common.quit_confirm"),
                                   parent=self.root):
                self.leave_to_menu()
            return "break"
        if self.screen_name == "battle":
            over = isinstance(self.screen, BattleScreen) and self.screen.is_over()
            if over or messagebox.askyesno(t("app.title"),
                                           t("common.quit_confirm"),
                                           parent=self.root):
                self.leave_to_menu()
            return "break"
        return None

    # -- teardown ----------------------------------------------------------- #
    def leave_to_menu(self) -> None:
        if self._closing:
            return
        self._close_client()
        self._stop_server()
        self._stop_watch()
        self.local = False
        self.local_game = None
        self.local_ai = None
        self._ai_busy = False
        self.sync = None
        self._last_view = None
        self._last_notice = None
        self.you = None
        self.placed.clear()
        self.placing_name = None
        self.placement_ready = False
        self.show_menu()

    def _close_client(self) -> None:
        net = self.net
        self.net = None
        if net is None:
            return
        try:
            net.leave()
        except Exception:  # noqa: BLE001
            pass
        try:
            net.close()
        except Exception:  # noqa: BLE001
            pass

    def _stop_server(self) -> None:
        server = self.server
        self.server = None
        if server is None:
            return
        try:
            server.stop()
        except Exception:  # noqa: BLE001
            pass

    def close(self, destroy_root: bool = True) -> None:
        """Stop everything owned by the app.

        With ``destroy_root=False`` the Tk root and its widgets are left alive
        (only the poll job, toast, socket and server are stopped). Tests use
        that to share one root across cases: destroying and re-creating Tk
        roots on macOS after a battle screen has been shown can wedge Tk's
        event loop. Production callers keep the default.
        """
        if self._closing:
            return
        self._closing = True
        if self._poll_job is not None:
            try:
                self.root.after_cancel(self._poll_job)
            except tk.TclError:
                pass
            self._poll_job = None
        self._dismiss_toast()
        self._close_client()
        self._stop_server()
        self._stop_watch()
        self.local = False
        if not destroy_root:
            return
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    # -- main loop ---------------------------------------------------------- #
    def run(self) -> None:
        self.root.mainloop()


# --------------------------------------------------------------------------- #
# Module entry point used by battleship.py
# --------------------------------------------------------------------------- #

def launch(mode="menu", code=None, ip=None, port=None, lang=None,
           ai_difficulty="normal") -> None:
    """Create the Tk root, build the app and enter the main loop."""
    app = BattleshipApp(mode=mode, code=code, ip=ip, port=port, lang=lang,
                        ai_difficulty=ai_difficulty)
    app.run()


if __name__ == "__main__":  # pragma: no cover - manual smoke run
    launch()
