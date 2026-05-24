"""E-Paper Widget Dashboard — Waveshare 2.7" V2 HAT (264×176) on Raspberry Pi 3B+.

Homepage-style configurable widget dashboard for e-paper display.
Define pages and widgets in ~/.dashboard/config.yaml.

Usage:
    dashboard [--port 8080] [--mock] [--data-dir ~/.dashboard]

API:
    curl -X POST http://rpi3:8080/push -H 'Content-Type: application/json' \
         -d '{"title":"Build failed","body":"ci #4821 on main","priority":3}'
    curl http://rpi3:8080/
    curl -X POST http://rpi3:8080/clear
"""
from __future__ import annotations

from io import BytesIO
import argparse
import json
import logging
import os
import signal
import ssl
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError

import yaml
from PIL import Image, ImageDraw
from dashboard.epd import (Canvas, Row, Col, Text, Section, Grid, StatusDot, HeaderBar,
                               Table, Card, KV, Sep, Spacer, ProgressBar, Padded, Icon, font,
                               FONT_SM, FONT_MD, FONT_LG, PAD, Node)

log = logging.getLogger("epd-dash")

# ── Display constants ──────────────────────────────────────────────
W, H = 264, 176
EPD_W, EPD_H = 176, 264
EPD_ROW_BYTES = EPD_W // 8  # 22
BUF_SIZE = EPD_ROW_BYTES * EPD_H  # 5808

# ── GPIO pins (Waveshare 2.7" HAT V2) ─────────────────────────────
PIN_RST = 17
PIN_DC = 25
PIN_CS = 8
PIN_BUSY = 24
PIN_KEYS = (5, 6, 13, 19)  # Key1-Key4

# Reusable SSL context for internal HTTPS (self-signed certs)
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


# ── EPD driver ─────────────────────────────────────────────────────
class EPD:
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


class MockEPD:
    """Drop-in EPD replacement for development. No hardware needed."""

    def __init__(self, out_dir: Path) -> None:
        self._dir = out_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._frame = 0
        self._vk_lock = threading.Lock()
        # Each entry: (key_idx, hold_cycles_remaining)
        # hold=0 means single-cycle tap (press+release in 2 polls)
        self._vk_pending: list[tuple[int, int]] = []
        # Keys currently held active
        self._vk_active: set[int] = set()
        log.info("Mock EPD → %s", self._dir)

    def init(self) -> None: pass

    def _save_frame(self) -> None:
        """Save the latest PNG to disk."""
        with _frame_lock:
            png = _latest_frame_png
        if not png:
            return
        (self._dir / f"frame_{self._frame:04d}.png").write_bytes(png)
        (self._dir / "latest.png").write_bytes(png)
        self._frame += 1

    def display_base(self, buf: bytes) -> None: self._save_frame()
    def display_partial(self, buf: bytes) -> None: self._save_frame()
    def sleep(self) -> None: pass

    def press_key(self, key_idx: int, hold_cycles: int = 1) -> None:
        """Queue a virtual key press.

        hold_cycles=1: short tap (active 1 poll, then released)
        hold_cycles=N: held for N polls at 20Hz, so N=30 ≈ 1.5s long press
        """
        with self._vk_lock:
            self._vk_pending.append((key_idx, hold_cycles))

    def read_keys(self) -> tuple[bool, bool, bool, bool]:
        """Advance the virtual key state machine. Called at 20Hz by main loop."""
        with self._vk_lock:
            # Promote pending presses to active
            for key, cycles in self._vk_pending:
                self._vk_active.add(key)
                # Store remaining cycles; if key already active, extend
                existing = getattr(self, f'_hold_{key}', 0)
                setattr(self, f'_hold_{key}', max(existing, cycles))
            self._vk_pending.clear()
            # Snapshot current active set
            result = tuple(i in self._vk_active for i in range(4))
            # Decrement hold counters, release when expired
            for i in list(self._vk_active):
                remaining = getattr(self, f'_hold_{i}', 0)
                if remaining <= 1:
                    self._vk_active.discard(i)
                    setattr(self, f'_hold_{i}', 0)
                else:
                    setattr(self, f'_hold_{i}', remaining - 1)
        return result  # type: ignore[return-value]

    def close(self) -> None: pass


# ── Rendering helpers ──────────────────────────────────────────────
def _pack(img: Image.Image, invert: bool = False) -> bytes:
    """Rotate 264×176 landscape → 176×264 portrait and pack to 1-bit."""
    buf = bytearray(b'\xff' * BUF_SIZE)
    pixels = img.convert("1").load()
    for y in range(H):
        for x in range(W):
            if pixels[x, y] == 0:
                newx = y
                newy = EPD_H - 1 - x
                idx = (newx + newy * EPD_W) // 8
                buf[idx] &= ~(0x80 >> (newx % 8))
    if invert:
        for i in range(BUF_SIZE):
            buf[i] ^= 0xFF
    return bytes(buf)

# ── HTTP fetch helper ──────────────────────────────────────────────
def _fetch_json(url: str, timeout: float = 5.0, headers: dict[str, str] | None = None,
                post_body: bytes | None = None) -> object | None:
    """Fetch JSON from a URL. Returns None on any error."""
    try:
        req = Request(url, data=post_body)
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)
        ctx = _ssl_ctx if url.startswith("https") else None
        with urlopen(req, timeout=timeout, context=ctx) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.debug("Fetch %s failed: %s", url, e)
        return None


def _fetch_grafana_alerts(grafana_url: str) -> int:
    """Return count of active Grafana alerts."""
    data = _fetch_json(f"{grafana_url}/api/alertmanager/grafana/api/v2/alerts")
    if isinstance(data, list):
        return sum(1 for a in data if a.get("status", {}).get("state") == "active")
    return 0


def _prom_query(grafana_url: str, expr: str) -> list[dict] | None:
    """Run an instant Prometheus query via Grafana proxy. Returns list of {labels, value}."""
    payload = {
        "queries": [{
            "refId": "A",
            "datasourceId": 1,
            "expr": expr,
            "instant": True,
        }],
    }
    data = _fetch_json(
        f"{grafana_url}/api/ds/query",
        timeout=8.0,
        headers={"Content-Type": "application/json"},
        post_body=json.dumps(payload).encode(),
    )
    if not data or not isinstance(data, dict):
        return None
    result = data.get("results", {}).get("A", {})
    if result.get("status") != 200:
        return None
    out: list[dict] = []
    for frame in result.get("frames", []):
        fields = frame.get("schema", {}).get("fields", [])
        values = frame.get("data", {}).get("values", [])
        if len(fields) < 2 or len(values) < 2:
            continue
        labels = fields[1].get("labels", {})
        val = values[1][0] if values[1] else None
        out.append({"labels": labels, "value": val})
    return out

# ── Data cache ─────────────────────────────────────────────────────
class Cache:
    """Thread-safe cache with async background refresh.

    Never blocks the caller on a network fetch. Returns stale data
    immediately and kicks off a background thread to refresh.
    First request for a key returns None (no data yet).
    """

    def __init__(self, on_update: threading.Event | None = None) -> None:
        self._data: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()
        self._pending: set[str] = set()
        self._on_update = on_update

    def get(self, key: str, ttl: float, fetch: callable) -> object | None:
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if entry is not None:
                ts, val = entry
                if now - ts < ttl:
                    return val  # fresh
                # Stale — schedule refresh, return stale data
                self._schedule(key, fetch)
                return val
            # No data at all — schedule fetch, return None
            self._schedule(key, fetch)
            return None

    def _schedule(self, key: str, fetch: callable) -> None:
        """Start a background fetch if one isn't already running for this key."""
        if key in self._pending:
            return
        self._pending.add(key)
        threading.Thread(target=self._do_fetch, args=(key, fetch), daemon=True).start()

    def _do_fetch(self, key: str, fetch: callable) -> None:
        try:
            val = fetch()
            with self._lock:
                self._data[key] = (time.monotonic(), val)
            if self._on_update:
                self._on_update.set()
        except Exception as e:
            log.debug("Cache fetch %s failed: %s", key, e)
        finally:
            with self._lock:
                self._pending.discard(key)

    def invalidate(self, prefix: str = "") -> None:
        with self._lock:
            if not prefix:
                self._data.clear()
            else:
                self._data = {k: v for k, v in self._data.items() if not k.startswith(prefix)}


_cache = Cache()


# ── Notification store ─────────────────────────────────────────────
@dataclass
class Notification:
    title: str
    body: str = ""
    priority: int = 3
    ts: float = field(default_factory=time.time)
    read: bool = False


class Store:
    def __init__(self, path: Path, max_items: int = 50) -> None:
        self._path = path
        self._max = max_items
        self._lock = threading.Lock()
        self.items: list[Notification] = []
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self.items = [Notification(**n) for n in json.loads(self._path.read_text())]
            except Exception:
                self.items = []

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps([asdict(n) for n in self.items]))

    def push(self, n: Notification) -> None:
        with self._lock:
            self.items.insert(0, n)
            if len(self.items) > self._max:
                self.items = self.items[:self._max]
            self._save()

    def clear(self) -> None:
        with self._lock:
            self.items.clear()
            self._save()

    def dismiss(self, idx: int) -> None:
        with self._lock:
            if 0 <= idx < len(self.items):
                self.items.pop(idx)
                self._save()


# ── Widgets ────────────────────────────────────────────────────────
class Widget:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg

    def layout(self) -> Node:
        return Spacer(h=0)

    @property
    def refresh_interval(self) -> float:
        return self.cfg.get('interval', 60.0)


class ClockWidget(Widget):
    _PT_DAYS = ['Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sáb', 'Dom']
    _PT_MONTHS = ['Jan', 'Fev', 'Mar', 'Abr', 'Mai', 'Jun',
                  'Jul', 'Ago', 'Set', 'Out', 'Nov', 'Dez']

    def _fetch_weather(self) -> dict | None:
        lat = self.cfg.get('latitude')
        lon = self.cfg.get('longitude')
        if lat is None or lon is None:
            return None
        return _cache.get(
            f'weather:{lat}:{lon}', self.cfg.get('weather_interval', 600),
            lambda: _fetch_json(
                f'https://api.open-meteo.com/v1/forecast?'
                f'latitude={lat}&longitude={lon}'
                f'&current=temperature_2m,weather_code'
                f'&timezone=auto'
            ),
        )

    def layout(self) -> Node:
        now = datetime.now()
        left = f'{Icon.CLOCK} {now.strftime("%H:%M")}'
        date_str = f'{self._PT_DAYS[now.weekday()]} {now.day} {self._PT_MONTHS[now.month - 1]}'
        center = ''
        data = self._fetch_weather()
        if data and 'current' in data:
            cur = data['current']
            icon = Icon.wmo(cur.get('weather_code', -1))
            temp = cur.get('temperature_2m', '?')
            center = f'{icon} {temp:.0f}°C' if isinstance(temp, (int, float)) else f'{icon} {temp}°C'
        return HeaderBar(left=left, center=center, right=date_str)


class WeatherWidget(Widget):
    def layout(self) -> Node:
        lat = self.cfg.get('latitude', 41.54)
        lon = self.cfg.get('longitude', -8.41)
        label = self.cfg.get('label', 'Meteorologia')
        data = _cache.get(
            f'weather:{lat}:{lon}', self.cfg.get('interval', 600),
            lambda: _fetch_json(
                f'https://api.open-meteo.com/v1/forecast?'
                f'latitude={lat}&longitude={lon}'
                f'&current=temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m'
                f'&daily=temperature_2m_max,temperature_2m_min,weather_code'
                f'&timezone=auto&forecast_days=3'
            ),
        )
        children: list[Node] = []
        if not data or 'current' not in data:
            children.append(Text('Sem dados', size=FONT_MD))
        else:
            cur = data['current']
            temp = cur.get('temperature_2m', '?')
            humidity = cur.get('relative_humidity_2m', '?')
            wind = cur.get('wind_speed_10m', '?')
            icon = Icon.wmo(cur.get('weather_code', -1))
            children.append(Row([
                Text(f'{icon} {temp}°C', size=FONT_LG, bold=True),
                Text(f'{Icon.W_HUMID}{humidity}% {Icon.W_WIND}{wind}km/h', size=FONT_MD, align='right'),
            ]))
            daily = data.get('daily', {})
            highs = daily.get('temperature_2m_max', [])
            lows = daily.get('temperature_2m_min', [])
            codes = daily.get('weather_code', [])
            for i in range(min(3, len(highs))):
                day_label = ['Hoje', 'Amanhã'][i] if i < 2 else daily.get('time', ['', '', ''])[i][5:]
                ic = Icon.wmo(codes[i]) if i < len(codes) else '?'
                children.append(Text(f'{day_label}: {ic} {lows[i]:.0f}-{highs[i]:.0f}°C', size=FONT_MD))
        return Section(label, icon=Icon.SUN, children=children)


class SystemWidget(Widget):
    def layout(self) -> Node:
        cpu_pct = _cpu_usage()
        temp = _cpu_temp()
        mu, mt, _ = _mem_info()
        du, dt, _ = _disk_info(self.cfg.get('mount', '/'))
        up = _uptime()
        return Section('Sistema', icon=Icon.MONITOR, children=[
            Row([
                Text(f'{Icon.CPU} {cpu_pct} {temp}', size=FONT_SM),
                Text(f'{Icon.RAM} {mu}/{mt}', size=FONT_SM),
            ]),
            Row([
                Text(f'{Icon.DISK} {du}/{dt}', size=FONT_SM),
                Text(f'{Icon.CLOCK} {up}', size=FONT_SM),
            ]),
        ])


class ServicesWidget(Widget):
    def layout(self) -> Node:
        label = self.cfg.get('label', 'Serviços')
        items = self.cfg.get('items', [])
        if not items:
            return Section(label, icon=Icon.SERVER, children=[
                Text('Sem serviços', size=FONT_SM),
            ])
        cols = self.cfg.get('columns', 3)
        dots: list[Node] = []
        for svc in items:
            name = svc.get('name', '?')
            url = svc.get('url', '')
            status = _cache.get(
                f'svc:{url}', self.cfg.get('interval', 120),
                lambda u=url: _check_service(u),
            )
            dots.append(StatusDot(name, up=status))
        return Section(label, icon=Icon.SERVER, children=[
            Grid(cols=cols, row_h=14, items=dots),
        ])


class NotificationsWidget(Widget):
    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self.scroll = 0
        self.store: Store | None = None

    def layout(self) -> Node:
        children: list[Node] = []
        if not self.store or not self.store.items:
            children.append(Text('Sem notificações', size=FONT_MD))
        else:
            for n in self.store.items[self.scroll:]:
                marker = Icon.WARNING if n.priority >= 4 else Icon.CHEVRON
                children.append(Row([
                    Text(f'{marker} {n.title}', size=FONT_MD),
                    Text(_ago(n.ts), size=FONT_MD, align='right'),
                ]))
                if n.body:
                    body = n.body if len(n.body) <= 34 else n.body[:32] + '…'
                    children.append(Text(f'  {body}', size=FONT_SM))
        return Section('Notificações', icon=Icon.BELL, children=children)


class NowPlayingWidget(Widget):
    def layout(self) -> Node:
        url = self.cfg.get('url', '')
        key = self.cfg.get('api_key', '')
        if not url or not key:
            return Section('A reproduzir', icon=Icon.PLAY, children=[
                Text('Tautulli não configurado', size=FONT_SM)])
        data = _cache.get(
            f'tautulli:{url}', self.cfg.get('interval', 30),
            lambda: _fetch_json(f'{url}/api/v2?apikey={key}&cmd=get_activity'),
        )
        sessions = []
        if data and isinstance(data, dict):
            resp = data.get('response', {}).get('data', {})
            sessions = resp.get('sessions', [])
        children: list[Node] = []
        if not sessions:
            children.append(Text('Nada a reproduzir', size=FONT_SM))
        else:
            for s in sessions[:3]:
                title = s.get('title', '?')
                user = s.get('friendly_name', '?')
                state = s.get('state', '?')
                player = s.get('player', '?')
                icon = Icon.PLAY if state == 'playing' else Icon.PAUSE if state == 'paused' else Icon.STOP
                children.append(Text(f'{icon} {title}', size=FONT_SM))
                children.append(Text(f'  {user} • {player}', size=FONT_SM))
        return Section('A reproduzir', icon=Icon.PLAY, children=children)


class SonarrWidget(Widget):
    def layout(self) -> Node:
        url = self.cfg.get('url', '')
        key = self.cfg.get('api_key', '')
        if not url or not key:
            return Section('Séries', icon=Icon.FILM, children=[
                Text('Sonarr não configurado', size=FONT_SM)])
        today = datetime.now().strftime('%Y-%m-%d')
        data = _cache.get(
            f'sonarr:{url}', self.cfg.get('interval', 900),
            lambda: _fetch_json(
                f'{url}/api/v3/calendar?start={today}&end=&includeSeries=true&includeEpisodeFile=false',
                headers={'X-Api-Key': key},
            ),
        )
        children: list[Node] = []
        if not data or not isinstance(data, list):
            children.append(Text('Sem dados', size=FONT_SM))
        elif not data:
            children.append(Text('Nada previsto', size=FONT_SM))
        else:
            for ep in data[:4]:
                series = ep.get('series', {}).get('title', '?')
                s = ep.get('seasonNumber', 0)
                e = ep.get('episodeNumber', 0)
                line = f'• {series} S{s:02d}E{e:02d}'
                if len(line) > 36:
                    line = line[:34] + '…'
                children.append(Text(line, size=FONT_SM))
        return Section('Séries', icon=Icon.FILM, children=children)


class RadarrWidget(Widget):
    def layout(self) -> Node:
        url = self.cfg.get('url', '')
        key = self.cfg.get('api_key', '')
        if not url or not key:
            return Section('Filmes', icon=Icon.FILM, children=[
                Text('Radarr não configurado', size=FONT_SM)])
        today = datetime.now().strftime('%Y-%m-%d')
        data = _cache.get(
            f'radarr:{url}', self.cfg.get('interval', 900),
            lambda: _fetch_json(
                f'{url}/api/v3/calendar?start={today}',
                headers={'X-Api-Key': key},
            ),
        )
        children: list[Node] = []
        if not data or not isinstance(data, list):
            children.append(Text('Sem dados', size=FONT_SM))
        elif not data:
            children.append(Text('Nada previsto', size=FONT_SM))
        else:
            for movie in data[:4]:
                title = movie.get('title', '?')
                year = movie.get('year', '')
                line = f'• {title} ({year})' if year else f'• {title}'
                if len(line) > 36:
                    line = line[:34] + '…'
                children.append(Text(line, size=FONT_SM))
        return Section('Filmes', icon=Icon.FILM, children=children)


class ProxmoxWidget(Widget):
    def layout(self) -> Node:
        url = self.cfg.get('url', '')
        node = self.cfg.get('node', '')
        user = self.cfg.get('username', '')
        token_name = self.cfg.get('token_name', '')
        token_value = self.cfg.get('token_value', '')
        label = self.cfg.get('label', 'Proxmox')
        if not url or not node:
            return Section(label, children=[Text('Não configurado', size=FONT_SM)])
        headers = {}
        if user and token_name and token_value:
            headers['Authorization'] = f'PVEAPIToken={user}!{token_name}={token_value}'
        data = _cache.get(
            f'pve:{url}:{node}', self.cfg.get('interval', 120),
            lambda: _fetch_json(f'{url}/api2/json/nodes/{node}/status', headers=headers),
        )
        if not data or not isinstance(data, dict):
            return Section(label, children=[Text('Sem dados', size=FONT_SM)])
        d = data.get('data', data)
        cpu = d.get('cpu', 0)
        mem = d.get('memory', {})
        mem_used = mem.get('used', 0)
        mem_total = mem.get('total', 1)
        uptime_s = d.get('uptime', 0)
        mp = 100 * mem_used / mem_total if mem_total else 0
        days = uptime_s // 86400
        hours = (uptime_s % 86400) // 3600
        return Section(label, children=[
            KV(f'{Icon.CPU} CPU', f'{cpu*100:.0f}%'),
            ProgressBar(pct=cpu*100),
            KV(f'{Icon.RAM} RAM', f'{mem_used//(1<<30):.1f}/{mem_total//(1<<30):.1f}G ({mp:.0f}%)'),
            ProgressBar(pct=mp),
            Text(f'{Icon.CLOCK} Up: {days}d {hours}h', size=FONT_SM),
        ])


class HomeAssistantWidget(Widget):
    def layout(self) -> Node:
        url = self.cfg.get('url', '')
        token = self.cfg.get('token', '')
        entities = self.cfg.get('entities', [])
        label = self.cfg.get('label', 'Home')
        if not url or not token or not entities:
            return Section(label, children=[Text('Não configurado', size=FONT_SM)])
        headers_dict = {'Authorization': f'Bearer {token}'}
        children: list[Node] = []
        for eid in entities:
            data = _cache.get(
                f'ha:{eid}', self.cfg.get('interval', 60),
                lambda e=eid: _fetch_json(f'{url}/api/states/{e}', headers=headers_dict),
            )
            if data and isinstance(data, dict):
                name = data.get('attributes', {}).get('friendly_name', eid)
                state = data.get('state', '?')
                unit = data.get('attributes', {}).get('unit_of_measurement', '')
                children.append(Text(f'{name}: {state}{unit}', size=FONT_SM))
            else:
                children.append(Text(f'{eid}: ?', size=FONT_SM))
        return Section(label, children=children)


class ClusterWidget(Widget):
    def _query(self, expr: str) -> list[dict] | None:
        url = self.cfg.get('grafana_url', '')
        if not url:
            return None
        return _cache.get(
            f'prom:{expr}', self.cfg.get('interval', 120),
            lambda: _prom_query(url, expr),
        )

    def layout(self) -> Node:
        grafana_url = self.cfg.get('grafana_url', '')
        badge = ''
        if grafana_url:
            alert_count = _cache.get(
                f'grafana_alerts:{grafana_url}', 120,
                lambda: _fetch_grafana_alerts(grafana_url),
            )
            if alert_count:
                badge = f'{Icon.WARNING} {alert_count}'

        cpu_data = self._query('instance:node_cpu_utilisation:rate5m{job="node-exporter"}')
        ram_data = self._query(
            '1 - (node_memory_MemAvailable_bytes{job="node-exporter"}'
            ' / node_memory_MemTotal_bytes{job="node-exporter"})'
        )
        disk_data = self._query(
            '1 - (node_filesystem_avail_bytes{job="node-exporter",mountpoint="/",fstype!="tmpfs"}'
            ' / node_filesystem_size_bytes{job="node-exporter",mountpoint="/",fstype!="tmpfs"})'
        )

        nodes: dict[str, dict[str, float]] = {}
        for metric, key in [(cpu_data, 'cpu'), (ram_data, 'ram'), (disk_data, 'disk')]:
            if metric:
                for item in metric:
                    inst = item['labels'].get('instance', '?')
                    node = item['labels'].get('node', inst.split(':')[0])
                    nodes.setdefault(node, {})[key] = (item['value'] or 0) * 100

        if not nodes:
            return Section('Cluster', icon=Icon.SERVER, badge=badge, children=[
                Text('Prometheus indisponível', size=FONT_SM),
            ])

        rows: list[list[str]] = []
        for node_name in sorted(nodes):
            stats = nodes[node_name]
            short = node_name.split('.')[0][:10]
            cpu = f"{stats['cpu']:.0f}%" if 'cpu' in stats else '?'
            ram = f"{stats['ram']:.0f}%" if 'ram' in stats else '?'
            disk = f"{stats['disk']:.0f}%" if 'disk' in stats else '?'
            rows.append([short, cpu, ram, disk])

        return Section('Cluster', icon=Icon.SERVER, badge=badge, children=[
            Table(
                columns=[('', 0), ('CPU', 76), ('RAM', 136), ('Disco', 196)],
                rows=rows,
            ),
        ])

# ── Widget registry ───────────────────────────────────────────────
WIDGET_TYPES: dict[str, type[Widget]] = {
    "clock": ClockWidget,
    "weather": WeatherWidget,
    "system": SystemWidget,
    "services": ServicesWidget,
    "notifications": NotificationsWidget,
    "now_playing": NowPlayingWidget,
    "sonarr": SonarrWidget,
    "radarr": RadarrWidget,
    "proxmox": ProxmoxWidget,
    "home_assistant": HomeAssistantWidget,
    "cluster": ClusterWidget,
}


# ── System stat helpers ────────────────────────────────────────────
def _read_stat(path: str, default: str = "?") -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def _cpu_temp() -> str:
    raw = _read_stat("/sys/class/thermal/thermal_zone0/temp", "0")
    return f"{int(raw) / 1000:.1f}°C"


def _cpu_usage() -> str:
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()
        idle = int(parts[4])
        total = sum(int(p) for p in parts[1:])
        prev = getattr(_cpu_usage, "_prev", None)
        _cpu_usage._prev = (idle, total)  # type: ignore[attr-defined]
        if prev is None:
            return "…"
        d_idle = idle - prev[0]
        d_total = total - prev[1]
        if d_total == 0:
            return "0%"
        return f"{100 * (1 - d_idle / d_total):.0f}%"
    except OSError:
        return "?"


def _mem_info() -> tuple[str, str, str]:
    try:
        info: dict[str, int] = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                info[parts[0].rstrip(":")] = int(parts[1])
        total = info["MemTotal"]
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        used = total - avail
        pct = f"{100 * used / total:.0f}%" if total else "?"
        return f"{used // 1024}M", f"{total // 1024}M", pct
    except OSError:
        return "?", "?", "?"


def _disk_info(mount: str = "/") -> tuple[str, str, str]:
    try:
        st = os.statvfs(mount)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - free
        pct = f"{100 * used / total:.0f}%" if total else "?"
        return f"{used // (1 << 30):.1f}G", f"{total // (1 << 30):.1f}G", pct
    except (OSError, AttributeError):
        return "?", "?", "?"


def _uptime() -> str:
    raw = _read_stat("/proc/uptime", "0")
    secs = int(float(raw.split()[0]))
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def _load_avg() -> str:
    raw = _read_stat("/proc/loadavg", "0 0 0")
    parts = raw.split()
    return f"{parts[0]} {parts[1]} {parts[2]}"


def _ago(ts: float) -> str:
    d = time.time() - ts
    if d < 60: return "agora"
    if d < 3600: return f"{int(d / 60)}m"
    if d < 86400: return f"{int(d / 3600)}h"
    return f"{int(d / 86400)}d"


_SERVICE_DOWN_CODES = {502, 503, 504}


def _check_service(url: str) -> bool:
    """Check if a service is reachable. 502/503/504 and connection failures = down."""
    ctx = _ssl_ctx if url.startswith("https") else None
    for method in ("HEAD", "GET"):
        try:
            req = Request(url, method=method)
            with urlopen(req, timeout=5, context=ctx) as resp:
                return resp.status not in _SERVICE_DOWN_CODES
        except Exception as e:
            if hasattr(e, 'code'):
                return e.code not in _SERVICE_DOWN_CODES
            if method == "HEAD":
                continue  # retry with GET
            return False
    return False


# ── Page & config ──────────────────────────────────────────────────
@dataclass
class Page:
    name: str
    widgets: list[Widget]


DEFAULT_CONFIG = {
    "pages": [
        {
            "name": "Dashboard",
            "widgets": [
                {"type": "clock", "latitude": 41.54, "longitude": -8.41},
                {"type": "system"},
                {
                    "type": "services",
                    "items": [
                        {"name": "Plex", "url": "https://plex.tiagomartins.dev"},
                        {"name": "HA", "url": "https://home.tiagomartins.dev"},
                        {"name": "Sonarr", "url": "https://sonarr.tiagomartins.dev"},
                        {"name": "Radarr", "url": "https://radarr.tiagomartins.dev"},
                        {"name": "SABnzbd", "url": "https://sabnzbd.tiagomartins.dev"},
                        {"name": "Prowlarr", "url": "https://prowlarr.k8s.zoca.lol"},
                        {"name": "NAS", "url": "https://nas.tiagomartins.dev"},
                        {"name": "PVE Mand.", "url": "https://mandalore.terrier-universe.ts.net"},
                        {"name": "PVE Jelly.", "url": "https://proxmox.tiagomartins.dev"},
                        {"name": "UDM", "url": "https://192.168.0.1"},
                    ],
                },
            ],
        },
        {
            "name": "Cluster",
            "widgets": [
                {"type": "clock", "latitude": 41.54, "longitude": -8.41},
                {"type": "cluster", "grafana_url": "https://grafana.k8s.zoca.lol"},
            ],
        },
    ],
}


def _load_config(path: Path) -> dict:
    if path.exists():
        try:
            cfg = yaml.safe_load(path.read_text())
            if isinstance(cfg, dict) and "pages" in cfg:
                return cfg
            log.warning("Invalid config, using defaults")
        except Exception as e:
            log.warning("Config parse error: %s, using defaults", e)
    return DEFAULT_CONFIG


def _build_pages(cfg: dict, store: Store) -> list[Page]:
    pages: list[Page] = []
    for page_cfg in cfg.get("pages", []):
        widgets: list[Widget] = []
        for wcfg in page_cfg.get("widgets", []):
            wtype = wcfg.get("type", "")
            cls = WIDGET_TYPES.get(wtype)
            if cls is None:
                log.warning("Unknown widget type: %s", wtype)
                continue
            w = cls(wcfg)
            if isinstance(w, NotificationsWidget):
                w.store = store
            widgets.append(w)
        pages.append(Page(name=page_cfg.get("name", "Page"), widgets=widgets))
    return pages


# ── Render a page ──────────────────────────────────────────────────
_latest_frame_png: bytes = b""
_frame_lock = threading.Lock()


def _build_toasts(store: Store) -> list[Card]:
    """Build toast Card nodes from notifications."""
    toasts: list[Card] = []
    for n in store.items[:3]:
        marker = Icon.WARNING if n.priority >= 4 else Icon.BELL
        children: list[Node] = [
            Row([
                Text(f'{marker} {n.title}', size=FONT_MD, bold=True),
                Text(_ago(n.ts), size=FONT_SM, align='right'),
            ]),
        ]
        if n.body:
            body = n.body if len(n.body) <= 32 else n.body[:30] + '…'
            children.append(Text(f'  {body}', size=FONT_SM))
        toasts.append(Card(children=children))
    return toasts


def render_page(page: Page, store: Store | None = None, invert: bool = False) -> bytes:
    """Render page to packed EPD buffer with optional toast overlay."""
    global _latest_frame_png
    nodes: list[Node] = [w.layout() for w in page.widgets]
    toasts = _build_toasts(store) if store and store.items else None
    canvas = Canvas(W, H, nodes)
    img = canvas.render_with_overlays(toasts) if toasts else canvas.render()
    # Cache PNG for /frame endpoint
    bio = BytesIO()
    img.save(bio, format='PNG')
    with _frame_lock:
        _latest_frame_png = bio.getvalue()
    return _pack(img, invert)


# ── HTTP server ────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    store: Store
    on_push: threading.Event
    pages: list[Page]
    current_page: int
    epd: EPD | MockEPD | None = None
    emulator_html: bytes = b""

    def log_message(self, fmt, *args) -> None:  # noqa: ARG002
        log.debug(fmt, *args)

    def _json(self, code: int, obj: object) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length)

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:
        if self.path == "/status":
            self._json(200, {
                "notifications": len(self.store.items),
                "pages": [p.name for p in self.pages],
                "current_page": self.current_page,
            })
        elif self.path == "/frame":
            with _frame_lock:
                png = _latest_frame_png
            if not png:
                self.send_response(204)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(png)
        elif self.path == "/" and self.emulator_html:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(self.emulator_html)))
            self.end_headers()
            self.wfile.write(self.emulator_html)
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path == "/push":
            try:
                data = json.loads(self._read_body())
                n = Notification(
                    title=data["title"],
                    body=data.get("body", ""),
                    priority=data.get("priority", 3),
                )
                self.store.push(n)
                self.on_push.set()
                log.info("← %s", n.title)
                self._json(200, {"ok": True})
            except (json.JSONDecodeError, KeyError) as e:
                self._json(400, {"error": str(e)})
        elif self.path == "/clear":
            self.store.clear()
            self.on_push.set()
            self._json(200, {"ok": True})
        elif self.path == "/buttons":
            epd = self.epd
            if not (epd and isinstance(epd, MockEPD)):
                self._json(400, {"error": "buttons only available in mock mode"})
                return
            try:
                data = json.loads(self._read_body())
                keys = data.get("keys", [])
                if not isinstance(keys, list) or not keys:
                    raise ValueError("keys required")
                for entry in keys:
                    if isinstance(entry, int):
                        # Legacy: bare int = short press
                        if not (0 <= entry <= 3):
                            raise ValueError(f"key {entry} out of range 0-3")
                        epd.press_key(entry, hold_cycles=1)
                    elif isinstance(entry, dict):
                        k = entry["key"]
                        action = entry.get("action", "press")
                        if not (isinstance(k, int) and 0 <= k <= 3):
                            raise ValueError(f"key {k} out of range 0-3")
                        if action == "press":
                            epd.press_key(k, hold_cycles=1)
                        elif action == "long_press":
                            epd.press_key(k, hold_cycles=30)  # ~1.5s at 20Hz
                        else:
                            raise ValueError(f"unknown action: {action}")
                    else:
                        raise ValueError("each key must be int or {key, action}")
            except (json.JSONDecodeError, ValueError, KeyError) as e:
                self._json(400, {"error": str(e)})
                return
            self.on_push.set()
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})


# ── Main ───────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="E-Paper Widget Dashboard")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--mock", action="store_true", help="Save PNGs instead of driving display")
    parser.add_argument("--data-dir", type=Path, default=Path.home() / ".dashboard")
    parser.add_argument("--config", type=Path, default=None, help="Config YAML path")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    data_dir: Path = args.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    config_path = args.config or data_dir / "config.yaml"
    cfg = _load_config(config_path)

    # Write default config if none exists
    if not config_path.exists():
        config_path.write_text(yaml.dump(DEFAULT_CONFIG, default_flow_style=False, sort_keys=False))
        log.info("Wrote default config to %s", config_path)

    # Auto-mock when hardware unavailable
    mock = args.mock
    if not mock:
        try:
            import lgpio; import spidev  # noqa: F401, E702
        except ImportError:
            log.warning("No SPI/GPIO libs — falling back to mock mode")
            mock = True

    epd: EPD | MockEPD = MockEPD(data_dir / "frames") if mock else EPD()
    store = Store(data_dir / "notifications.json")
    push_event = threading.Event()
    _cache._on_update = push_event  # wake main loop when data arrives

    pages = _build_pages(cfg, store)
    if not pages:
        log.error("No pages configured")
        sys.exit(1)

    log.info("Loaded %d pages: %s", len(pages), ", ".join(p.name for p in pages))

    # HTTP server
    Handler.store = store
    Handler.on_push = push_event
    Handler.pages = pages
    Handler.current_page = 0
    Handler.epd = epd
    # Load emulator HTML if present next to this script
    emulator_path = Path(__file__).parent / "emulator.html"
    if emulator_path.exists():
        Handler.emulator_html = emulator_path.read_bytes()
        log.info("Emulator UI at http://localhost:%d/", args.port)
    httpd = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log.info("Listening on :%d", args.port)

    epd.init()

    page_idx = 0
    dark_mode = False
    key4_down_at: float | None = None
    key1_down_at: float | None = None
    LONG_PRESS = 1.0
    full_refresh_interval = 30 * 60
    last_full = 0.0
    prev_keys = (False, False, False, False)

    def shutdown(*_: object) -> None:
        log.info("Shutting down")
        httpd.shutdown()
        epd.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Initial full refresh
    buf = render_page(pages[page_idx], store)
    epd.display_base(buf)
    last_full = time.monotonic()
    last_render = last_full
    log.info("Dashboard ready — page: %s", pages[page_idx].name)

    while True:
        push_event.wait(timeout=0.05)
        pushed = push_event.is_set()
        push_event.clear()

        keys = epd.read_keys()
        pressed = tuple(k and not pk for k, pk in zip(keys, prev_keys))
        released = tuple(pk and not k for k, pk in zip(keys, prev_keys))
        prev_keys = keys

        need_redraw = pushed

        # Key1: dismiss notification (tap=top, long=all)
        if pressed[0]:
            key1_down_at = time.monotonic()
        if released[0] and key1_down_at is not None:
            if store.items:
                if time.monotonic() - key1_down_at >= LONG_PRESS:
                    store.clear()
                    log.info("Cleared all notifications")
                else:
                    store.dismiss(0)
                    log.info("Dismissed top notification")
                need_redraw = True
            key1_down_at = None

        # Key2: next page
        if pressed[1]:
            page_idx = (page_idx + 1) % len(pages)
            Handler.current_page = page_idx
            log.info("Page: %s", pages[page_idx].name)
            need_redraw = True

        # Key4: dark mode (tap) / force full refresh (hold)
        if pressed[3]:
            key4_down_at = time.monotonic()
        if released[3] and key4_down_at is not None:
            if time.monotonic() - key4_down_at >= LONG_PRESS:
                last_full = 0.0
            else:
                dark_mode = not dark_mode
            key4_down_at = None
            need_redraw = True

        # Auto-refresh based on minimum widget interval
        now = time.monotonic()
        min_interval = 60.0
        for w in pages[page_idx].widgets:
            min_interval = min(min_interval, w.refresh_interval)
        if now - last_render >= min_interval:
            need_redraw = True

        if not need_redraw:
            continue

        buf = render_page(pages[page_idx], store, dark_mode)
        last_render = time.monotonic()
        if now - last_full >= full_refresh_interval:
            epd.display_base(buf)
            last_full = now
        else:
            epd.display_partial(buf)

