#!/usr/bin/env python3
"""Shared pygame look & feel for Connect Four: palette, fonts, primitives, widgets.

Everything draws with plain pygame so there are no image or font assets to ship;
the rounded panels, discs and text fields are generated at runtime and cached.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Sequence, Tuple

import math

import pygame

# --------------------------------------------------------------------------- #
# Palette
# --------------------------------------------------------------------------- #

INK = (233, 238, 248)
INK_SOFT = (198, 208, 226)
MUTED = (140, 155, 180)
DIM = (98, 112, 136)
BG_TOP = (12, 16, 26)
BG_BOT = (21, 28, 45)
CARD = (25, 33, 50)
CARD_HI = (31, 41, 62)
CARD_DEEP = (18, 24, 38)
STROKE = (45, 58, 84)
STROKE_SOFT = (35, 45, 66)
ACCENT = (88, 156, 255)
ACCENT_HOVER = (112, 174, 255)
ACCENT_DEEP = (44, 84, 150)
RED = (229, 79, 79)
RED_DEEP = (150, 42, 42)
YELLOW = (245, 197, 78)
YELLOW_DEEP = (168, 126, 26)
BOARD = (25, 57, 96)
BOARD_EDGE = (15, 36, 64)
BOARD_SHADE = (18, 43, 74)
HOLE = (9, 16, 27)
OK = (86, 214, 154)
WARN = (255, 178, 74)
ERR = (255, 108, 108)
WHITE = (255, 255, 255)

SEAT_COLORS = {1: RED, 2: YELLOW}
SEAT_DEEP = {1: RED_DEEP, 2: YELLOW_DEEP}

# --------------------------------------------------------------------------- #
# Fonts (first CJK-capable family that exists on this machine wins)
# --------------------------------------------------------------------------- #

_FONT_CANDIDATES = (
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/segoeui.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)
_FONT_PATH: Optional[str] = None
_FONTS: Dict[Tuple[int, bool], pygame.font.Font] = {}


def _pick_font_path() -> Optional[str]:
    global _FONT_PATH
    if _FONT_PATH is not None:
        return _FONT_PATH
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            _FONT_PATH = path
            return path
    for name in ("pingfang", "hiraginosansgb", "stheitilight", "notosanscjk", "arialunicodems", "dejavusans"):
        found = pygame.font.match_font(name)
        if found:
            _FONT_PATH = found
            return found
    _FONT_PATH = ""
    return None


def font(size: int, bold: bool = False) -> pygame.font.Font:
    key = (size, bold)
    cached = _FONTS.get(key)
    if cached is not None:
        return cached
    if not pygame.font.get_init():
        pygame.font.init()
    path = _pick_font_path()
    f = pygame.font.Font(path, size) if path else pygame.font.Font(None, size)
    f.set_bold(bold)
    _FONTS[key] = f
    return f


def text_size(content: str, size: int, bold: bool = False) -> Tuple[int, int]:
    return font(size, bold).size(content)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def blend(a: Sequence[int], b: Sequence[int], t: float) -> Tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))  # type: ignore[return-value]


def with_alpha(color: Sequence[int], alpha: int) -> Tuple[int, int, int, int]:
    return (color[0], color[1], color[2], max(0, min(255, alpha)))


_BG_CACHE: Dict[Tuple[int, int], pygame.Surface] = {}


def background(size: Tuple[int, int]) -> pygame.Surface:
    surf = _BG_CACHE.get(size)
    if surf is not None:
        return surf
    w, h = size
    surf = pygame.Surface(size).convert()
    for y in range(h):
        surf.fill(blend(BG_TOP, BG_BOT, y / max(1, h - 1)), (0, y, w, 1))
    _BG_CACHE[size] = surf
    return surf


def draw_text(
    surf: pygame.Surface,
    content: str,
    size: int,
    color: Sequence[int],
    pos: Tuple[int, int],
    anchor: str = "topleft",
    bold: bool = False,
) -> pygame.Rect:
    img = font(size, bold).render(content, True, tuple(color))
    rect = img.get_rect(**{anchor: pos})
    surf.blit(img, rect)
    return rect


def wrap_text(content: str, size: int, max_width: int, bold: bool = False) -> List[str]:
    """Greedy wrap that works for CJK (character-wise) and latin (word-wise)."""
    f = font(size, bold)
    lines: List[str] = []
    for paragraph in str(content).split("\n"):
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for ch in paragraph:
            if f.size(current + ch)[0] <= max_width:
                current += ch
            else:
                if current:
                    lines.append(current)
                current = ch
        if current:
            lines.append(current)
    return lines


def panel(
    surf: pygame.Surface,
    rect: pygame.Rect | Tuple[int, int, int, int],
    color: Sequence[int] = CARD,
    radius: int = 16,
    border: Optional[Sequence[int]] = STROKE_SOFT,
    border_width: int = 1,
) -> pygame.Rect:
    rect = pygame.Rect(rect)
    pygame.draw.rect(surf, tuple(color), rect, border_radius=radius)
    if border is not None:
        pygame.draw.rect(surf, tuple(border), rect, width=border_width, border_radius=radius)
    return rect


def hline(surf: pygame.Surface, x1: int, x2: int, y: int, color=STROKE_SOFT) -> None:
    pygame.draw.line(surf, tuple(color), (x1, y), (x2, y), 1)


# --------------------------------------------------------------------------- #
# Discs & holes (pre-rendered at 3x and downscaled for smooth edges)
# --------------------------------------------------------------------------- #

_DISC_CACHE: Dict[Tuple[int, int, bool], pygame.Surface] = {}
_HOLE_CACHE: Dict[int, pygame.Surface] = {}


def reset_caches() -> None:
    """Drop every surface/font cache -- needed after a pygame quit/re-init cycle."""
    _FONTS.clear()
    _BG_CACHE.clear()
    _DISC_CACHE.clear()
    _HOLE_CACHE.clear()


def _supersample(r: int) -> int:
    return 3 if r <= 60 else 2


def disc_surface(radius: int, seat: int, ghost: bool = False) -> pygame.Surface:
    """A glossy plastic disc: flat body, top-left sheen, bottom shading, rim."""
    key = (radius, seat, ghost)
    cached = _DISC_CACHE.get(key)
    if cached is not None:
        return cached
    ss = _supersample(radius)
    size = radius * 2 + 4
    big = pygame.Surface((size * ss, size * ss), pygame.SRCALPHA)
    cx = cy = size * ss // 2
    r = radius * ss
    color = SEAT_COLORS.get(seat, MUTED)
    deep = SEAT_DEEP.get(seat, DIM)
    light = tuple(min(255, int(c + (255 - c) * 0.45)) for c in color)

    # flat body, then a light-to-dark overlay stack for depth
    pygame.draw.circle(big, color + (255,), (cx, cy), r)
    light_layer = pygame.Surface((size * ss, size * ss), pygame.SRCALPHA)
    pygame.draw.circle(light_layer, light + (70,), (int(cx - r * 0.12), int(cy - r * 0.30)), int(r * 0.92))
    big.blit(light_layer, (0, 0))
    dark_layer = pygame.Surface((size * ss, size * ss), pygame.SRCALPHA)
    pygame.draw.circle(dark_layer, deep + (85,), (int(cx + r * 0.20), int(cy + r * 0.42)), int(r * 0.95))
    big.blit(dark_layer, (0, 0))
    sheen = pygame.Surface((size * ss, size * ss), pygame.SRCALPHA)
    pygame.draw.circle(sheen, (255, 255, 255, 95), (int(cx - r * 0.30), int(cy - r * 0.38)), max(1, int(r * 0.34)))
    big.blit(sheen, (0, 0))
    # rim
    pygame.draw.circle(big, deep + (255,), (cx, cy), r, max(2, int(2.4 * ss)))

    small = pygame.transform.smoothscale(big, (size, size))
    if ghost:
        small = small.copy()
        small.set_alpha(110)
    _DISC_CACHE[key] = small
    return small


def hole_surface(radius: int) -> pygame.Surface:
    cached = _HOLE_CACHE.get(radius)
    if cached is not None:
        return cached
    ss = _supersample(radius)
    size = radius * 2 + 4
    big = pygame.Surface((size * ss, size * ss), pygame.SRCALPHA)
    cx = cy = size * ss // 2
    r = radius * ss
    pygame.draw.circle(big, HOLE + (255,), (cx, cy), r)
    pygame.draw.circle(big, BOARD_SHADE + (255,), (cx, cy), max(1, int(r * 0.90)), max(1, int(3 * ss)))
    small = pygame.transform.smoothscale(big, (size, size))
    _HOLE_CACHE[radius] = small
    return small


def blit_center(surf: pygame.Surface, image: pygame.Surface, center: Tuple[int, int]) -> None:
    surf.blit(image, image.get_rect(center=center))


def disc(surf: pygame.Surface, center: Tuple[int, int], radius: int, seat: int, ghost: bool = False) -> None:
    blit_center(surf, disc_surface(radius, seat, ghost), center)


# --------------------------------------------------------------------------- #
# Buttons
# --------------------------------------------------------------------------- #

Kind = str  # "primary" | "secondary" | "ghost" | "danger" | "chip"


class Button:
    def __init__(
        self,
        rect: pygame.Rect | Tuple[int, int, int, int],
        label: str,
        on_click=None,
        kind: Kind = "primary",
        enabled: bool = True,
        size: int = 18,
        radius: int = 12,
        visible: bool = True,
        data=None,
    ) -> None:
        self.rect = pygame.Rect(rect)
        self.label = label
        self.on_click = on_click
        self.kind = kind
        self.enabled = enabled
        self.size = size
        self.radius = radius
        self.visible = visible
        self.data = data
        self.hover = False

    def update(self, mouse_pos: Tuple[int, int]) -> None:
        self.hover = self.visible and self.enabled and self.rect.collidepoint(mouse_pos)

    def handle(self, event: pygame.event.Event) -> bool:
        if (
            event.type == pygame.MOUSEBUTTONUP
            and event.button == 1
            and self.visible
            and self.enabled
            and self.rect.collidepoint(event.pos)
        ):
            if self.on_click:
                self.on_click(self)
            return True
        return False

    def draw(self, surf: pygame.Surface) -> None:
        if not self.visible:
            return
        draw_button(
            surf, self.rect, self.label, self.kind, self.hover and self.enabled, self.enabled, self.size, self.radius
        )


def draw_button(
    surf: pygame.Surface,
    rect: pygame.Rect,
    label: str,
    kind: Kind = "primary",
    hover: bool = False,
    enabled: bool = True,
    size: int = 18,
    radius: int = 12,
) -> None:
    base = CARD_HI
    fg = INK
    border: Optional[Sequence[int]] = STROKE
    if kind == "primary":
        base = ACCENT_HOVER if hover else ACCENT
        fg = (12, 20, 34)
        border = None
    elif kind == "secondary":
        base = CARD_HI if not hover else blend(CARD_HI, ACCENT, 0.16)
        border = STROKE
    elif kind == "ghost":
        base = None  # type: ignore[assignment]
        fg = INK_SOFT
        border = STROKE if not hover else ACCENT
    elif kind == "danger":
        base = (86, 32, 40) if not hover else (112, 40, 52)
        fg = (255, 190, 190)
        border = (140, 60, 74)
    elif kind == "chip":
        base = CARD_DEEP if not hover else CARD_HI
        fg = INK_SOFT
        border = STROKE
    if not enabled:
        base = CARD_DEEP if kind != "ghost" else None  # type: ignore[assignment]
        fg = DIM
        border = STROKE_SOFT
    if base is not None:
        pygame.draw.rect(surf, tuple(base), rect, border_radius=radius)
    if border is not None:
        pygame.draw.rect(surf, tuple(border), rect, width=1, border_radius=radius)
    draw_text(surf, label, size, fg if enabled else DIM, rect.center, anchor="center")


# --------------------------------------------------------------------------- #
# Text field
# --------------------------------------------------------------------------- #

class TextField:
    def __init__(
        self,
        rect: pygame.Rect | Tuple[int, int, int, int],
        text: str = "",
        placeholder: str = "",
        max_len: int = 40,
        numeric: bool = False,
        size: int = 18,
        on_change=None,
    ) -> None:
        self.rect = pygame.Rect(rect)
        self.text = text
        self.placeholder = placeholder
        self.max_len = max_len
        self.numeric = numeric
        self.size = size
        self.on_change = on_change
        self.focused = False
        self.hover = False
        self._caret_t = 0.0
        self._typed_since_focus = 0

    def focus(self, value: Optional[bool] = None) -> None:
        self.focused = bool(value) if value is not None else True
        self._caret_t = 0.0

    def update(self, mouse_pos, dt: float) -> None:
        self.hover = self.rect.collidepoint(mouse_pos)
        self._caret_t += dt

    # -- events ------------------------------------------------------------ #

    def handle(self, event: pygame.event.Event) -> Optional[str]:
        """Returns ``"submit"``, ``"cancel"``, ``"focus"``/``"blur"`` or ``None``."""
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            inside = self.rect.collidepoint(event.pos)
            was = self.focused
            self.focused = inside
            if inside and not was:
                self._caret_t = 0.0
                return "focus"
            if not inside and was:
                return "blur"
            return None
        if not self.focused:
            return None
        if event.type == pygame.KEYDOWN:
            if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                return "submit"
            if event.key == pygame.K_ESCAPE:
                self.focused = False
                return "cancel"
            if event.key == pygame.K_BACKSPACE:
                self.text = self.text[:-1]
                self._notify()
            return None
        if event.type == pygame.TEXTINPUT:
            chunk = event.text
            if self.numeric:
                chunk = "".join(ch for ch in chunk if ch.isdigit())
            room = self.max_len - len(self.text)
            if room > 0 and chunk:
                self.text = (self.text + chunk)[: self.max_len]
                self._notify()
        return None

    def _notify(self) -> None:
        if self.on_change:
            self.on_change(self.text)

    def draw(self, surf: pygame.Surface) -> None:
        pygame.draw.rect(surf, CARD_DEEP, self.rect, border_radius=10)
        border = ACCENT if self.focused else (STROKE if self.hover else STROKE_SOFT)
        pygame.draw.rect(surf, tuple(border), self.rect, width=1 if not self.focused else 2, border_radius=10)
        pad = 12
        content = self.text
        color = INK
        if not content:
            content = self.placeholder
            color = DIM
        inner = self.rect.inflate(-pad * 2, 0)
        img = font(self.size).render(content, True, tuple(color))
        clipping = img.get_width() > inner.width
        if clipping:
            crop = img.subsurface((img.get_width() - inner.width, 0, inner.width, img.get_height())).copy()
            surf.blit(crop, (inner.x, self.rect.centery - img.get_height() // 2))
        else:
            surf.blit(img, (inner.x, self.rect.centery - img.get_height() // 2))
        if self.focused and self._caret_t % 1.0 < 0.55:
            caret_x = inner.x + min(img.get_width(), inner.width) + 1
            pygame.draw.line(
                surf, (230, 238, 255), (caret_x, self.rect.centery - 11), (caret_x, self.rect.centery + 11), 2
            )


# --------------------------------------------------------------------------- #
# Toast
# --------------------------------------------------------------------------- #

class Toast:
    def __init__(self) -> None:
        self.message = ""
        self.color = INK
        self.until = 0.0

    def show(self, message: str, color: Sequence[int] = INK, seconds: float = 3.0) -> None:
        self.message = message
        self.color = tuple(color)
        import time as _time

        self.until = _time.monotonic() + seconds

    def draw(self, surf: pygame.Surface, now: float) -> None:
        if not self.message or now > self.until:
            return
        alpha = 1.0 if now < self.until - 0.4 else max(0.0, (self.until - now) / 0.4)
        img = font(15).render(self.message, True, tuple(self.color))
        pad_x, pad_y = 16, 10
        rect = pygame.Rect(0, 0, img.get_width() + pad_x * 2, img.get_height() + pad_y * 2)
        rect.midbottom = (surf.get_width() // 2, surf.get_height() - 18)
        box = pygame.Surface(rect.size, pygame.SRCALPHA)
        pygame.draw.rect(box, (18, 24, 38, int(235 * alpha)), box.get_rect(), border_radius=12)
        pygame.draw.rect(box, (70, 88, 120, int(200 * alpha)), box.get_rect(), width=1, border_radius=12)
        surf.blit(box, rect)
        img.set_alpha(int(255 * alpha))
        surf.blit(img, (rect.x + pad_x, rect.y + pad_y))


# --------------------------------------------------------------------------- #
# Small composite pieces
# --------------------------------------------------------------------------- #

def badge(surf: pygame.Surface, rect: pygame.Rect, label: str, color=ACCENT, size: int = 13) -> None:
    pygame.draw.rect(surf, tuple(color), rect, border_radius=rect.height // 2)
    draw_text(surf, label, size, (10, 16, 28), rect.center, anchor="center", bold=True)


def pill(surf: pygame.Surface, pos: Tuple[int, int], label: str, color=INK_SOFT, size: int = 13) -> pygame.Rect:
    img = font(size).render(label, True, tuple(color))
    rect = pygame.Rect(pos[0], pos[1], img.get_width() + 18, img.get_height() + 8)
    pygame.draw.rect(surf, CARD_DEEP, rect, border_radius=rect.height // 2)
    pygame.draw.rect(surf, STROKE_SOFT, rect, width=1, border_radius=rect.height // 2)
    surf.blit(img, (rect.x + 9, rect.y + 4))
    return rect


def spinner(surf: pygame.Surface, center: Tuple[int, int], radius: int, t: float, color=ACCENT) -> None:
    import math

    for i in range(8):
        angle = t * 5.0 + i * math.pi / 4
        alpha = 60 + int(180 * ((i / 8) % 1.0))
        x = center[0] + int(math.cos(angle) * radius)
        y = center[1] + int(math.sin(angle) * radius)
        pygame.draw.circle(surf, with_alpha(color, alpha)[:3], (x, y), max(2, radius // 6))
