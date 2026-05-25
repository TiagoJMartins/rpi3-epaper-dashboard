"""
epd — Declarative layout engine for 1-bit e-paper displays.

Build a tree of layout nodes, then render to a Pillow Image.
No CSS, no floats — just integer pixels, top-down flow, and explicit structure.

Usage:
    from epd import Canvas, Row, Text, Section, Grid, Sep, Icon

    img = Canvas(264, 176, [
        Row([Text("16:30", bold=True), Text("Dom 24 Mai", align="right")]),
        Sep(),
        Section("Sistema", icon="monitor", children=[
            Row([Text(f"{Icon.CPU} 12% 44°C"), Text(f"{Icon.RAM} 256/920M")]),
        ]),
    ]).render()
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageDraw, ImageFont


# ── Icon registry ─────────────────────────────────────────────────
class Icon:
    """Nerd Font glyph constants. Use as f"{Icon.CPU} 12%"."""

    # System
    CPU       = "\uf2db"  # nf-fa-microchip
    RAM       = "\uefc5"  # nf-md-memory
    DISK      = "\uf0a0"  # nf-fa-hdd_o
    CLOCK     = "\uf017"  # nf-fa-clock_o
    MONITOR   = "\uf108"  # nf-fa-desktop
    LOAD      = "\uf080"  # nf-fa-bar_chart
    TEMP      = "\uf2c9"  # nf-fa-thermometer_half
    NETWORK   = "\uf0ac"  # nf-fa-globe
    SERVER    = "\uf233"  # nf-fa-server

    # Status
    CHECK     = "\uf00c"  # nf-fa-check
    WARNING   = "\uf071"  # nf-fa-exclamation_triangle
    ERROR     = "\uf00d"  # nf-fa-times
    CIRCLE    = "\uf111"  # nf-fa-circle
    CIRCLE_O  = "\uf10c"  # nf-fa-circle_o
    INFO      = "\uf05a"  # nf-fa-info_circle
    BELL      = "\uf0f3"  # nf-fa-bell
    CHEVRON   = "\uf054"  # nf-fa-chevron_right
    ARROW_UP  = "\uf062"  # nf-fa-arrow_up

    # Media
    PLAY      = "\uf04b"  # nf-fa-play
    PAUSE     = "\uf04c"  # nf-fa-pause
    STOP      = "\uf04d"  # nf-fa-stop
    FILM      = "\uf008"  # nf-fa-film

    # Weather
    SUN       = "\uf185"  # nf-fa-sun_o
    W_SUNNY   = "\ue302"  # nf-weather-day_sunny
    W_CLOUDY  = "\ue312"  # nf-weather-cloudy
    W_FOG     = "\ue313"  # nf-weather-fog
    W_SHOWERS = "\ue309"  # nf-weather-showers
    W_RAIN    = "\ue318"  # nf-weather-rain
    W_SNOW    = "\ue31a"  # nf-weather-snow
    W_THUNDER = "\ue31d"  # nf-weather-thunderstorm
    W_HUMID   = "\ue373"  # nf-weather-humidity
    W_WIND    = "\ue34b"  # nf-weather-strong_wind

    @staticmethod
    def wmo(code: int) -> str:
        """WMO weather code → nerd font icon."""
        if code <= 1: return Icon.W_SUNNY
        if code <= 3: return Icon.W_CLOUDY
        if code in (45, 48): return Icon.W_FOG
        if code in (51, 53, 55, 56, 57): return Icon.W_SHOWERS
        if code in (61, 63, 65, 66, 67): return Icon.W_RAIN
        if code in (71, 73, 75, 77): return Icon.W_SNOW
        if code in (80, 81, 82): return Icon.W_RAIN
        if code in (85, 86): return Icon.W_SNOW
        if code in (95, 96, 99): return Icon.W_THUNDER
        return "?"


# ── Font management ───────────────────────────────────────────────
_PKG_DIR = Path(__file__).parent
_FONT_SEARCH = {
    False: [
        # Package-relative (dev: repo_root/fonts/, installed: alongside package)
        _PKG_DIR / "fonts" / "JetBrainsMonoNerdFontPropo-Regular.ttf",
        _PKG_DIR.parent.parent / "fonts" / "JetBrainsMonoNerdFontPropo-Regular.ttf",
        # Home directory (Pi deploy)
        Path.home() / "fonts" / "JetBrainsMonoNerdFontPropo-Regular.ttf",
        Path.home() / ".local/share/fonts/JetBrainsMonoNerdFontPropo-Regular.ttf",
        # System fallback
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ],
    True: [
        _PKG_DIR / "fonts" / "JetBrainsMonoNerdFontPropo-Bold.ttf",
        _PKG_DIR.parent.parent / "fonts" / "JetBrainsMonoNerdFontPropo-Bold.ttf",
        Path.home() / "fonts" / "JetBrainsMonoNerdFontPropo-Bold.ttf",
        Path.home() / ".local/share/fonts/JetBrainsMonoNerdFontPropo-Bold.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ],
}
_font_cache: dict[tuple[int, bool], ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load and cache a font at the given size."""
    key = (size, bold)
    if key not in _font_cache:
        for fp in _FONT_SEARCH[bold]:
            if fp.exists():
                _font_cache[key] = ImageFont.truetype(str(fp), size)
                break
        else:
            _font_cache[key] = ImageFont.load_default()
    return _font_cache[key]


def text_width(draw: ImageDraw.ImageDraw, text: str,
               f: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> int:
    """Measure rendered text width."""
    bbox = draw.textbbox((0, 0), text, font=f)
    return bbox[2] - bbox[0]



def fit_text(draw: ImageDraw.ImageDraw, text: str,
             f: ImageFont.FreeTypeFont | ImageFont.ImageFont,
             max_w: int) -> str:
    """Truncate *text* with '…' so it fits within *max_w* pixels."""
    if max_w <= 0:
        return ''
    tw = text_width(draw, text, f)
    if tw <= max_w:
        return text
    ellipsis = '…'
    ew = text_width(draw, ellipsis, f)
    # Binary search for longest prefix that fits with ellipsis
    lo, hi, best = 0, len(text), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if text_width(draw, text[:mid], f) + ew <= max_w:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return text[:best] + ellipsis if best < len(text) else text

# ── Default sizes ─────────────────────────────────────────────────
FONT_SM = 11
FONT_MD = 13
FONT_LG = 15
PAD = 4  # standard horizontal padding


# ── Layout nodes ──────────────────────────────────────────────────
class Node:
    """Base layout node. Subclasses implement paint()."""

    __slots__ = ()

    def paint(self, draw: ImageDraw.ImageDraw, x: int, y: int, w: int) -> int:
        """Render at (x, y) with available width w. Returns height consumed."""
        return 0


# ── Leaf nodes ────────────────────────────────────────────────────
@dataclass(slots=True)
class Text(Node):
    """Single text span."""

    text: str
    size: int = FONT_MD
    bold: bool = False
    align: str = "left"  # left | center | right
    fill: int = 0

    def paint(self, draw: ImageDraw.ImageDraw, x: int, y: int, w: int) -> int:
        f = font(self.size, self.bold)
        t = fit_text(draw, self.text, f, w)
        tw = text_width(draw, t, f)
        if self.align == "right":
            tx = x + w - tw
        elif self.align == "center":
            tx = x + (w - tw) // 2
        else:
            tx = x
        draw.text((tx, y), t, font=f, fill=self.fill)
        bbox = draw.textbbox((0, 0), t, font=f)
        return bbox[3] - bbox[1] + 2  # text height + 2px breathing room


@dataclass(slots=True)
class Spacer(Node):
    """Vertical gap."""

    h: int = 3

    def paint(self, draw: ImageDraw.ImageDraw, x: int, y: int, w: int) -> int:
        return self.h


@dataclass(slots=True)
class Sep(Node):
    """Horizontal separator line."""

    def paint(self, draw: ImageDraw.ImageDraw, x: int, y: int, w: int) -> int:
        draw.line([(x, y + 1), (x + w, y + 1)], fill=0)
        return 3


@dataclass(slots=True)
class ProgressBar(Node):
    """Filled progress bar."""

    pct: float
    bar_w: int = 100
    bar_h: int = 6

    def paint(self, draw: ImageDraw.ImageDraw, x: int, y: int, w: int) -> int:
        pct = max(0.0, min(100.0, self.pct))
        draw.rectangle([x, y, x + self.bar_w, y + self.bar_h], outline=0)
        fill_w = int(self.bar_w * pct / 100)
        if fill_w > 0:
            draw.rectangle([x, y, x + fill_w, y + self.bar_h], fill=0)
        return self.bar_h + 2


# ── Container nodes ───────────────────────────────────────────────
@dataclass(slots=True)
class Row(Node):
    """Horizontal layout. Children split width evenly or by weights.

    With h=N, all children paint at the same y within a fixed-height row.
    Without h, height is the max child height.
    """

    children: list[Node] = field(default_factory=list)
    h: int = 0  # 0 = auto
    weights: list[int] | None = None  # e.g. [2, 1] for 2:1 split

    def paint(self, draw: ImageDraw.ImageDraw, x: int, y: int, w: int) -> int:
        n = len(self.children)
        if not n:
            return self.h

        if self.weights and len(self.weights) == n:
            total_w = sum(self.weights)
            widths = [w * wt // total_w for wt in self.weights]
        else:
            widths = [w // n] * n

        max_h = 0
        cx = x
        for child, cw in zip(self.children, widths):
            ch = child.paint(draw, cx, y, cw)
            max_h = max(max_h, ch)
            cx += cw

        return self.h or max_h


@dataclass(slots=True)
class Col(Node):
    """Vertical stack. Children flow top-down."""

    children: list[Node] = field(default_factory=list)

    def paint(self, draw: ImageDraw.ImageDraw, x: int, y: int, w: int) -> int:
        cy = y
        for child in self.children:
            cy += child.paint(draw, x, cy, w)
        return cy - y


@dataclass(slots=True)
class Padded(Node):
    """Adds horizontal padding around a child."""

    child: Node | None = None
    pad: int = PAD

    def paint(self, draw: ImageDraw.ImageDraw, x: int, y: int, w: int) -> int:
        if not self.child:
            return 0
        return self.child.paint(draw, x + self.pad, y, w - self.pad * 2)


@dataclass(slots=True)
class Grid(Node):
    """N-column grid with fixed row height."""

    items: list[Node] = field(default_factory=list)
    cols: int = 3
    row_h: int = 14

    def paint(self, draw: ImageDraw.ImageDraw, x: int, y: int, w: int) -> int:
        col_w = w // self.cols
        for i, item in enumerate(self.items):
            c, r = i % self.cols, i // self.cols
            item.paint(draw, x + c * col_w, y + r * self.row_h, col_w)
        rows = (len(self.items) + self.cols - 1) // self.cols
        return rows * self.row_h


@dataclass(slots=True)
class Card(Node):
    """Bordered rectangle with content inside. For toast overlays."""

    children: list[Node] = field(default_factory=list)
    pad: int = 4
    margin: int = 6

    def measure(self, w: int) -> int:
        """Calculate height without painting."""
        dummy = ImageDraw.Draw(Image.new("1", (1, 1)))
        inner_w = w - self.margin * 2 - self.pad * 2
        h = self.pad * 2
        for child in self.children:
            h += child.paint(dummy, 0, 0, inner_w)
        return h

    def paint(self, draw: ImageDraw.ImageDraw, x: int, y: int, w: int) -> int:
        inner_w = w - self.margin * 2 - self.pad * 2
        # Measure first
        content_h = 0
        for child in self.children:
            content_h += child.paint(ImageDraw.Draw(Image.new("1", (1, 1))), 0, 0, inner_w)
        card_h = content_h + self.pad * 2

        # Draw border
        draw.rectangle(
            [x + self.margin, y, x + w - self.margin, y + card_h],
            fill=1, outline=0,
        )
        # Draw children
        cy = y + self.pad
        ix = x + self.margin + self.pad
        for child in self.children:
            cy += child.paint(draw, ix, cy, inner_w)

        return card_h


# ── Composite patterns ────────────────────────────────────────────
@dataclass(slots=True)
class Section(Node):
    """Section header + vertical content. Standard pattern for widget blocks.

    Renders: margin-top, bold title with optional icon and right-badge, then children.
    """

    title: str
    icon: str = ""  # prepended to title
    badge: str = ""  # right-aligned
    children: list[Node] = field(default_factory=list)
    margin_top: int = 3

    def paint(self, draw: ImageDraw.ImageDraw, x: int, y: int, w: int) -> int:
        fb = font(FONT_LG, bold=True)
        f = font(FONT_SM)

        cy = y + self.margin_top
        label = f"{self.icon} {self.title}" if self.icon else self.title
        inner = w - PAD * 2
        if self.badge:
            badge_w = text_width(draw, self.badge, f) + PAD
            title_text = fit_text(draw, label, fb, inner - badge_w)
        else:
            title_text = fit_text(draw, label, fb, inner)
        draw.text((x + PAD, cy), title_text, font=fb, fill=0)

        if self.badge:
            tw = text_width(draw, self.badge, f)
            draw.text((x + w - PAD - tw, cy + 2), self.badge, font=f, fill=0)

        cy += 18

        for child in self.children:
            cy += child.paint(draw, x + PAD, cy, w - PAD * 2)

        return cy - y


@dataclass(slots=True)
class KV(Node):
    """Key-value pair: key left-aligned, value right-aligned on same line."""

    key: str
    value: str
    size: int = FONT_SM

    def paint(self, draw: ImageDraw.ImageDraw, x: int, y: int, w: int) -> int:
        f = font(self.size)
        kw = text_width(draw, self.key, f)
        gap = 4
        val_max = w - kw - gap
        val_text = fit_text(draw, self.value, f, val_max)
        draw.text((x, y), self.key, font=f, fill=0)
        vw = text_width(draw, val_text, f)
        draw.text((x + w - vw, y), val_text, font=f, fill=0)
        bbox = draw.textbbox((0, 0), self.key, font=f)
        return bbox[3] - bbox[1] + 2


@dataclass(slots=True)
class StatusDot(Node):
    """Service status indicator: check/warning icon + label."""

    name: str
    up: bool | None = None  # None = unknown/loading
    size: int = FONT_SM

    def paint(self, draw: ImageDraw.ImageDraw, x: int, y: int, w: int) -> int:
        f = font(self.size)
        if self.up is None:
            dot = "?"
        elif self.up:
            dot = Icon.CHECK
        else:
            dot = Icon.WARNING
        full = f"{dot} {self.name}"
        t = fit_text(draw, full, f, w)
        draw.text((x, y), t, font=f, fill=0)
        bbox = draw.textbbox((0, 0), t, font=f)
        return bbox[3] - bbox[1] + 2


@dataclass(slots=True)
class HeaderBar(Node):
    """Top header bar: left text, optional center text, right text, then separator.

    Standard pattern for clock + weather + date.
    """

    left: str = ""
    center: str = ""
    right: str = ""

    def paint(self, draw: ImageDraw.ImageDraw, x: int, y: int, w: int) -> int:
        fb = font(FONT_LG, bold=True)
        f = font(FONT_MD)

        # Measure right first, then allocate remaining to left, then center
        rw = (text_width(draw, self.right, f) + PAD * 2) if self.right else 0
        left_max = w - rw
        if self.left:
            lt = fit_text(draw, self.left, fb, left_max)
            draw.text((x + PAD, y + 1), lt, font=fb, fill=0)
            lw = text_width(draw, lt, fb) + PAD * 2
        else:
            lw = 0
        if self.center:
            center_max = w - lw - rw
            ct = fit_text(draw, self.center, fb, center_max)
            cw = text_width(draw, ct, fb)
            draw.text((x + (w - cw) // 2, y + 1), ct, font=fb, fill=0)
        if self.right:
            tw = text_width(draw, self.right, f)
            draw.text((x + w - PAD - tw, y + 3), self.right, font=f, fill=0)

        draw.line([(x, y + 18), (x + w, y + 18)], fill=0)
        return 20


@dataclass(slots=True)
class Table(Node):
    """Table with header row + data rows. Fixed column positions.

    columns: list of (label, width_px) — widths are absolute pixel positions from left edge.
    rows:    list of list[str] matching column count.
    """

    columns: list[tuple[str, int]] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    row_h: int = 13
    size: int = FONT_SM

    def paint(self, draw: ImageDraw.ImageDraw, x: int, y: int, w: int) -> int:
        f = font(self.size)
        cy = y

        # Header
        for label, col_x in self.columns:
            draw.text((x + col_x, cy), label, font=f, fill=0)
        cy += self.row_h

        # Data
        for row in self.rows:
            for i, cell in enumerate(row):
                if i < len(self.columns):
                    draw.text((x + self.columns[i][1], cy), cell, font=f, fill=0)
            cy += self.row_h

        return cy - y


# ── Canvas (top-level) ────────────────────────────────────────────
@dataclass
class Canvas:
    """Root container. Renders children top-down into a Pillow 1-bit Image."""

    w: int
    h: int
    children: list[Node] = field(default_factory=list)

    def render(self) -> Image.Image:
        img = Image.new("1", (self.w, self.h), 1)
        draw = ImageDraw.Draw(img)
        y = 0
        for child in self.children:
            if y >= self.h:
                break
            y += child.paint(draw, 0, y, self.w)
        return img

    def render_with_overlays(self, overlays: list[Card] | None = None) -> Image.Image:
        """Render children top-down, then overlay cards from the bottom up."""
        img = Image.new("1", (self.w, self.h), 1)
        draw = ImageDraw.Draw(img)

        y = 0
        for child in self.children:
            if y >= self.h:
                break
            y += child.paint(draw, 0, y, self.w)

        if overlays:
            bottom = self.h - 2
            for card in overlays:
                card_h = card.measure(self.w)
                card_top = bottom - card_h
                if card_top < 20:  # don't overlap header
                    break
                card.paint(draw, 0, card_top, self.w)
                bottom = card_top - 3

        return img
