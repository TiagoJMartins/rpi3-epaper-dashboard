"""Tests for dashboard.display — pack, FrameBuffer, MockEPD."""
from __future__ import annotations

import threading
from io import BytesIO

from PIL import Image

from dashboard.display import BUF_SIZE, H, W, FrameBuffer, MockEPD, pack


class TestPack:
    def test_white_image_produces_all_ff(self):
        img = Image.new("1", (W, H), 1)
        buf = pack(img)
        assert len(buf) == BUF_SIZE
        assert buf == b'\xff' * BUF_SIZE

    def test_black_image_produces_all_00(self):
        img = Image.new("1", (W, H), 0)
        buf = pack(img)
        assert len(buf) == BUF_SIZE
        assert buf == b'\x00' * BUF_SIZE

    def test_portrait_packs_differently(self):
        landscape_img = Image.new("1", (W, H), 0)
        portrait_img = Image.new("1", (H, W), 0)
        assert pack(landscape_img) == pack(portrait_img, portrait=True)

    def test_single_black_pixel_changes_buffer(self):
        white = Image.new("1", (W, H), 1)
        with_pixel = Image.new("1", (W, H), 1)
        with_pixel.putpixel((0, 0), 0)
        assert pack(white) != pack(with_pixel)

    def test_roundtrip_preserves_content(self):
        img = Image.new("1", (W, H), 1)
        for x in range(10):
            img.putpixel((x, 0), 0)
        buf = pack(img)
        assert buf != b'\xff' * BUF_SIZE


class TestFrameBuffer:
    def test_initially_empty(self):
        fb = FrameBuffer()
        assert fb.png == b""

    def test_set_and_get(self):
        fb = FrameBuffer()
        fb.png = b"fake-png-data"
        assert fb.png == b"fake-png-data"

    def test_thread_safety(self):
        fb = FrameBuffer()
        errors = []

        def writer():
            for i in range(100):
                fb.png = f"frame-{i}".encode()

        def reader():
            for _ in range(100):
                val = fb.png
                if not isinstance(val, bytes):
                    errors.append(f"got {type(val)}")

        t1 = threading.Thread(target=writer)
        t2 = threading.Thread(target=reader)
        t1.start(); t2.start()
        t1.join(); t2.join()
        assert not errors


class TestMockEPD:
    def test_saves_frame_on_display(self, tmp_path):
        fb = FrameBuffer()
        img = Image.new("1", (W, H), 1)
        bio = BytesIO()
        img.save(bio, format="PNG")
        fb.png = bio.getvalue()

        epd = MockEPD(tmp_path / "frames", fb)
        epd.init()
        epd.display_base(b"")
        assert (tmp_path / "frames" / "frame_0000.png").exists()
        assert (tmp_path / "frames" / "latest.png").exists()
        epd.display_partial(b"")
        assert (tmp_path / "frames" / "frame_0001.png").exists()

    def test_no_crash_on_empty_frame(self, tmp_path):
        fb = FrameBuffer()
        epd = MockEPD(tmp_path / "frames", fb)
        epd.display_base(b"")
        assert not (tmp_path / "frames" / "frame_0000.png").exists()

    def test_virtual_key_tap(self, tmp_path):
        fb = FrameBuffer()
        epd = MockEPD(tmp_path, fb)
        assert epd.read_keys() == (False, False, False, False)
        epd.press_key(0, hold_cycles=1)
        keys = epd.read_keys()
        assert keys[0] is True
        keys = epd.read_keys()
        assert keys[0] is False

    def test_virtual_key_long_press(self, tmp_path):
        fb = FrameBuffer()
        epd = MockEPD(tmp_path, fb)
        epd.press_key(2, hold_cycles=3)
        assert epd.read_keys()[2] is True
        assert epd.read_keys()[2] is True
        assert epd.read_keys()[2] is True
        assert epd.read_keys()[2] is False

    def test_multiple_keys_simultaneous(self, tmp_path):
        fb = FrameBuffer()
        epd = MockEPD(tmp_path, fb)
        epd.press_key(0, hold_cycles=1)
        epd.press_key(3, hold_cycles=1)
        keys = epd.read_keys()
        assert keys == (True, False, False, True)

    def test_close_is_noop(self, tmp_path):
        fb = FrameBuffer()
        epd = MockEPD(tmp_path, fb)
        epd.close()
