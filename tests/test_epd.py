"""Tests for dashboard.epd — layout engine."""
from __future__ import annotations

from PIL import Image, ImageDraw

from dashboard.epd import (
    Canvas, Col, FONT_MD, FONT_SM, Grid, HeaderBar, Icon, KV, Node,
    Padded, ProgressBar, Row, Section, Sep, Spacer, StatusDot, Table, Text,
    Card, font, text_width,
)


class TestFont:
    def test_loads_and_caches(self):
        f1 = font(12)
        f2 = font(12)
        assert f1 is f2

    def test_different_sizes(self):
        f_sm = font(10)
        f_lg = font(16)
        assert f_sm is not f_lg


class TestTextWidth:
    def test_returns_positive_for_text(self):
        draw = ImageDraw.Draw(Image.new("1", (100, 100)))
        w = text_width(draw, "hello", font(12))
        assert w > 0

    def test_empty_string_returns_zero(self):
        draw = ImageDraw.Draw(Image.new("1", (100, 100)))
        w = text_width(draw, "", font(12))
        assert w == 0


class TestLeafNodes:
    def test_text_paints(self):
        draw = ImageDraw.Draw(Image.new("1", (264, 176)))
        t = Text("Hello", size=FONT_SM)
        h = t.paint(draw, 0, 0, 264)
        assert h > 0

    def test_text_right_aligned(self):
        draw = ImageDraw.Draw(Image.new("1", (264, 176)))
        t = Text("Right", size=FONT_SM, align="right")
        h = t.paint(draw, 0, 0, 264)
        assert h > 0

    def test_text_bold(self):
        draw = ImageDraw.Draw(Image.new("1", (264, 176)))
        t = Text("Bold", size=FONT_SM, bold=True)
        h = t.paint(draw, 0, 0, 264)
        assert h > 0

    def test_spacer(self):
        draw = ImageDraw.Draw(Image.new("1", (100, 100)))
        s = Spacer(h=10)
        assert s.paint(draw, 0, 0, 100) == 10

    def test_sep(self):
        draw = ImageDraw.Draw(Image.new("1", (100, 100)))
        s = Sep()
        assert s.paint(draw, 0, 0, 100) == 3

    def test_progress_bar(self):
        draw = ImageDraw.Draw(Image.new("1", (264, 176)))
        pb = ProgressBar(pct=75.0)
        h = pb.paint(draw, 0, 0, 264)
        assert h > 0

    def test_progress_bar_clamps(self):
        draw = ImageDraw.Draw(Image.new("1", (264, 176)))
        pb_over = ProgressBar(pct=150.0)
        pb_under = ProgressBar(pct=-10.0)
        # Should not crash
        pb_over.paint(draw, 0, 0, 264)
        pb_under.paint(draw, 0, 0, 264)


class TestContainerNodes:
    def test_row_layout(self):
        draw = ImageDraw.Draw(Image.new("1", (264, 176)))
        row = Row([Text("A", size=FONT_SM), Text("B", size=FONT_SM)])
        h = row.paint(draw, 0, 0, 264)
        assert h > 0

    def test_col_layout(self):
        draw = ImageDraw.Draw(Image.new("1", (264, 176)))
        col = Col([Text("A", size=FONT_SM), Text("B", size=FONT_SM)])
        h = col.paint(draw, 0, 0, 264)
        assert h > 0

    def test_padded(self):
        draw = ImageDraw.Draw(Image.new("1", (264, 176)))
        p = Padded(child=Text("inner", size=FONT_SM), pad=10)
        h = p.paint(draw, 0, 0, 264)
        assert h > 0

    def test_grid(self):
        draw = ImageDraw.Draw(Image.new("1", (264, 176)))
        items = [Text(f"item{i}", size=FONT_SM) for i in range(6)]
        g = Grid(cols=3, row_h=14, items=items)
        h = g.paint(draw, 0, 0, 264)
        assert h == 28  # 6 items / 3 cols = 2 rows * 14

    def test_card(self):
        draw = ImageDraw.Draw(Image.new("1", (264, 176)))
        c = Card(children=[Text("toast", size=FONT_SM)])
        h = c.paint(draw, 10, 10, 200)
        assert h > 0


class TestCompositeNodes:
    def test_section(self):
        draw = ImageDraw.Draw(Image.new("1", (264, 176)))
        s = Section("Test", icon=Icon.CPU, children=[Text("content", size=FONT_SM)])
        h = s.paint(draw, 0, 0, 264)
        assert h > 0

    def test_section_with_badge(self):
        draw = ImageDraw.Draw(Image.new("1", (264, 176)))
        s = Section("Test", badge="3", children=[Text("content", size=FONT_SM)])
        h = s.paint(draw, 0, 0, 264)
        assert h > 0

    def test_kv(self):
        draw = ImageDraw.Draw(Image.new("1", (264, 176)))
        kv = KV("Key", "Value")
        h = kv.paint(draw, 0, 0, 264)
        assert h > 0

    def test_status_dot_up(self):
        draw = ImageDraw.Draw(Image.new("1", (264, 176)))
        sd = StatusDot("Service", up=True)
        h = sd.paint(draw, 0, 0, 264)
        assert h > 0

    def test_status_dot_down(self):
        draw = ImageDraw.Draw(Image.new("1", (264, 176)))
        sd = StatusDot("Service", up=False)
        h = sd.paint(draw, 0, 0, 264)
        assert h > 0

    def test_status_dot_unknown(self):
        draw = ImageDraw.Draw(Image.new("1", (264, 176)))
        sd = StatusDot("Service", up=None)
        h = sd.paint(draw, 0, 0, 264)
        assert h > 0

    def test_header_bar(self):
        draw = ImageDraw.Draw(Image.new("1", (264, 176)))
        hb = HeaderBar(left="L", center="C", right="R")
        h = hb.paint(draw, 0, 0, 264)
        assert h == 20

    def test_table(self):
        draw = ImageDraw.Draw(Image.new("1", (264, 176)))
        t = Table(
            columns=[("Name", 0), ("CPU", 100), ("RAM", 200)],
            rows=[["node1", "10%", "50%"], ["node2", "20%", "60%"]],
        )
        h = t.paint(draw, 0, 0, 264)
        assert h > 0


class TestCanvas:
    def test_renders_image(self):
        c = Canvas(264, 176, [Text("Hello", size=FONT_MD)])
        img = c.render()
        assert img.size == (264, 176)
        assert img.mode == "1"

    def test_renders_with_overlays(self):
        c = Canvas(264, 176, [Text("Hello", size=FONT_MD)])
        overlays = [Card(children=[Text("toast", size=FONT_SM)])]
        img = c.render_with_overlays(overlays)
        assert img.size == (264, 176)

    def test_empty_children(self):
        c = Canvas(264, 176, [])
        img = c.render()
        assert img.size == (264, 176)


class TestIcon:
    def test_wmo_known_codes(self):
        assert Icon.wmo(0) == Icon.W_SUNNY
        assert Icon.wmo(61) == Icon.W_RAIN

    def test_wmo_unknown_code(self):
        result = Icon.wmo(99999)
        assert isinstance(result, str)
