"""EPD display driver and frame management."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Protocol

from PIL import Image

log = logging.getLogger("epd-dash")

# ── Display constants ─────────────────────────────────────────────
W, H = 264, 176
EPD_W, EPD_H = 176, 264
EPD_ROW_BYTES = EPD_W // 8  # 22
BUF_SIZE = EPD_ROW_BYTES * EPD_H  # 5808

# ── GPIO pins (Waveshare 2.7" HAT V2) ────────────────────────────
PIN_RST = 17
PIN_DC = 25
PIN_CS = 8
PIN_BUSY = 24
PIN_KEYS = (5, 6, 13, 19)


# ── Display protocol ─────────────────────────────────────────────
class Display(Protocol):
    def init(self) -> None: ...
    def display_base(self, buf: bytes) -> None: ...
    def display_partial(self, buf: bytes) -> None: ...
    def read_keys(self) -> tuple[bool, bool, bool, bool]: ...
    def close(self) -> None: ...


def pack(img: Image.Image, portrait: bool = False) -> bytes:
    """Pack a rendered image to EPD buffer.

    landscape (default): img is 264×176, rotated 90° to 176×264 for EPD.
    portrait: img is already 176×264, packed directly.
    """
    buf = bytearray(b'\xff' * BUF_SIZE)
    pixels = img.convert("1").load()
    if portrait:
        # Image is already 176×264 — pack row-major, no rotation
        for y in range(EPD_H):
            for x in range(EPD_W):
                if pixels[x, y] == 0:
                    idx = (x + y * EPD_W) // 8
                    buf[idx] &= ~(0x80 >> (x % 8))
    else:
        # Rotate 264×176 landscape → 176×264 portrait
        for y in range(H):
            for x in range(W):
                if pixels[x, y] == 0:
                    newx = y
                    newy = EPD_H - 1 - x
                    idx = (newx + newy * EPD_W) // 8
                    buf[idx] &= ~(0x80 >> (newx % 8))
    return bytes(buf)


class FrameBuffer:
    """Thread-safe latest-frame storage for the HTTP server."""

    __slots__ = ("_png", "_lock")

    def __init__(self) -> None:
        self._png: bytes = b""
        self._lock = threading.Lock()

    @property
    def png(self) -> bytes:
        with self._lock:
            return self._png

    @png.setter
    def png(self, value: bytes) -> None:
        with self._lock:
            self._png = value


# ── EPD hardware driver ───────────────────────────────────────────
class EPD:  # pragma: no cover
    """Minimal driver for Waveshare 2.7" e-Paper V2 (SSD1680)."""

    def __init__(self) -> None:
        import lgpio
        import spidev

        self._lg = lgpio
        self._h = lgpio.gpiochip_open(0)
        lgpio.gpio_claim_output(self._h, PIN_RST, 1)
        lgpio.gpio_claim_output(self._h, PIN_DC, 0)
        lgpio.gpio_claim_input(self._h, PIN_BUSY)
        for pin in PIN_KEYS:
            lgpio.gpio_claim_input(self._h, pin, lgpio.SET_PULL_UP)

        self._spi = spidev.SpiDev()
        self._spi.open(0, 0)
        self._spi.max_speed_hz = 4_000_000
        self._spi.mode = 0b00
        self._prev_buf: bytes | None = None

    def _write(self, pin: int, val: int) -> None:
        self._lg.gpio_write(self._h, pin, val)

    def _read(self, pin: int) -> int:
        return self._lg.gpio_read(self._h, pin)

    def _cmd(self, c: int) -> None:
        self._write(PIN_DC, 0)
        self._spi.writebytes([c])

    def _data(self, d: bytes | list[int]) -> None:
        self._write(PIN_DC, 1)
        if isinstance(d, int):
            d = [d]
        self._spi.writebytes2(list(d) if isinstance(d, bytes) else d)

    def _wait(self, timeout: float = 10.0) -> None:
        t0 = time.monotonic()
        while self._read(PIN_BUSY):
            if time.monotonic() - t0 > timeout:
                log.warning("BUSY timeout")
                break
            time.sleep(0.01)

    def _reset(self) -> None:
        self._write(PIN_RST, 1); time.sleep(0.2)
        self._write(PIN_RST, 0); time.sleep(0.002)
        self._write(PIN_RST, 1); time.sleep(0.2)

    def init(self) -> None:
        self._reset(); self._wait()
        self._cmd(0x12); self._wait()  # SW_RESET
        self._cmd(0x45); self._data([0x00, 0x00, 0x07, 0x01])  # RAM Y 0-263
        self._cmd(0x4F); self._data([0x00, 0x00])  # Y counter
        self._cmd(0x11); self._data([0x03])  # Data entry mode
        log.info("EPD initialized")

    def display_base(self, buf: bytes) -> None:
        self._cmd(0x24); self._data(buf)  # BW RAM
        self._cmd(0x26); self._data(buf)  # RED RAM
        self._cmd(0x22); self._data([0xF7])
        self._cmd(0x20); self._wait()
        self._prev_buf = buf

    def display_partial(self, buf: bytes) -> None:
        if self._prev_buf is None:
            return self.display_base(buf)
        self._reset()
        self._cmd(0x3C); self._data([0x80])  # Border
        self._cmd(0x44); self._data([0x00, EPD_ROW_BYTES - 1])
        self._cmd(0x45); self._data([0x00, 0x00, 0x07, 0x01])
        self._cmd(0x4E); self._data([0x00])
        self._cmd(0x4F); self._data([0x00, 0x00])
        self._cmd(0x24); self._data(buf)
        self._cmd(0x22); self._data([0xFF])
        self._cmd(0x20); self._wait()
        self._prev_buf = buf

    def sleep(self) -> None:
        self._cmd(0x10); self._data([0x01]); time.sleep(0.1)

    def read_keys(self) -> tuple[bool, bool, bool, bool]:
        return tuple(self._read(p) == 0 for p in PIN_KEYS)  # type: ignore[return-value]

    def close(self) -> None:
        self.sleep(); self._spi.close(); self._lg.gpiochip_close(self._h)


# ── Mock display for development ──────────────────────────────────
class MockEPD:
    """Drop-in EPD replacement for development. No hardware needed."""

    def __init__(self, out_dir: Path, frame_buf: FrameBuffer) -> None:
        self._dir = out_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._frame_buf = frame_buf
        self._frame = 0
        self._vk_lock = threading.Lock()
        self._vk_pending: list[tuple[int, int]] = []
        self._vk_active: set[int] = set()
        self._vk_holds: dict[int, int] = {}
        log.info("Mock EPD → %s", self._dir)

    def init(self) -> None: pass

    def _save_frame(self) -> None:
        png = self._frame_buf.png
        if not png:
            return
        (self._dir / f"frame_{self._frame:04d}.png").write_bytes(png)
        (self._dir / "latest.png").write_bytes(png)
        self._frame += 1

    def display_base(self, buf: bytes) -> None: self._save_frame()
    def display_partial(self, buf: bytes) -> None: self._save_frame()

    def press_key(self, key_idx: int, hold_cycles: int = 1) -> None:
        """Queue a virtual key press."""
        with self._vk_lock:
            self._vk_pending.append((key_idx, hold_cycles))

    def read_keys(self) -> tuple[bool, bool, bool, bool]:
        """Advance the virtual key state machine. Called at 20Hz by main loop."""
        with self._vk_lock:
            for key, cycles in self._vk_pending:
                self._vk_active.add(key)
                self._vk_holds[key] = max(self._vk_holds.get(key, 0), cycles)
            self._vk_pending.clear()
            result = tuple(i in self._vk_active for i in range(4))
            for i in list(self._vk_active):
                remaining = self._vk_holds.get(i, 0)
                if remaining <= 1:
                    self._vk_active.discard(i)
                    self._vk_holds.pop(i, None)
                else:
                    self._vk_holds[i] = remaining - 1
        return result  # type: ignore[return-value]

    def close(self) -> None: pass
