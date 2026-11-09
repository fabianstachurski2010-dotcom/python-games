#!/usr/bin/env python3
"""Pygame front end for Connect Four: menu, join, game, chat, replays.

The whole UI is drawn onto a fixed ``DESIGN`` canvas that is scaled to fit the
window, so layout math can use absolute coordinates and every screen looks the
same on a MacBook and an external monitor.  All state that decides what a
player may do lives on the server; this module only sends intents and renders
the newest ``sync`` snapshot.
"""

from __future__ import annotations

import math
import os
import queue
import socket
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pygame

import engine
import i18n
import net_client
import records
import settings
import theme
from theme import (ACCENT, CARD, CARD_DEEP, CARD_HI, DIM, ERR, INK, INK_SOFT, MUTED, OK, RED, STROKE,
                   STROKE_SOFT, WARN, YELLOW)

DESIGN = (1160, 740)
FPS = 60
VERSION = "1.0.0"


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #

def _card_shadow(surf: pygame.Surface, rect: pygame.Rect, radius: int = 18, spread: int = 10) -> None:
    shade = pygame.Surface((rect.w + spread * 2, rect.h + spread * 2), pygame.SRCALPHA)
    pygame.draw.rect(
        shade, (0, 0, 0, 70), shade.get_rect().inflate(-spread, -spread), border_radius=radius + 4
    )
    surf.blit(shade, (rect.x - spread, rect.y - spread + 5))


def _truncate(content: str, size: int, width: int, bold: bool = False) -> str:
    f = theme.font(size, bold)
    if f.size(content)[0] <= width:
        return content
    out = content
    while out and f.size(out + "…")[0] > width:
        out = out[:-1]
    return out + "…" if out else ""


class BoardView:
    """Draws the 7x6 board and translates mouse positions to columns."""

    def __init__(self, origin: Tuple[int, int] = (68, 160), cell: int = 84, radius: int = 33, pad: int = 22):
        self.origin = origin
        self.cell = cell
        self.radius = radius
        self.pad = pad
        self.rect = pygame.Rect(
            origin[0] - pad, origin[1] - pad, engine.COLS * cell + pad * 2, engine.ROWS * cell + pad * 2
        )

    def center(self, row: int, col: int) -> Tuple[int, int]:
        return (
            self.origin[0] + col * self.cell + self.cell // 2,
            self.origin[1] + row * self.cell + self.cell // 2,
        )

    def col_at(self, pos: Tuple[int, int]) -> Optional[int]:
        if not self.rect.collidepoint(pos):
            return None
        col = (pos[0] - self.origin[0]) // self.cell
        return int(col) if 0 <= col < engine.COLS else None

    def draw(
        self,
        surf: pygame.Surface,
        cells: List[List[int]],
        hover_col: Optional[int] = None,
        ghost_row: Optional[int] = None,
        ghost_seat: Optional[int] = None,
        win_line: Optional[List[List[int]]] = None,
        anim: Optional[dict] = None,
        pulse: float = 0.0,
    ) -> None:
        rect = self.rect
        _card_shadow(surf, rect, radius=22, spread=8)
        pygame.draw.rect(surf, theme.BOARD_EDGE, rect, border_radius=22)
        inner = rect.inflate(-4, -4)
        pygame.draw.rect(surf, theme.BOARD, inner, border_radius=19)
        # subtle top light so the board reads as a plastic tray
        glow = pygame.Surface(inner.size, pygame.SRCALPHA)
        pygame.draw.rect(glow, (255, 255, 255, 14), glow.get_rect(), border_radius=19)
        surf.blit(glow, inner)

        if hover_col is not None:
            col_rect = pygame.Rect(
                self.origin[0] + hover_col * self.cell + 4,
                self.origin[1] - self.cell // 2,
                self.cell - 8,
                rect.bottom - self.origin[1] + self.cell // 2 - 8,
            )
            tint = pygame.Surface(col_rect.size, pygame.SRCALPHA)
            pygame.draw.rect(tint, (140, 190, 255, 26), tint.get_rect(), border_radius=14)
            surf.blit(tint, col_rect)

        for row in range(engine.ROWS):
            for col in range(engine.COLS):
                cx, cy = self.center(row, col)
                surf.blit(theme.hole_surface(self.radius), theme.hole_surface(self.radius).get_rect(center=(cx, cy)))

        skip = (anim["row"], anim["col"]) if anim else None
        win_set = {(int(r), int(c)) for r, c in (win_line or [])}
        for row in range(engine.ROWS):
            for col in range(engine.COLS):
                seat = cells[row][col]
                if seat == engine.EMPTY or (row, col) == skip:
                    continue
                cx, cy = self.center(row, col)
                theme.blit_center(surf, theme.disc_surface(self.radius, seat), (cx, cy))
                if (row, col) in win_set:
                    alpha = 90 + int(70 * (0.5 + 0.5 * math.sin(pulse * 5.0)))
                    ring = pygame.Surface((self.radius * 2 + 18, self.radius * 2 + 18), pygame.SRCALPHA)
                    pygame.draw.circle(
                        ring,
                        theme.with_alpha(theme.WHITE, alpha),
                        (ring.get_width() // 2, ring.get_height() // 2),
                        self.radius + 5,
                        3,
                    )
                    surf.blit(ring, ring.get_rect(center=(cx, cy)))

        if ghost_seat and ghost_row is not None and hover_col is not None:
            cx, cy = self.center(ghost_row, hover_col)
            color = theme.SEAT_COLORS.get(ghost_seat, MUTED)
            ghost = pygame.Surface((self.radius * 2 + 4, self.radius * 2 + 4), pygame.SRCALPHA)
            pygame.draw.circle(ghost, color + (58,), (ghost.get_width() // 2, ghost.get_height() // 2), self.radius)
            pygame.draw.circle(
                ghost, color + (190,), (ghost.get_width() // 2, ghost.get_height() // 2), self.radius, 3
            )
            surf.blit(ghost, ghost.get_rect(center=(cx, cy)))

        if anim:
            t = max(0.0, min(1.0, anim.get("t", 1.0)))
            ease = 1.0 - (1.0 - t) ** 3
            start_y = self.origin[1] - self.cell
            end_y = self.center(anim["row"], anim["col"])[1]
            cx = self.center(anim["row"], anim["col"])[0]
            cy = int(start_y + (end_y - start_y) * ease)
            prev_clip = surf.get_clip()
            surf.set_clip(inner.inflate(2, 2))
            theme.blit_center(surf, theme.disc_surface(self.radius, anim["seat"]), (cx, cy))
            surf.set_clip(prev_clip)


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

def default_name() -> str:
    saved = (settings.load().get("name") or "").strip()
    if saved:
        return saved[:16]
    try:
        import getpass

        user = getpass.getuser().strip()
    except Exception:  # pragma: no cover - no passwd entry
        user = ""
    if user:
        return user.replace("_", " ").title()[:16]
    try:
        host = socket.gethostname().split(".")[0]
    except OSError:  # pragma: no cover
        host = ""
    return (host or i18n.t("join.default_name"))[:16]


class App:
    def __init__(self, lang: Optional[str] = None, headless: bool = False) -> None:
        pygame.init()
        theme.reset_caches()
        pygame.key.set_repeat(0)
        i18n.init_from_settings()
        if lang:
            i18n.set_lang(lang)
        self.headless = headless
        self.name = default_name()
        self.local_ip = net_client.local_ip()
        self.mouse: Tuple[int, int] = (-100, -100)
        self.running = True
        self.toast = theme.Toast()
        self._scale = 1.0
        self._offset = (0, 0)
        self.canvas = pygame.Surface(DESIGN)

        flags = pygame.RESIZABLE
        size = DESIGN
        if not headless:
            info = pygame.display.Info()
            usable = (int(info.current_w * 0.94), int(info.current_h * 0.90))
            self._scale = min(1.0, usable[0] / DESIGN[0], usable[1] / DESIGN[1])
            size = (int(DESIGN[0] * self._scale), int(DESIGN[1] * self._scale))
        self.window = pygame.display.set_mode(size, flags)
        pygame.display.set_caption(f"{i18n.t('app.title')} · Connect Four")
        self._recompute_view()
        try:
            pygame.key.start_text_input()
        except (AttributeError, pygame.error):  # pragma: no cover
            pass

        self.screens: Dict[str, object] = {
            "menu": MenuScreen(self),
            "join": JoinScreen(self),
            "game": GameScreen(self),
            "replays": ReplayScreen(self),
            "watch": WatchScreen(self),
        }
        self.current = "menu"
        self.screens["menu"].enter()  # type: ignore[attr-defined]
        self.clock = pygame.time.Clock()

    # -- window scaling ---------------------------------------------------- #

    def _recompute_view(self) -> None:
        w, h = self.window.get_size()
        self._scale = min(w / DESIGN[0], h / DESIGN[1])
        self._offset = (
            int((w - DESIGN[0] * self._scale) / 2),
            int((h - DESIGN[1] * self._scale) / 2),
        )

    def _to_design(self, pos: Tuple[int, int]) -> Tuple[int, int]:
        if self._scale <= 0:
            return pos
        return (
            int((pos[0] - self._offset[0]) / self._scale),
            int((pos[1] - self._offset[1]) / self._scale),
        )

    # -- navigation -------------------------------------------------------- #

    def goto(self, name: str, **kwargs) -> None:
        if not self.running:
            return
        old = self.screens[self.current]
        if self.current != name and hasattr(old, "leave"):
            old.leave()  # type: ignore[attr-defined]
        self.current = name
        target = self.screens[name]
        if hasattr(target, "enter"):
            target.enter(**kwargs)  # type: ignore[attr-defined]
        if not self.headless:
            pygame.display.set_caption(f"{i18n.t('app.title')} · {i18n.t('app.subtitle')}")

    def toggle_lang(self) -> None:
        i18n.toggle_lang()
        settings.save(lang=i18n.get_lang())
        for screen in self.screens.values():
            if hasattr(screen, "relabel"):
                screen.relabel()  # type: ignore[attr-defined]

    def copy_text(self, text: str) -> None:
        try:
            pygame.scrap.init()
            pygame.scrap.put(pygame.SCRAP_TEXT, text.encode("utf-8"))
            self.toast.show(i18n.t("common.copied"), OK)
        except Exception:
            self.toast.show(text, INK_SOFT, 4.0)

    def quit(self) -> None:
        self.running = False

    # -- main loop --------------------------------------------------------- #

    def handle_event(self, event: pygame.event.Event) -> None:
        if event.type == pygame.QUIT:
            self.quit()
            return
        if event.type == pygame.VIDEORESIZE:
            self.window = pygame.display.set_mode((max(720, event.w), max(460, event.h)), pygame.RESIZABLE)
            self._recompute_view()
            return
        if event.type in (pygame.MOUSEMOTION, pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP):
            pos = self._to_design(event.pos)
            self.mouse = pos
            event = pygame.event.Event(event.type, {**event.dict, "pos": pos})
        screen = self.screens[self.current]
        if hasattr(screen, "handle"):
            screen.handle(event)  # type: ignore[attr-defined]

    def run(self) -> None:
        while self.running:
            dt = self.clock.tick(FPS) / 1000.0
            dt = min(dt, 0.1)
            for event in pygame.event.get():
                self.handle_event(event)
            if not self.running:
                break
            screen = self.screens[self.current]
            screen.update(dt, self.mouse)  # type: ignore[attr-defined]
            screen.draw(self.canvas)  # type: ignore[attr-defined]
            self.toast.draw(self.canvas, time.monotonic())
            self._present()
        self.shutdown()

    def _present(self) -> None:
        if self._scale == 1.0 and self._offset == (0, 0):
            self.window.blit(self.canvas, (0, 0))
        else:
            self.window.fill((0, 0, 0))
            target = (int(DESIGN[0] * self._scale), int(DESIGN[1] * self._scale))
            self.window.blit(pygame.transform.smoothscale(self.canvas, target), self._offset)
        pygame.display.flip()

    def shot(self, path: Path) -> None:
        self.canvas.fill((0, 0, 0))
        self.screens[self.current].draw(self.canvas)  # type: ignore[attr-defined]
        self.toast.draw(self.canvas, time.monotonic())
        path.parent.mkdir(parents=True, exist_ok=True)
        pygame.image.save(self.canvas, str(path))

    def shutdown(self) -> None:
        for screen in self.screens.values():
            if hasattr(screen, "leave"):
                try:
                    screen.leave()  # type: ignore[attr-defined]
                except Exception:
                    pass
        pygame.quit()


class Screen:
    def __init__(self, app: App) -> None:
        self.app = app
        self.buttons: List[theme.Button] = []

    # -- hooks ------------------------------------------------------------- #

    def enter(self, **kwargs) -> None:  # pragma: no cover - overridden
        pass

    def leave(self) -> None:  # pragma: no cover - overridden
        pass

    def relabel(self) -> None:  # pragma: no cover - overridden
        pass

    def update(self, dt: float, mouse: Tuple[int, int]) -> None:
        for button in self.buttons:
            button.update(mouse)

    def draw(self, surf: pygame.Surface) -> None:  # pragma: no cover - overridden
        pass

    def draw_buttons(self, surf: pygame.Surface) -> None:
        for button in self.buttons:
            button.draw(surf)

    def handle(self, event: pygame.event.Event) -> bool:
        for button in self.buttons:
            if button.handle(event):
                return True
        return False

    def lang_button(self) -> theme.Button:
        return theme.Button(
            (1036, 26, 92, 34),
            "English" if i18n.get_lang() == "zh" else "中文",
            lambda _b: self.app.toggle_lang(),
            kind="chip",
            size=14,
        )


# --------------------------------------------------------------------------- #
# Menu
# --------------------------------------------------------------------------- #

class MenuScreen(Screen):
    def __init__(self, app: App) -> None:
        super().__init__(app)
        self.rooms: List[dict] = []
        self.scanning = False
        self.scanned_at = 0.0
        self.error = ""
        self.t = 0.0
        self.room_hover = -1

    def enter(self, skip_scan: bool = False, **kwargs) -> None:
        self.build()
        if not skip_scan:
            self.start_scan()

    def build(self) -> None:
        self.buttons = [
            self.lang_button(),
            theme.Button(
                (952, 132, 108, 26),
                i18n.t("menu.rescan"),
                lambda _b: self.start_scan(),
                kind="chip",
                size=12,
            ),
            theme.Button(
                (76, 660, 200, 46),
                i18n.t("menu.quit"),
                lambda _b: self.app.quit(),
                kind="ghost",
                size=17,
            ),
            theme.Button(
                (1036, 664, 92, 38),
                f"v{VERSION}",
                None,
                kind="ghost",
                size=13,
                enabled=False,
            ),
        ]
        # action cards are drawn by hand; these invisible-ish buttons capture clicks
        self.card_buttons = [
            theme.Button((76, 316, 520, 76), "", lambda _b: self.host()),
            theme.Button((76, 400, 520, 76), "", lambda _b: self.app.goto("join")),
            theme.Button((76, 484, 520, 76), "", lambda _b: self.app.goto("watch", mode="me_ai")),
            theme.Button((76, 568, 520, 76), "", lambda _b: self.app.goto("replays")),
        ]

    def relabel(self) -> None:
        self.build()

    def host(self) -> None:
        self.app.goto("game", mode="host")

    # -- discovery --------------------------------------------------------- #

    def start_scan(self) -> None:
        if self.scanning:
            return
        self.scanning = True
        threading.Thread(target=self._scan, daemon=True, name="c4-discover").start()

    def _scan(self) -> None:
        try:
            self.rooms = net_client.discover_rooms(1.3)
        except Exception:
            self.rooms = []
        finally:
            self.scanning = False
            self.scanned_at = time.monotonic()

    # -- update / draw ----------------------------------------------------- #

    def update(self, dt: float, mouse: Tuple[int, int]) -> None:
        self.t += dt
        super().update(dt, mouse)
        if not self.scanning and time.monotonic() - self.scanned_at > 7.0:
            self.start_scan()
        self.room_hover = -1
        for index, rect in enumerate(self.room_rects()):
            if rect.collidepoint(mouse):
                self.room_hover = index

    def room_rects(self) -> List[pygame.Rect]:
        rects = []
        y = 190
        for _ in self.rooms[:5]:
            rects.append(pygame.Rect(676, y, 400, 62))
            y += 70
        return rects

    def handle(self, event: pygame.event.Event) -> bool:
        if event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            for index, rect in enumerate(self.room_rects()):
                if rect.collidepoint(event.pos):
                    room = self.rooms[index]
                    self.app.goto("join", host=room.get("ip", ""), port=room.get("port", 0))
                    return True
        if super().handle(event):
            return True
        if event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            for button in self.card_buttons:
                if button.handle(event):
                    return True
        return False

    def draw(self, surf: pygame.Surface) -> None:
        surf.blit(theme.background(DESIGN), (0, 0))
        # decorative header discs
        for index in range(4):
            seat = engine.RED if index % 2 == 0 else engine.YELLOW
            theme.blit_center(
                surf, theme.disc_surface(13, seat), (100 + index * 34, 104)
            )
        theme.draw_text(surf, i18n.t("app.title"), 62, INK, (76, 132), bold=True)
        theme.draw_text(surf, "Connect Four · " + i18n.t("app.subtitle"), 19, MUTED, (80, 206))
        for index, line in enumerate(theme.wrap_text(i18n.t("app.tagline"), 16, 470)):
            theme.draw_text(surf, line, 16, INK_SOFT, (80, 244 + index * 24))

        cards = [
            ("menu.host", "menu.host_hint", True),
            ("menu.join", "menu.join_hint", False),
            ("menu.watch", "menu.watch_hint", False),
            ("menu.replays", "menu.replays_hint", False),
        ]
        for index, (button, (title_key, hint_key, _)) in enumerate(zip(self.card_buttons, cards)):
            rect = pygame.Rect(76, 316 + index * 84, 520, 76)
            hover = rect.collidepoint(self.app.mouse)
            color = CARD_HI if hover else CARD
            border = theme.blend(STROKE, ACCENT, 0.55) if hover else STROKE_SOFT
            theme.panel(surf, rect, color, radius=16, border=border)
            # accent stripe
            stripe = pygame.Rect(rect.x, rect.y + 12, 4, rect.h - 24)
            pygame.draw.rect(surf, ACCENT if hover else theme.blend(ACCENT, CARD, 0.55), stripe, border_radius=2)
            theme.draw_text(surf, i18n.t(title_key), 21, INK, (rect.x + 26, rect.y + 13), bold=True)
            theme.draw_text(surf, i18n.t(hint_key), 14, MUTED, (rect.x + 26, rect.y + 46))
            theme.draw_text(surf, "›", 30, ACCENT if hover else MUTED, (rect.right - 34, rect.centery - 2), anchor="center")

        self._draw_rooms(surf)
        self.draw_buttons(surf)
        theme.draw_text(
            surf, "pygame · LAN · " + i18n.t("app.subtitle"), 12, DIM, (300, 683)
        )
        if self.error:
            theme.draw_text(surf, self.error, 14, ERR, (300, 668))

    def _draw_rooms(self, surf: pygame.Surface) -> None:
        panel = pygame.Rect(660, 118, 432, 452)
        theme.panel(surf, panel, CARD, radius=18, border=STROKE_SOFT)
        theme.draw_text(surf, i18n.t("menu.rooms"), 17, INK, (panel.x + 24, panel.y + 20), bold=True)
        status = i18n.t("menu.scanning") if self.scanning else ""
        status_rect = theme.draw_text(surf, status, 12, DIM, (panel.x + 24, panel.y + 46))
        if self.scanning:
            theme.spinner(surf, (status_rect.right + 14, status_rect.centery), 8, self.t, ACCENT)
        else:
            pygame.draw.circle(surf, OK, (panel.x + 24 + 4, panel.y + 24), 4)
        theme.hline(surf, panel.x + 20, panel.right - 20, panel.y + 72)
        if not self.rooms:
            lines = theme.wrap_text(i18n.t("menu.no_rooms"), 14, panel.w - 60)
            for index, line in enumerate(lines):
                theme.draw_text(surf, line, 14, MUTED, (panel.x + 24, panel.y + 110 + index * 22))
            return
        for index, room in enumerate(self.rooms[:5]):
            rect = self.room_rects()[index]
            hover = index == self.room_hover
            pygame.draw.rect(surf, CARD_HI if hover else CARD_DEEP, rect, border_radius=12)
            if hover:
                pygame.draw.rect(surf, theme.blend(STROKE, ACCENT, 0.7), rect, width=1, border_radius=12)
            theme.blit_center(surf, theme.disc_surface(11, engine.RED), (rect.x + 24, rect.centery - 8))
            theme.blit_center(surf, theme.disc_surface(11, engine.YELLOW), (rect.x + 48, rect.centery - 8))
            host = _truncate(str(room.get("host") or "?"), 15, 210, bold=True)
            theme.draw_text(surf, host, 15, INK, (rect.x + 74, rect.y + 12), bold=True)
            detail = f"{room.get('ip')}:{room.get('port')}"
            theme.draw_text(surf, detail, 12, MUTED, (rect.x + 74, rect.y + 34))
            badge_rect = pygame.Rect(rect.right - 74, rect.y + 18, 56, 24)
            label = f"{room.get('players', 0)}/2"
            theme.badge(surf, badge_rect, label, OK if room.get("players", 0) < 2 else WARN, size=12)


# --------------------------------------------------------------------------- #
# Join
# --------------------------------------------------------------------------- #

class JoinScreen(Screen):
    def __init__(self, app: App) -> None:
        super().__init__(app)
        self.role = "player"
        self.busy = False
        self.error = ""
        self.t = 0.0
        self._result: "queue.Queue[tuple]" = queue.Queue()
        self.host_field = theme.TextField((380, 220, 400, 46), "", i18n.t("join.host_placeholder"), max_len=64)
        self.port_field = theme.TextField((380, 292, 400, 46), str(net_client.DISCOVERY_PORT), "", max_len=5, numeric=True)
        self.name_field = theme.TextField((380, 364, 400, 46), app.name, i18n.t("join.name_placeholder"), max_len=16)

    def enter(self, host: str = "", port: int = 0, **kwargs) -> None:
        if host:
            self.host_field.text = host
        if port:
            self.port_field.text = str(port)
        self.name_field.text = self.name_field.text or self.app.name
        self.error = ""
        self.busy = False
        self.build()

    def build(self) -> None:
        self.buttons = [
            self.lang_button(),
            theme.Button((380, 508, 400, 52), i18n.t("join.connect"), lambda _b: self.connect(), kind="primary", size=19),
            theme.Button((380, 574, 190, 44), i18n.t("common.back"), lambda _b: self.app.goto("menu"), kind="ghost", size=16),
            theme.Button((590, 436, 90, 36), i18n.t("join.role_player"), lambda _b: self.set_role("player"), kind="chip", size=14),
            theme.Button((688, 436, 92, 36), i18n.t("join.role_spectator"), lambda _b: self.set_role("spec"), kind="chip", size=14),
        ]
        self.refresh_role_buttons()

    def relabel(self) -> None:
        self.host_field.placeholder = i18n.t("join.host_placeholder")
        self.name_field.placeholder = i18n.t("join.name_placeholder")
        self.build()

    def set_role(self, role: str) -> None:
        self.role = role
        self.refresh_role_buttons()

    def refresh_role_buttons(self) -> None:
        for button in self.buttons:
            if button.data == "role":
                pass
        # role buttons are index 3 and 4
        for index, role in ((3, "player"), (4, "spec")):
            if index < len(self.buttons):
                button = self.buttons[index]
                active = self.role == role
                button.kind = "primary" if active else "chip"

    def connect(self) -> None:
        if self.busy:
            return
        host = self.host_field.text.strip() or "127.0.0.1"
        try:
            port = int(self.port_field.text or net_client.DISCOVERY_PORT)
        except ValueError:
            self.error = i18n.t("err.bad_message")
            return
        name = (self.name_field.text.strip() or self.app.name)[:16]
        self.app.name = name
        settings.save(name=name)
        self.error = ""
        self.busy = True
        threading.Thread(
            target=self._connect_worker, args=(host, port, name, self.role), daemon=True, name="c4-connect"
        ).start()

    def _connect_worker(self, host: str, port: int, name: str, role: str) -> None:
        try:
            client = net_client.NetClient(host, port, name, role)
            client.start()
            self._result.put(("ok", client))
        except OSError as exc:
            self._result.put(("err", f"{host}:{port} ({exc.__class__.__name__})"))

    def update(self, dt: float, mouse: Tuple[int, int]) -> None:
        self.t += dt
        super().update(dt, mouse)
        for field in (self.host_field, self.port_field, self.name_field):
            field.update(mouse, dt)
        try:
            kind, payload = self._result.get_nowait()
        except queue.Empty:
            return
        self.busy = False
        if kind == "ok":
            self.app.goto("game", client=payload, mode="guest")
        else:
            self.error = i18n.t("join.failed", where=payload)

    def handle(self, event: pygame.event.Event) -> bool:
        for field in (self.host_field, self.port_field, self.name_field):
            result = field.handle(event)
            if result == "submit":
                self.connect()
                return True
            if result == "cancel":
                self.app.goto("menu")
                return True
        if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            self.app.goto("menu")
            return True
        return super().handle(event)

    def draw(self, surf: pygame.Surface) -> None:
        surf.blit(theme.background(DESIGN), (0, 0))
        panel = pygame.Rect(340, 118, 480, 520)
        _card_shadow(surf, panel)
        theme.panel(surf, panel, CARD, radius=20, border=STROKE_SOFT)
        theme.draw_text(surf, i18n.t("join.title"), 28, INK, (380, 146), bold=True)

        theme.draw_text(surf, i18n.t("join.host"), 13, MUTED, (380, 198))
        self.host_field.draw(surf)
        theme.draw_text(surf, i18n.t("join.port"), 13, MUTED, (380, 270))
        self.port_field.draw(surf)
        theme.draw_text(surf, i18n.t("join.name"), 13, MUTED, (380, 342))
        self.name_field.draw(surf)
        theme.draw_text(surf, i18n.t("join.role"), 13, MUTED, (380, 444))
        self.draw_buttons(surf)

        if self.busy:
            theme.spinner(surf, (700, 534), 10, self.t, INK)
        if self.error:
            for index, line in enumerate(theme.wrap_text(self.error, 13, 400)):
                theme.draw_text(surf, line, 13, ERR, (380, 620 + index * 18))
        theme.draw_text(
            surf,
            "TCP · " + i18n.t("join.role_player") + " / " + i18n.t("join.role_spectator"),
            12,
            DIM,
            (340, 656),
        )


# --------------------------------------------------------------------------- #
# Game
# --------------------------------------------------------------------------- #

class GameScreen(Screen):
    """Host lobby, live match, spectator view, chat and the result overlay."""

    def __init__(self, app: App) -> None:
        super().__init__(app)
        self.mode = "guest"
        self.server = None
        self.client: Optional[net_client.NetClient] = None
        self.snap: Optional[dict] = None
        self.welcome: Optional[dict] = None
        self.room = "????"
        self.port = 0
        self.t = 0.0
        self.anim: Optional[dict] = None
        self.pulse = 0.0
        self.hover_col: Optional[int] = None
        self.chat_scroll = 0
        self.chat_field = theme.TextField((724, 622, 300, 44), "", "", max_len=200, size=15)
        self.ai_level = settings.load().get("ai_level", "normal")
        self.confirm_leave = False
        self.disconnected = False
        self._chat_sig: Tuple = ()
        self._saved_marker: Optional[tuple] = None
        self._last_phase: Optional[str] = None
        self._hint_t = 0.0
        self.board = BoardView()
        self._pending_ai: Optional[str] = None

    # -- lifecycle --------------------------------------------------------- #

    def enter(
        self,
        mode: str = "guest",
        client: Optional[net_client.NetClient] = None,
        ai_level: Optional[str] = None,
        port: int = 0,
        **kwargs,
    ) -> None:
        self.mode = mode
        self.snap = None
        self.welcome = None
        self.anim = None
        self.chat_scroll = 0
        self.confirm_leave = False
        self.disconnected = False
        self._chat_sig = ()
        self._saved_marker = None
        self._last_phase = None
        self.error_text = ""
        self.server = None
        self._pending_ai = ai_level
        if mode == "host":
            self._start_host(port)
        elif client is not None:
            self.client = client
            self.room = "????"
        self.chat_field.text = ""
        self.chat_field.focus(False)
        self.build()

    def _start_host(self, port: int = 0) -> None:
        import net_server

        try:
            server = net_server.GameServer(
                tcp_port=port,
                discovery=True,
                host_name=self.app.name,
                records_dir=records.records_dir(),
                log=lambda *_a, **_k: None,
            )
            server.start()
        except OSError as exc:
            self.app.toast.show(f"{i18n.t('err.connection')}: {exc}", ERR)
            self.app.goto("menu")
            return
        self.server = server
        self.room = server.room_code
        self.port = server.port
        try:
            client = net_client.NetClient("127.0.0.1", server.port, self.app.name, "player")
            client.start()
        except OSError as exc:
            server.stop()
            self.app.toast.show(f"{i18n.t('err.connection')}: {exc}", ERR)
            self.app.goto("menu")
            return
        self.client = client

    def leave(self) -> None:
        if self.client is not None:
            try:
                self.client.send({"t": "leave"})
            except Exception:
                pass
            self.client.close()
            self.client = None
        if self.server is not None:
            try:
                self.server.stop()
            except Exception:
                pass
            self.server = None

    # -- widgets ----------------------------------------------------------- #

    def build(self) -> None:
        self.chat_field.placeholder = i18n.t("chat.placeholder")
        self.buttons = [
            theme.Button(
                (926, 24, 92, 34),
                "English" if i18n.get_lang() == "zh" else "中文",
                lambda _b: self.app.toggle_lang(),
                kind="chip",
                size=13,
            ),
            theme.Button((1028, 24, 92, 34), i18n.t("game.leave"), lambda _b: self.ask_leave(), kind="ghost", size=13),
            theme.Button((1024, 626, 76, 44), i18n.t("chat.send"), lambda _b: self.send_chat(), kind="primary", size=14),
        ]
        # overlay-only buttons are rebuilt on demand in _overlay_buttons()

    def relabel(self) -> None:
        self.chat_field.placeholder = i18n.t("chat.placeholder")
        self.build()

    def relabel_keep_widgets(self) -> None:
        self.relabel()

    def ask_leave(self) -> None:
        self.confirm_leave = True

    # -- networking -------------------------------------------------------- #

    def _poll(self) -> None:
        client = self.client
        if client is None:
            return
        for msg in client.poll():
            kind = msg.get("t")
            if kind == "sync":
                self._on_sync(msg)
            elif kind == "welcome":
                self.welcome = msg
                self.room = str(msg.get("room") or self.room)
                if msg.get("why") == "room_full":
                    self.app.toast.show(i18n.t("err.room_full"), WARN)
                if self._pending_ai and msg.get("seat") == engine.RED:
                    level = self._pending_ai
                    self._pending_ai = None
                    client.send({"t": "ai", "level": level})
            elif kind == "error":
                self.app.toast.show(i18n.error_text(str(msg.get("code")), str(msg.get("msg"))), ERR)
            elif kind == "_disconnected":
                self._on_disconnect()

    def _on_disconnect(self) -> None:
        if self.disconnected:
            return
        self.disconnected = True
        self.app.toast.show(i18n.t("game.disconnected"), ERR, 4.0)

    def _on_sync(self, snap: dict) -> None:
        previous = self.snap
        self.snap = snap
        self.room = str(snap.get("room") or self.room)
        if snap.get("phase") == "playing" and previous and previous.get("phase") == "playing":
            if snap.get("moves", 0) == previous.get("moves", 0) + 1 and snap.get("last"):
                last = snap["last"]
                self.anim = {
                    "col": int(last["col"]),
                    "row": int(last["row"]),
                    "seat": int(last["seat"]),
                    "t": 0.0,
                }
        phase = snap.get("phase")
        if phase != self._last_phase:
            if phase == "playing" and self._last_phase == "lobby":
                self.anim = None
                self.chat_scroll = 0
            if phase == "over":
                self._save_record(snap)
            self._last_phase = phase
        messages = snap.get("chat") or []
        signature = (len(messages), messages[-1].get("ts") if messages else 0)
        if signature != self._chat_sig:
            self._chat_sig = signature
            if self.chat_scroll <= 0:
                self.chat_scroll = 0

    def _save_record(self, snap: dict) -> None:
        record = snap.get("record")
        if not isinstance(record, dict):
            return
        marker = (record.get("started"), record.get("ended"), len(record.get("columns") or []))
        if self._saved_marker == marker:
            return
        self._saved_marker = marker
        path = records.save(record)
        if path is not None:
            self.app.toast.show(i18n.t("records.title") + " ✓", OK, 2.0)

    def send_chat(self) -> None:
        text = self.chat_field.text.strip()
        if not text or self.client is None:
            return
        self.client.send({"t": "chat", "text": text})
        self.chat_field.text = ""
        self.chat_scroll = 0

    def send_move(self, col: int) -> None:
        if self.client is None:
            return
        self.client.send({"t": "move", "col": int(col)})

    # -- derived state ----------------------------------------------------- #

    @property
    def me_seat(self) -> int:
        you = (self.snap or {}).get("you") or {}
        return int(you.get("seat") or 0)

    @property
    def is_spectator(self) -> bool:
        you = (self.snap or {}).get("you") or {}
        return str(you.get("role")) == "spec"

    def seat_info(self, seat: int) -> Optional[dict]:
        seats = (self.snap or {}).get("seats") or {}
        return seats.get(str(seat))

    def seat_name(self, seat: int) -> str:
        info = self.seat_info(seat)
        if not info:
            return "—"
        if info.get("ai"):
            return f"AI · {i18n.level_label(str(info.get('level') or 'normal'))}"
        return str(info.get("name") or "—")

    def score(self, seat: int) -> int:
        scores = (self.snap or {}).get("scores") or {}
        return int(scores.get(str(seat)) or 0)

    def hint_text(self, snap: dict) -> Tuple[str, Tuple[int, int, int]]:
        phase = snap.get("phase")
        if phase == "lobby":
            return i18n.t("game.waiting_player"), MUTED
        if phase == "over":
            if snap.get("draw"):
                return i18n.t("game.draw"), WARN
            winner = snap.get("winner")
            if not winner:
                return i18n.t("game.draw"), WARN
            return i18n.t("game.wins", name=self.seat_name(int(winner))), theme.SEAT_COLORS[int(winner)]
        turn = int(snap.get("turn") or engine.RED)
        if self.is_spectator:
            return i18n.t("game.turn", name=self.seat_name(turn)), INK_SOFT
        if turn == self.me_seat:
            return i18n.t("game.your_turn"), ACCENT
        if snap.get("thinking"):
            return i18n.t("game.thinking", name=self.seat_name(turn)), INK_SOFT
        return i18n.t("game.turn", name=self.seat_name(turn)), INK_SOFT

    # -- update ------------------------------------------------------------ #

    def update(self, dt: float, mouse: Tuple[int, int]) -> None:
        self.t += dt
        self.pulse += dt
        self._poll()
        super().update(dt, mouse)
        self.chat_field.update(mouse, dt)
        if self.anim is not None:
            self.anim["t"] += dt / 0.24
            if self.anim["t"] >= 1.0:
                self.anim = None
        snap = self.snap
        self.hover_col = None
        if snap and snap.get("phase") == "playing" and not self.is_spectator and not self._overlay():
            col = self.board.col_at(mouse)
            if col is not None and int(snap.get("turn") or 0) == self.me_seat:
                if col in (snap.get("legal") or []):
                    self.hover_col = col
        if not self._overlay() and self.board.rect.collidepoint(mouse):
            col = self.board.col_at(mouse)
            if col is not None:
                self.hover_col = col if col in ((snap or {}).get("legal") or []) else None

    def _overlay(self) -> Optional[str]:
        if self.confirm_leave:
            return "confirm"
        snap = self.snap
        if snap is None:
            return "connecting"
        if self.disconnected and not (self.server is not None):
            return "disconnected"
        if snap.get("phase") == "lobby":
            return "lobby"
        if snap.get("phase") == "over":
            return "over"
        return None

    # -- events ------------------------------------------------------------ #

    def overlay_buttons(self) -> List[theme.Button]:
        overlay = self._overlay()
        buttons: List[theme.Button] = []
        if overlay == "over":
            snap = self.snap or {}
            voted = self.me_seat in (snap.get("rematch") or [])
            label = i18n.t("game.rematch_voted") if voted else i18n.t("game.rematch")
            buttons.append(
                theme.Button((0, 0, 190, 46), label, lambda _b: self.vote_rematch(), kind="primary", size=17,
                             enabled=not voted and not self.is_spectator)
            )
            buttons.append(
                theme.Button((0, 0, 132, 46), i18n.t("game.leave"), lambda _b: self.app.goto("menu"), kind="ghost", size=15)
            )
        elif overlay == "lobby":
            if self.mode == "host":
                buttons.append(
                    theme.Button((0, 0, 150, 46), i18n.t("host.close_room"), lambda _b: self.app.goto("menu"), kind="ghost", size=14)
                )
                buttons.append(
                    theme.Button((0, 0, 86, 34), i18n.t("common.copy"), lambda _b: self.copy_address(), kind="chip", size=13)
                )
            else:
                buttons.append(
                    theme.Button((0, 0, 150, 46), i18n.t("game.leave"), lambda _b: self.app.goto("menu"), kind="ghost", size=14)
                )
        elif overlay == "confirm":
            buttons.append(
                theme.Button((0, 0, 130, 44), i18n.t("common.cancel"), lambda _b: self.set_confirm(False), kind="ghost", size=15)
            )
            buttons.append(
                theme.Button((0, 0, 130, 44), i18n.t("game.leave"), lambda _b: self.app.goto("menu"), kind="danger", size=15)
            )
        elif overlay == "disconnected":
            buttons.append(
                theme.Button((0, 0, 160, 44), i18n.t("game.leave"), lambda _b: self.app.goto("menu"), kind="primary", size=15)
            )
            if self.server is not None:
                buttons.append(
                    theme.Button((0, 0, 160, 44), "Reconnect", lambda _b: self.reconnect(), kind="ghost", size=15)
                )
        return buttons

    def set_confirm(self, value: bool) -> None:
        self.confirm_leave = value

    def set_ai_level(self, level: str) -> None:
        self.ai_level = level
        settings.save(ai_level=level)

    def start_ai(self) -> None:
        if self.client is None:
            return
        self.client.send({"t": "ai", "level": self.ai_level})

    def vote_rematch(self) -> None:
        if self.client is not None:
            self.client.send({"t": "rematch"})

    def copy_address(self) -> None:
        address = f"{self.app.local_ip}:{self.port}"
        self.app.copy_text(address)

    def reconnect(self) -> None:
        if self.server is None:
            return
        try:
            client = net_client.NetClient("127.0.0.1", self.server.port, self.app.name, "player")
            client.start()
        except OSError as exc:
            self.app.toast.show(str(exc), ERR)
            return
        self.client = client
        self.disconnected = False
        self.app.toast.show("✓", OK)

    def _overlay_buttons_positioned(self) -> List[theme.Button]:
        buttons = self.overlay_buttons()
        overlay = self._overlay()
        if overlay == "over":
            card = pygame.Rect(142, 265, 440, 262)
            buttons[0].rect = pygame.Rect(card.centerx - 96 - 66, card.y + 192, 190, 46)
            if len(buttons) > 1:
                buttons[1].rect = pygame.Rect(card.centerx + 56, card.y + 192, 132, 46)
        elif overlay == "lobby":
            card = pygame.Rect(102, 232, 520, 368)
            if self.mode == "host":
                if len(buttons) > 0:
                    buttons[0].rect = pygame.Rect(card.centerx - 75, card.bottom - 74, 150, 46)
                if len(buttons) > 1:
                    buttons[1].rect = pygame.Rect(card.right - 122, card.y + 112, 86, 34)
            else:
                buttons[0].rect = pygame.Rect(card.centerx - 75, card.bottom - 74, 150, 46)
        elif overlay == "confirm":
            card = pygame.Rect(342, 300, 480, 156)
            if len(buttons) > 0:
                buttons[0].rect = pygame.Rect(card.centerx - 140, card.bottom - 58, 130, 44)
            if len(buttons) > 1:
                buttons[1].rect = pygame.Rect(card.centerx + 10, card.bottom - 58, 130, 44)
        elif overlay == "disconnected":
            card = pygame.Rect(360, 320, 440, 150)
            if len(buttons) > 0:
                buttons[0].rect = pygame.Rect(card.centerx - 170, card.bottom - 62, 160, 44)
            if len(buttons) > 1:
                buttons[1].rect = pygame.Rect(card.centerx + 10, card.bottom - 62, 160, 44)
        return buttons

    def handle(self, event: pygame.event.Event) -> bool:
        overlay_buttons = self._overlay_buttons_positioned()
        for button in overlay_buttons:
            button.enabled and button.update(self.app.mouse)
            if button.handle(event):
                return True

        if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            if self._overlay() == "confirm":
                self.confirm_leave = False
            else:
                self.ask_leave()
            return True

        result = self.chat_field.handle(event)
        if result == "submit":
            self.send_chat()
            return True
        if result == "cancel":
            self.chat_field.focus(False)
            return True

        if super().handle(event):
            return True

        if event.type == pygame.MOUSEWHEEL:
            area = pygame.Rect(716, 200, 400, 404)
            if area.collidepoint(self.app.mouse):
                self.chat_scroll = max(0, self.chat_scroll + event.y * 42)
                return True
            return False

        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            if self.chat_field.rect.collidepoint(event.pos):
                self.chat_field.focus(True)
                return True
            self.chat_field.focus(False)
            if self._overlay() is None:
                col = self.board.col_at(event.pos)
                snap = self.snap or {}
                if col is not None and not self.is_spectator and int(snap.get("turn") or 0) == self.me_seat:
                    if col in (snap.get("legal") or []):
                        self.send_move(col)
                    else:
                        self.app.toast.show(i18n.t("err.bad_move"), ERR, 1.6)
                return True
        return False

    # -- drawing ----------------------------------------------------------- #

    def draw(self, surf: pygame.Surface) -> None:
        surf.blit(theme.background(DESIGN), (0, 0))
        snap = self.snap or {}
        self._draw_header(surf, snap)
        cells = snap.get("board") or engine.empty_cells()
        ghost_row = None
        if self.hover_col is not None and not self.is_spectator:
            ghost_row = engine.landing_row(cells, self.hover_col)
        live = snap.get("phase") == "playing" and not snap.get("draw")
        self.board.draw(
            surf,
            cells,
            hover_col=self.hover_col if live else None,
            ghost_row=ghost_row if live else None,
            ghost_seat=(self.me_seat or None) if live else None,
            win_line=snap.get("win_line"),
            anim=self.anim,
            pulse=self.pulse,
        )
        self._draw_sidebar(surf, snap)
        theme.draw_text(
            surf,
            i18n.t("game.moves_count", n=int(snap.get("moves") or 0)),
            13,
            MUTED,
            (46, 700),
        )
        if self.is_spectator:
            theme.pill(surf, (900, 692), i18n.t("game.spectating"), INK_SOFT, 13)
        self.draw_buttons(surf)
        self._draw_overlay(surf, snap)

    def _draw_header(self, surf: pygame.Surface, snap: dict) -> None:
        room = self.room if self.room and self.room != "????" else "…"
        pill_rect = theme.pill(surf, (46, 24), f"{i18n.t('host.code')} {room}", INK_SOFT, 13)
        if self.mode == "host" and self.port:
            theme.draw_text(
                surf,
                f"{self.app.local_ip}:{self.port}",
                12,
                DIM,
                (pill_rect.right + 12, 34),
            )

        self._draw_seat_card(surf, pygame.Rect(46, 66, 300, 56), engine.RED, snap)
        self._draw_seat_card(surf, pygame.Rect(814, 66, 300, 56), engine.YELLOW, snap)

        hint, color = self.hint_text(snap) if snap else (i18n.t("join.connecting"), MUTED)
        center = (560, 84)
        theme.draw_text(surf, hint, 18, color, center, anchor="center", bold=True)
        if snap.get("thinking") and snap.get("phase") == "playing":
            theme.spinner(surf, (center[0] - theme.text_size(hint, 18, True)[0] // 2 - 22, center[1]), 9, self.t, INK_SOFT)
        elif snap.get("phase") == "playing" and not self.is_spectator:
            turn_color = theme.SEAT_COLORS.get(int(snap.get("turn") or 0), INK)
            pygame.draw.circle(surf, turn_color, (center[0] - theme.text_size(hint, 18, True)[0] // 2 - 22, center[1]), 6)

    def _draw_seat_card(self, surf: pygame.Surface, rect: pygame.Rect, seat: int, snap: dict) -> None:
        info = self.seat_info(seat)
        active = snap.get("phase") == "playing" and int(snap.get("turn") or 0) == seat
        color = CARD_HI if active else CARD
        border = theme.blend(STROKE, theme.SEAT_COLORS[seat], 0.7) if active else STROKE_SOFT
        theme.panel(surf, rect, color, radius=14, border=border)
        theme.blit_center(surf, theme.disc_surface(17, seat), (rect.x + 30, rect.centery))
        name = self.seat_name(seat)
        you = seat == self.me_seat and not self.is_spectator
        label = _truncate(name, 16, 150, bold=True)
        theme.draw_text(surf, label, 16, INK if info else MUTED, (rect.x + 56, rect.y + 10), bold=True)
        sub = i18n.seat_label(seat)
        if you:
            sub += " · " + i18n.t("game.you")
        theme.draw_text(surf, sub, 12, MUTED, (rect.x + 56, rect.y + 32))
        theme.draw_text(surf, str(self.score(seat)), 26, theme.SEAT_COLORS[seat], (rect.right - 24, rect.centery), anchor="center", bold=True)

    def _draw_sidebar(self, surf: pygame.Surface, snap: dict) -> None:
        panel = pygame.Rect(716, 138, 400, 548)
        theme.panel(surf, panel, CARD, radius=18, border=STROKE_SOFT)
        theme.draw_text(surf, i18n.t("chat.title"), 17, INK, (panel.x + 24, panel.y + 20), bold=True)
        specs = int(snap.get("specs") or 0)
        pill_label = f"{i18n.t('host.spectators')} {specs}"
        pill_w = theme.text_size(pill_label, 12)[0] + 18
        theme.pill(surf, (panel.right - pill_w - 20, panel.y + 14), pill_label, MUTED, 12)
        theme.hline(surf, panel.x + 20, panel.right - 20, panel.y + 56)

        area = pygame.Rect(panel.x + 14, panel.y + 64, panel.w - 28, panel.h - 148)
        self._draw_chat(surf, snap.get("chat") or [], area)

        self.chat_field.rect = pygame.Rect(panel.x + 16, panel.bottom - 60, panel.w - 128, 44)
        self.chat_field.draw(surf)
        self.buttons[-1].rect = pygame.Rect(panel.right - 100, panel.bottom - 60, 84, 44)

    def _draw_chat(self, surf: pygame.Surface, messages: List[dict], area: pygame.Rect) -> None:
        if not messages:
            theme.draw_text(surf, i18n.t("chat.empty"), 13, DIM, (area.x + 10, area.y + 12))
            return
        width = area.w - 20
        blocks: List[Tuple[int, object]] = []
        for message in messages:
            if message.get("system"):
                text = self._system_text(message)
                lines = theme.wrap_text(text, 13, width)
                blocks.append((len(lines) * 18 + 12, ("sys", lines)))
            else:
                lines = theme.wrap_text(str(message.get("text") or ""), 15, width)
                blocks.append((22 + len(lines) * 20 + 8, ("msg", message, lines)))
        total = sum(height for height, _ in blocks)
        max_scroll = max(0, total - area.h)
        self.chat_scroll = max(0, min(self.chat_scroll, max_scroll))
        y = area.bottom - total + self.chat_scroll
        prev_clip = surf.get_clip()
        surf.set_clip(area)
        for height, payload in blocks:
            if y + height >= area.y and y <= area.bottom:
                if payload[0] == "sys":
                    for index, line in enumerate(payload[1]):
                        theme.draw_text(surf, line, 13, DIM, (area.x + 10, y + 4 + index * 18))
                else:
                    _, message, lines = payload
                    seat = int(message.get("seat") or 0)
                    color = theme.SEAT_COLORS.get(seat, MUTED)
                    name = str(message.get("name") or "")
                    if seat == self.me_seat and seat:
                        name += i18n.t("common.you")
                    theme.draw_text(surf, name, 13, color, (area.x + 10, y + 2), bold=True)
                    for index, line in enumerate(lines):
                        theme.draw_text(surf, line, 15, INK_SOFT, (area.x + 10, y + 22 + index * 20))
            y += height
        surf.set_clip(prev_clip)

    def _system_text(self, message: dict) -> str:
        code = message.get("code") or ""
        if code:
            key = "sys." + code
            args = message.get("args") or {}
            if isinstance(args, dict):
                try:
                    return i18n.t(key, **{k: str(v) for k, v in args.items()})
                except Exception:
                    pass
        return str(message.get("text") or "")

    def _draw_overlay(self, surf: pygame.Surface, snap: dict) -> None:
        overlay = self._overlay()
        if overlay is None:
            return
        dim = pygame.Surface(DESIGN, pygame.SRCALPHA)
        dim.fill((6, 9, 16, 120))
        surf.blit(dim, (0, 0))
        if overlay == "over":
            self._draw_result_card(surf, snap)
        elif overlay == "lobby":
            self._draw_lobby_card(surf, snap)
        elif overlay == "confirm":
            self._draw_confirm_card(surf)
        elif overlay == "disconnected":
            self._draw_disconnected_card(surf)
        elif overlay == "connecting":
            card = pygame.Rect(342, 300, 480, 170)
            _card_shadow(surf, card)
            theme.panel(surf, card, CARD, radius=20, border=STROKE_SOFT)
            theme.spinner(surf, (card.centerx, card.y + 62), 16, self.t, ACCENT)
            theme.draw_text(surf, i18n.t("join.connecting"), 18, INK_SOFT, (card.centerx, card.y + 112), anchor="center")

        for button in self._overlay_buttons_positioned():
            button.draw(surf)

    def _draw_result_card(self, surf: pygame.Surface, snap: dict) -> None:
        card = pygame.Rect(142, 265, 440, 262)
        _card_shadow(surf, card)
        theme.panel(surf, card, CARD, radius=20, border=STROKE_SOFT)
        draw = bool(snap.get("draw"))
        winner = snap.get("winner")
        if draw:
            title = i18n.t("game.draw")
            color = WARN
        else:
            title = i18n.t("game.wins", name=self.seat_name(int(winner or 0)))
            color = theme.SEAT_COLORS.get(int(winner or 0), INK)
        if not draw and winner:
            theme.blit_center(surf, theme.disc_surface(24, int(winner)), (card.centerx, card.y + 42))
            theme.draw_text(surf, title, 28, color, (card.centerx, card.y + 92), anchor="center", bold=True)
        else:
            theme.draw_text(surf, title, 32, color, (card.centerx, card.y + 66), anchor="center", bold=True)
        score_text = f"{self.seat_name(engine.RED)}  {self.score(engine.RED)} : {self.score(engine.YELLOW)}  {self.seat_name(engine.YELLOW)}"
        theme.draw_text(surf, _truncate(score_text, 15, card.w - 48), 15, INK_SOFT, (card.centerx, card.y + 128), anchor="center")
        voted = self.me_seat in (snap.get("rematch") or [])
        note = None
        if voted:
            note = (i18n.t("game.rematch_voted"), OK)
        elif snap.get("record_path"):
            note = (i18n.t("records.saved"), DIM)
        if note:
            theme.draw_text(surf, note[0], 12, note[1], (card.centerx, card.y + 156), anchor="center")

    def _draw_lobby_card(self, surf: pygame.Surface, snap: dict) -> None:
        card = pygame.Rect(102, 232, 520, 368)
        _card_shadow(surf, card)
        theme.panel(surf, card, CARD, radius=20, border=STROKE_SOFT)
        host = self.mode == "host"
        theme.draw_text(surf, i18n.t("host.title") if host else i18n.t("game.waiting_player"), 22, INK, (card.x + 40, card.y + 32), bold=True)
        theme.draw_text(surf, f"{i18n.t('host.code')} {self.room}", 13, MUTED, (card.x + 40, card.y + 74))
        if self.port:
            address = f"{self.app.local_ip}:{self.port}"
            theme.draw_text(surf, address, 24, ACCENT, (card.x + 40, card.y + 96), bold=True)
        else:
            theme.draw_text(surf, f"{i18n.t('host.code')} {self.room}", 24, ACCENT, (card.x + 40, card.y + 96), bold=True)
        if host:
            theme.hline(surf, card.x + 36, card.right - 36, card.y + 152, STROKE_SOFT)
        theme.spinner(surf, (card.centerx, card.y + 200), 16, self.t, ACCENT)
        theme.draw_text(surf, i18n.t("host.waiting"), 13, MUTED, (card.centerx, card.y + 246), anchor="center")

    def _draw_confirm_card(self, surf: pygame.Surface) -> None:
        card = pygame.Rect(342, 300, 480, 156)
        _card_shadow(surf, card)
        theme.panel(surf, card, CARD, radius=20, border=STROKE_SOFT)
        theme.draw_text(surf, i18n.t("game.leave_confirm"), 19, INK, (card.centerx, card.y + 48), anchor="center", bold=True)
        theme.draw_text(surf, i18n.t("game.leave"), 12, DIM, (card.centerx, card.y + 82), anchor="center")

    def _draw_disconnected_card(self, surf: pygame.Surface) -> None:
        card = pygame.Rect(360, 320, 440, 150)
        _card_shadow(surf, card)
        theme.panel(surf, card, CARD, radius=20, border=STROKE_SOFT)
        theme.draw_text(surf, i18n.t("game.disconnected"), 19, ERR, (card.centerx, card.y + 44), anchor="center", bold=True)


# --------------------------------------------------------------------------- #
# Replays
# --------------------------------------------------------------------------- #

class ReplayScreen(Screen):
    def __init__(self, app: App) -> None:
        super().__init__(app)
        self.entries: List[dict] = []
        self.selected = 0
        self.scroll = 0
        self.frames: List[dict] = []
        self.index = 0
        self.playing = False
        self.speed = 1.0
        self.accum = 0.0
        self.t = 0.0
        self.board = BoardView(origin=(571, 150), cell=66, radius=26, pad=18)
        self.error = ""

    def enter(self, **kwargs) -> None:
        self.entries = records.list_records()
        self.selected = 0
        self.scroll = 0
        self.playing = False
        self.build()
        if self.entries:
            self.load(0)
        else:
            self.frames = []

    def build(self) -> None:
        self.buttons = [
            self.lang_button(),
            theme.Button((46, 24, 96, 38), i18n.t("common.back"), lambda _b: self.app.goto("menu"), kind="ghost", size=14),
            theme.Button((986, 104, 128, 34), i18n.t("records.open_folder"),
                         lambda _b: records.open_folder(), kind="ghost", size=12),
            theme.Button((553, 580, 96, 44), i18n.t("records.prev"), lambda _b: self.step(-1), kind="chip", size=14),
            theme.Button((655, 580, 96, 44), i18n.t("records.play"), lambda _b: self.toggle_play(), kind="primary", size=15),
            theme.Button((757, 580, 96, 44), i18n.t("records.next"), lambda _b: self.step(1), kind="chip", size=14),
            theme.Button((859, 580, 96, 44), i18n.t("records.restart"), lambda _b: self.restart(), kind="chip", size=13),
        ]
        # speed chips
        for index, (label, value) in enumerate((("0.5×", 0.5), ("1×", 1.0), ("2×", 2.0), ("4×", 4.0))):
            self.buttons.append(
                theme.Button(
                    (596 + index * 54, 632, 48, 30),
                    label,
                    (lambda v: lambda _b: self.set_speed(v))(value),
                    kind="primary" if abs(self.speed - value) < 0.01 else "chip",
                    size=11,
                    radius=8,
                )
            )

    def relabel(self) -> None:
        self.build()

    def load(self, index: int) -> None:
        if not self.entries:
            return
        self.selected = max(0, min(index, len(self.entries) - 1))
        record = self.entries[self.selected]["record"]
        self.frames = records.frames_for(record)
        self.index = len(self.frames)
        self.playing = False
        self.accum = 0.0

    def toggle_play(self) -> None:
        if not self.frames:
            return
        if self.index >= len(self.frames):
            self.index = 0
        self.playing = not self.playing
        self.accum = 0.0

    def step(self, delta: int) -> None:
        self.playing = False
        self.index = max(0, min(self.index + delta, len(self.frames)))

    def restart(self) -> None:
        self.index = 0
        self.playing = True
        self.accum = 0.0

    def set_speed(self, speed: float) -> None:
        self.speed = speed
        self.build()

    def current(self) -> dict:
        if not self.frames:
            return {"board": engine.empty_cells(), "win_line": None}
        if self.index <= 0:
            return {"board": engine.empty_cells(), "win_line": None}
        return self.frames[min(self.index, len(self.frames)) - 1]

    # -- update ------------------------------------------------------------ #

    def update(self, dt: float, mouse: Tuple[int, int]) -> None:
        self.t += dt
        super().update(dt, mouse)
        if self.playing and self.frames:
            self.accum += dt * self.speed
            interval = 0.62
            while self.accum >= interval and self.playing:
                self.accum -= interval
                if self.index < len(self.frames):
                    self.index += 1
                if self.index >= len(self.frames):
                    self.playing = False

    def list_rects(self) -> List[pygame.Rect]:
        rects = []
        y = 152 - self.scroll
        for _ in self.entries:
            rects.append(pygame.Rect(46, y, 420, 74))
            y += 82
        return rects

    def handle(self, event: pygame.event.Event) -> bool:
        if event.type == pygame.KEYDOWN:
            if event.key == pygame.K_ESCAPE:
                self.app.goto("menu")
                return True
            if event.key == pygame.K_SPACE:
                self.toggle_play()
                return True
            if event.key == pygame.K_LEFT:
                self.step(-1)
                return True
            if event.key == pygame.K_RIGHT:
                self.step(1)
                return True
        if event.type == pygame.MOUSEWHEEL:
            if self.app.mouse[0] < 480:
                self.scroll = max(0, self.scroll - event.y * 48)
                self.scroll = min(self.scroll, max(0, len(self.entries) * 82 - 500))
                return True
        if event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            for index, rect in enumerate(self.list_rects()):
                if rect.collidepoint(event.pos):
                    self.load(index)
                    return True
        return super().handle(event)

    # -- draw -------------------------------------------------------------- #

    def draw(self, surf: pygame.Surface) -> None:
        surf.blit(theme.background(DESIGN), (0, 0))
        theme.draw_text(surf, i18n.t("records.title"), 32, INK, (156, 28), bold=True)
        theme.draw_text(surf, i18n.t("records.count", n=len(self.entries)), 13, MUTED, (46, 76))
        self.draw_buttons(surf)
        self._draw_list(surf)
        self._draw_preview(surf)
        if not self.entries:
            theme.draw_text(surf, i18n.t("records.empty"), 15, MUTED, (553, 300), anchor="center")

    def _draw_list(self, surf: pygame.Surface) -> None:
        panel = pygame.Rect(30, 108, 452, 584)
        theme.panel(surf, panel, CARD, radius=18, border=STROKE_SOFT)
        prev_clip = surf.get_clip()
        surf.set_clip(panel.inflate(-8, -14))
        for index, rect in enumerate(self.list_rects()):
            if not (panel.inflate(-8, -14).colliderect(rect)):
                continue
            entry = self.entries[index]
            info = entry["info"]
            active = index == self.selected
            pygame.draw.rect(surf, CARD_HI if active else CARD_DEEP, rect, border_radius=12)
            if active:
                pygame.draw.rect(surf, theme.blend(STROKE, ACCENT, 0.75), rect, width=1, border_radius=12)
            theme.blit_center(surf, theme.disc_surface(9, engine.RED), (rect.x + 22, rect.y + 22))
            theme.blit_center(surf, theme.disc_surface(9, engine.YELLOW), (rect.x + 42, rect.y + 22))
            names = _truncate(f"{info['red']} vs {info['yellow']}", 13, rect.w - 100, bold=True)
            theme.draw_text(surf, names, 13, INK, (rect.x + 64, rect.y + 12), bold=True)
            meta = i18n.t("records.meta", moves=info["moves"], when=info["when"])
            theme.draw_text(surf, meta, 11, MUTED, (rect.x + 16, rect.y + 40))
            if info["draw"]:
                theme.draw_text(surf, i18n.t("game.draw"), 11, WARN, (rect.right - 16, rect.y + 40), anchor="topright")
            elif info["winner"]:
                label = info["red"] if info["winner"] == 1 else info["yellow"]
                theme.draw_text(surf, "🏆 " + _truncate(label, 11, 90), 11, theme.SEAT_COLORS[info["winner"]],
                                (rect.right - 16, rect.y + 40), anchor="topright")
        surf.set_clip(prev_clip)

    def _draw_preview(self, surf: pygame.Surface) -> None:
        if not self.entries:
            return
        entry = self.entries[self.selected]
        info = entry["info"]
        frame = self.current()
        cells = frame.get("board") or engine.empty_cells()
        self.board.draw(surf, cells, win_line=frame.get("win_line"))
        theme.draw_text(surf, f"{info['red']}  vs  {info['yellow']}", 20, INK, (553, 104), bold=True)
        theme.draw_text(surf, i18n.t("records.speed"), 12, MUTED, (553, 640))
        counter = f"{min(self.index, len(self.frames))} / {len(self.frames)}"
        theme.draw_text(surf, counter, 15, INK_SOFT, (1090, 647), anchor="midright", bold=True)
        if self.error:
            theme.draw_text(surf, self.error, 13, ERR, (553, 664))


# --------------------------------------------------------------------------- #
# Play vs the AI, or watch it (no room needed)
# --------------------------------------------------------------------------- #

class WatchScreen(Screen):
    """Local games against the AI / bots, or spectate AI games.  No network.

    * ``me_ai``  - you against easy / normal / hard / the learned AI
    * ``ai_ai``  - the learned AI against itself, game after game
    * ``ai_bot`` - the learned AI against a search bot (colours swap every game)

    A running score is kept per player.
    """

    MODES = ("me_ai", "ai_ai", "ai_bot")

    def __init__(self, app: App) -> None:
        super().__init__(app)
        self.board = BoardView(origin=(76, 168), cell=76, radius=30, pad=20)
        self.mode = "me_ai"
        self.bot_level = "normal"  # opponent in ai_bot
        self.opp_level = "learned"  # opponent in me_ai
        self.my_seat = engine.RED
        self.speed = 1.0
        self.paused = False
        self.t = 0.0
        self.hover_col: Optional[int] = None
        self.game = engine.Game()
        self.game_no = 0
        self.tally = {"p0": 0, "p1": 0, "draw": 0}
        self.p0_seat = engine.RED
        self.ais: Dict[int, object] = {}
        self.results: "queue.Queue[tuple]" = queue.Queue()
        self.token = 0
        self.thinking = False
        self.wait = 0.0
        self.anim: Optional[dict] = None
        self.over_timer = 0.0
        self.model_ready = True

    @property
    def me_mode(self) -> bool:
        return self.mode == "me_ai"

    # -- lifecycle --------------------------------------------------------- #

    def enter(self, **kwargs) -> None:
        import ai as ai_module

        mode = kwargs.get("mode")
        if mode in self.MODES:
            self.mode = mode
        self.paused = False
        self.model_ready = ai_module.learned_available()
        self.build()
        self.new_series()

    def leave(self) -> None:
        self.token += 1  # drop any move still being computed

    def relabel(self) -> None:
        self.build()

    # -- widgets ----------------------------------------------------------- #

    def current_level(self) -> str:
        return self.opp_level if self.me_mode else self.bot_level

    def build(self) -> None:
        self.buttons = [
            self.lang_button(),
            theme.Button((46, 24, 96, 38), i18n.t("common.back"), lambda _b: self.app.goto("menu"), kind="ghost", size=14),
        ]
        for index, (mode, key) in enumerate(
            (("me_ai", "watch.mode_me_ai"), ("ai_ai", "watch.mode_ai_ai"), ("ai_bot", "watch.mode_ai_bot"))
        ):
            self.buttons.append(
                theme.Button(
                    (716 + index * 136, 146, 128, 40),
                    i18n.t(key),
                    (lambda m: lambda _b: self.set_mode(m))(mode),
                    kind="primary" if self.mode == mode else "chip",
                    size=14,
                )
            )
        if self.mode != "ai_ai":
            levels = ("easy", "normal", "hard", "learned") if self.me_mode else ("easy", "normal", "hard")
            for index, level in enumerate(levels):
                self.buttons.append(
                    theme.Button(
                        (716 + index * 96, 232, 88, 34),
                        i18n.level_label(level),
                        (lambda lv: lambda _b: self.set_level(lv))(level),
                        kind="primary" if self.current_level() == level else "chip",
                        size=13,
                    )
                )
        if self.me_mode:
            for index, (seat, key) in enumerate(((engine.RED, "watch.side_red"), (engine.YELLOW, "watch.side_yellow"))):
                self.buttons.append(
                    theme.Button(
                        (716 + index * 204, 316, 196, 34),
                        i18n.t(key),
                        (lambda s: lambda _b: self.set_side(s))(seat),
                        kind="primary" if self.my_seat == seat else "chip",
                        size=13,
                    )
                )
            self.buttons.append(
                theme.Button((716, 372, 196, 44), i18n.t("watch.again"), lambda _b: self.next_game(), kind="primary", size=15)
            )
            self.buttons.append(
                theme.Button((922, 372, 130, 44), i18n.t("watch.reset"), lambda _b: self.new_series(), kind="ghost", size=13)
            )
            return
        for index, (label, value) in enumerate((("0.5×", 0.5), ("1×", 1.0), ("2×", 2.0), ("4×", 4.0))):
            self.buttons.append(
                theme.Button(
                    (716 + index * 66, 316, 58, 32),
                    label,
                    (lambda v: lambda _b: self.set_speed(v))(value),
                    kind="primary" if abs(self.speed - value) < 0.01 else "chip",
                    size=12,
                    radius=9,
                )
            )
        self.buttons.append(
            theme.Button((716, 372, 150, 44), i18n.t("watch.resume") if self.paused else i18n.t("watch.pause"),
                         lambda _b: self.toggle_pause(), kind="primary", size=15)
        )
        self.buttons.append(
            theme.Button((876, 372, 130, 44), i18n.t("watch.next"), lambda _b: self.next_game(), kind="chip", size=14)
        )
        self.buttons.append(
            theme.Button((1016, 372, 102, 44), i18n.t("watch.reset"), lambda _b: self.new_series(), kind="ghost", size=12)
        )

    def set_mode(self, mode: str) -> None:
        if mode != self.mode:
            self.mode = mode
            self.paused = False
            self.build()
            self.new_series()

    def set_level(self, level: str) -> None:
        if level != self.current_level():
            if self.me_mode:
                self.opp_level = level
            else:
                self.bot_level = level
            self.build()
            self.new_series()

    def set_side(self, seat: int) -> None:
        if seat != self.my_seat:
            self.my_seat = seat
            self.build()
            self.new_series()

    def set_speed(self, speed: float) -> None:
        self.speed = speed
        self.build()

    def toggle_pause(self) -> None:
        if self.me_mode:
            return
        self.paused = not self.paused
        self.build()

    # -- game flow --------------------------------------------------------- #

    @staticmethod
    def level_name(level: str) -> str:
        if level == "learned":
            return i18n.t("watch.ai")
        return i18n.t("watch.bot", level=i18n.level_label(level))

    def names(self) -> Tuple[str, str]:
        if self.mode == "me_ai":
            return i18n.t("game.you"), self.level_name(self.opp_level)
        if self.mode == "ai_ai":
            return i18n.t("watch.ai_a"), i18n.t("watch.ai_b")
        return i18n.t("watch.ai"), self.level_name(self.bot_level)

    def new_series(self) -> None:
        self.tally = {"p0": 0, "p1": 0, "draw": 0}
        self.game_no = 0
        self.token += 1
        self.start_game()

    def next_game(self) -> None:
        self.token += 1
        self.start_game()

    def start_game(self) -> None:
        import ai as ai_module

        self.token += 1
        self.game_no += 1
        self.game = engine.Game()
        self.anim = None
        self.thinking = False
        self.wait = 0.4
        self.over_timer = 0.0
        seed = int(time.time() * 1000) % (1 << 30)
        if self.me_mode:
            self.p0_seat = self.my_seat
            self.ais = {engine.other_seat(self.p0_seat): ai_module.Connect4AI(self.opp_level, seed=seed)}
            return
        # alternate colours so neither player always moves first
        self.p0_seat = engine.RED if self.game_no % 2 == 1 else engine.YELLOW
        p1_seat = engine.other_seat(self.p0_seat)
        ai0 = ai_module.Connect4AI("learned", seed=seed)
        ai1 = ai_module.Connect4AI("learned" if self.mode == "ai_ai" else self.bot_level, seed=seed + 1)
        self.ais = {self.p0_seat: ai0, p1_seat: ai1}

    def seat_player(self, seat: int) -> str:
        return "p0" if seat == self.p0_seat else "p1"

    def _think(self, token: int, ai_obj, cells, seat: int) -> None:
        try:
            col = ai_obj.choose_move(cells, seat)
        except Exception:
            legal = engine.legal_moves(cells)
            col = legal[0] if legal else 0
        self.results.put((token, col))

    def play_col(self, col: int) -> None:
        game = self.game
        seat = game.turn
        row = game.drop(col)
        self.anim = {"row": row, "col": col, "seat": seat, "t": 0.0}
        self.wait = 0.25
        if game.over:
            if game.winner is not None:
                self.tally[self.seat_player(game.winner)] += 1
            else:
                self.tally["draw"] += 1

    def human_turn(self) -> bool:
        game = self.game
        return self.me_mode and not game.over and game.turn not in self.ais

    def update(self, dt: float, mouse: Tuple[int, int]) -> None:
        self.t += dt
        super().update(dt, mouse)
        game = self.game
        self.hover_col = None
        if self.paused:
            return
        step = dt * (1.0 if self.me_mode else self.speed)
        if self.anim:
            self.anim["t"] += step / 0.38
            if self.anim["t"] >= 1.0:
                self.anim = None
        if game.over:
            if not self.me_mode:
                self.over_timer += step
                if self.over_timer >= 2.2:
                    self.start_game()
            return
        if self.human_turn():
            col = self.board.col_at(mouse)
            if col is not None and not self.anim and game.can_play(col):
                self.hover_col = col
            return
        if self.anim:
            return
        if self.wait > 0:
            self.wait -= step
            return
        if not self.thinking:
            self.thinking = True
            seat = game.turn
            threading.Thread(
                target=self._think,
                args=(self.token, self.ais[seat], engine.clone_cells(game.cells), seat),
                daemon=True,
                name="c4-watch-ai",
            ).start()
            return
        try:
            token, col = self.results.get_nowait()
        except queue.Empty:
            return
        if token != self.token:
            return
        self.thinking = False
        if not game.can_play(col):
            legal = game.legal_moves()
            if not legal:
                return
            col = legal[0]
        self.play_col(col)

    def handle(self, event: pygame.event.Event) -> bool:
        if event.type == pygame.KEYDOWN:
            if event.key == pygame.K_ESCAPE:
                self.app.goto("menu")
                return True
            if event.key == pygame.K_SPACE and not self.me_mode:
                self.toggle_pause()
                return True
            if event.key == pygame.K_n:
                self.next_game()
                return True
        if super().handle(event):
            return True
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1 and self.human_turn() and not self.anim:
            col = self.board.col_at(event.pos)
            if col is not None:
                if self.game.can_play(col):
                    self.play_col(col)
                else:
                    self.app.toast.show(i18n.t("err.column_full"), ERR, 1.6)
                return True
        return False

    # -- draw -------------------------------------------------------------- #

    def draw(self, surf: pygame.Surface) -> None:
        surf.blit(theme.background(DESIGN), (0, 0))
        theme.draw_text(surf, i18n.t("watch.title"), 32, INK, (156, 28), bold=True)
        name0, name1 = self.names()
        names = {self.p0_seat: name0, engine.other_seat(self.p0_seat): name1}

        # who is who
        theme.draw_text(surf, i18n.t("watch.games", n=self.game_no), 13, MUTED, (76, 92))
        for seat, x in ((engine.RED, 76), (engine.YELLOW, 360)):
            theme.blit_center(surf, theme.disc_surface(11, seat), (x + 11, 130))
            theme.draw_text(surf, _truncate(names[seat], 16, 230, bold=True), 16, INK, (x + 30, 120), bold=True)

        game = self.game
        win_line = game.win_line if game.over else None
        ghost_row = engine.landing_row(game.cells, self.hover_col) if self.hover_col is not None else None
        self.board.draw(
            surf,
            game.cells,
            hover_col=self.hover_col,
            ghost_row=ghost_row,
            ghost_seat=game.turn if self.hover_col is not None else None,
            win_line=win_line,
            anim=self.anim,
            pulse=self.t,
        )

        # status line under the board
        if game.over:
            if game.winner is not None:
                text = i18n.t("watch.won", name=names[game.winner])
                color = theme.SEAT_COLORS[game.winner]
            else:
                text, color = i18n.t("game.draw"), WARN
        elif self.paused:
            text, color = i18n.t("watch.resume") + " ▸", MUTED
        elif self.human_turn():
            text, color = i18n.t("game.your_turn"), INK
        else:
            text, color = i18n.t("watch.thinking", name=names[game.turn]), INK_SOFT
        theme.draw_text(surf, text, 18, color, (76 + 273, 676), anchor="center", bold=True)

        # right panel
        panel = pygame.Rect(700, 108, 432, 586)
        theme.panel(surf, panel, CARD, radius=18, border=STROKE_SOFT)
        if self.mode != "ai_ai":
            key = "watch.opponent" if self.me_mode else "watch.bot_level"
            theme.draw_text(surf, i18n.t(key), 13, MUTED, (716, 208))
        theme.draw_text(surf, i18n.t("watch.side") if self.me_mode else i18n.t("watch.speed"), 13, MUTED, (716, 292))
        self._draw_score(surf, pygame.Rect(716, 440, 400, 150), name0, name1)
        if self.me_mode:
            theme.draw_text(surf, i18n.t("watch.hint_click"), 12, DIM, (716, 606))
        uses_learned = self.mode != "me_ai" or self.opp_level == "learned"
        if uses_learned and not self.model_ready:
            for index, line in enumerate(theme.wrap_text(i18n.t("watch.no_model"), 12, 396)[:3]):
                theme.draw_text(surf, line, 12, WARN, (716, 636 + index * 16))
        self.draw_buttons(surf)

    def _draw_score(self, surf: pygame.Surface, rect: pygame.Rect, name0: str, name1: str) -> None:
        theme.panel(surf, rect, CARD_DEEP, radius=14, border=STROKE_SOFT)
        theme.draw_text(surf, i18n.t("watch.score"), 13, MUTED, (rect.x + 18, rect.y + 14))
        cols = ((name0, self.tally["p0"], ACCENT), (name1, self.tally["p1"], WARN), (i18n.t("game.draw"), self.tally["draw"], INK_SOFT))
        width = rect.w // 3
        for index, (label, value, color) in enumerate(cols):
            cx = rect.x + width * index + width // 2
            theme.draw_text(surf, str(value), 40, color, (cx, rect.y + 70), anchor="center", bold=True)
            theme.draw_text(surf, _truncate(label, 12, width - 10), 12, MUTED, (cx, rect.y + 116), anchor="center")


# --------------------------------------------------------------------------- #
# Offscreen screenshots (documentation + visual smoke test)
# --------------------------------------------------------------------------- #

def _demo_snapshot(moves: Sequence[int], names: Tuple[str, str], *, chat=None, ai_seat=None) -> dict:
    game = engine.Game()
    for col in moves:
        if game.over:
            break
        game.drop(col)
    board = game.board()
    seats = {
        "1": {"name": names[0], "ai": ai_seat == 1, "level": "hard" if ai_seat == 1 else None, "connected": True},
        "2": {"name": names[1], "ai": ai_seat == 2, "level": "hard" if ai_seat == 2 else None, "connected": True},
    }
    phase = "over" if game.over else "playing"
    snap = {
        "t": "sync",
        "room": "7841",
        "seq": 12,
        "proto": 1,
        "phase": phase,
        "board": board,
        "moves": len(game.moves),
        "turn": game.turn,
        "winner": game.winner,
        "win_line": game.win_line,
        "draw": game.draw,
        "status": game.status,
        "legal": game.legal_moves(),
        "last": game.last_move(),
        "seats": seats,
        "ai": {"seat": ai_seat, "level": "hard", "name": "AI"} if ai_seat else None,
        "scores": {"1": 2, "2": 1},
        "specs": 1,
        "chat": chat or [],
        "rematch": [],
        "record": None,
        "record_path": None,
        "thinking": False,
        "you": {"id": 1, "name": names[0], "role": "player", "seat": 1},
    }
    return snap


DEMO_MOVES = [3, 2, 4, 4, 3, 3, 5, 2, 2, 6, 6, 5, 5, 1, 1]


def run_shots(outdir: Path, lang: str = "zh") -> List[Path]:
    """Render every screen offscreen; used for docs images and layout checks."""
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
    app = App(lang=lang, headless=True)
    app.window = pygame.display.set_mode(DESIGN)
    outdir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []

    def save(name: str) -> None:
        app._present = lambda: None  # type: ignore[method-assign]
        app.canvas.fill((0, 0, 0))
        screen = app.screens[app.current]
        screen.draw(app.canvas)  # type: ignore[attr-defined]
        app.toast.draw(app.canvas, time.monotonic())
        path = outdir / f"{name}.png"
        pygame.image.save(app.canvas, str(path))
        written.append(path)

    # menu with discovered rooms
    menu = app.screens["menu"]
    app.current = "menu"
    menu.enter(skip_scan=True)
    menu.scanning = True
    menu.rooms = [
        {"code": "7841", "host": "Kyle's MacBook", "ip": "192.168.1.23", "port": 51423, "players": 1, "phase": "lobby"},
        {"code": "2205", "host": "Living-Room-iMac", "ip": "192.168.1.42", "port": 51424, "players": 2, "phase": "playing"},
    ]
    save("01-menu")

    # join form
    join = app.screens["join"]
    app.current = "join"
    join.enter(host="192.168.1.23", port=51423)
    join.host_field.focused = True
    save("02-join")

    # game, mid-match
    game = app.screens["game"]
    app.current = "game"
    game.enter(mode="guest")
    game.mode = "guest"
    game.room = "7841"
    chat = [
        {"seat": 0, "name": "", "text": "New game — red starts.", "ts": 1, "system": True, "code": "new_game", "args": {}},
        {"seat": 1, "name": "Kyle", "text": "good luck!", "ts": 2, "system": False},
        {"seat": 2, "name": "Amy", "text": "你也是 :D", "ts": 3, "system": False},
        {"seat": 1, "name": "Kyle", "text": "第七列是我的了", "ts": 4, "system": False},
    ]
    game.snap = _demo_snapshot(DEMO_MOVES, ("Kyle", "Amy"), chat=chat)
    game.hover_col = 6
    game.buttons.clear()
    game.build()
    save("03-game")

    # lobby (host waiting)
    game.snap = {
        **_demo_snapshot([], ("Kyle", "—")),
        "phase": "lobby",
        "seats": {"1": {"name": "Kyle", "ai": False, "level": None, "connected": True}, "2": None},
        "scores": {"1": 0, "2": 0},
        "moves": 0,
        "specs": 0,
        "chat": [],
        "board": engine.empty_cells(),
    }
    game.mode = "host"
    game.port = 51423
    game.room = "7841"
    save("04-lobby")

    # game over
    over_moves = [3, 0, 3, 1, 3, 2, 3]
    game.snap = {
        **_demo_snapshot(over_moves, ("Kyle", "Amy")),
        "phase": "over",
        "record": {"v": 1, "game": "connect4", "columns": over_moves, "moves": []},
        "record_path": "records/c4_20261025-101500_Kyle_vs_Amy.json",
    }
    game.mode = "guest"
    save("05-game-over")

    # replays
    replay = app.screens["replays"]
    app.current = "replays"
    replay.enter()
    record = engine.make_record(
        names={"1": "Kyle", "2": "Amy"},
        moves=[{"seat": i % 2 + 1, "col": c} for i, c in enumerate(DEMO_MOVES)],
        winner=1,
        draw=False,
        started="2026-10-25T10:15:00+08:00",
        ended="2026-10-25T10:18:00+08:00",
    )
    replay.entries = [{"path": Path("demo.json"), "record": record, "info": records.summary(record), "mtime": 0.0}]
    replay.selected = 0
    replay.frames = records.frames_for(record)
    replay.index = len(replay.frames)
    replay.build()
    save("06-replay")

    app.shutdown()
    return written
