"""E-Paper Widget Dashboard — Waveshare 2.7" V2 HAT (264×176) on Raspberry Pi 3B+.

Renders a multi-page widget dashboard to an e-Paper display (or PNG for dev).
Push notifications via HTTP. Browser-based emulator for development.

Usage:
    python -m dashboard                    # auto-detects hardware
    python -m dashboard --mock             # force PNG output
    python -m dashboard --config my.yaml   # custom config

Endpoints:
    GET  /         Emulator UI (if available)
    GET  /status   JSON: {notifications, pages, current_page}
    GET  /frame    Latest rendered frame as PNG
    POST /push     Push notification: {"title": "...", "body": "...", "priority": 3}
    POST /clear    Clear all notifications
    POST /buttons  Virtual buttons (mock mode only): {"keys": [0]} or {"keys": [{"key": 0, "action": "long_press"}]}
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path

import yaml

from dashboard.data import Cache
from dashboard.display import EPD, FrameBuffer, MockEPD, W, H, pack
from dashboard.epd import Canvas
from dashboard.notifications import Notification, Store
from dashboard.widgets import WIDGET_TYPES, NotificationsWidget, Widget, build_toasts

log = logging.getLogger("epd-dash")


# ── Page & config ─────────────────────────────────────────────────
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


def _build_pages(cfg: dict, cache: Cache, store: Store) -> list[Page]:
    pages: list[Page] = []
    for page_cfg in cfg.get("pages", []):
        widgets: list[Widget] = []
        for wcfg in page_cfg.get("widgets", []):
            wtype = wcfg.get("type", "")
            cls = WIDGET_TYPES.get(wtype)
            if cls is None:
                log.warning("Unknown widget type: %s", wtype)
                continue
            w = cls(wcfg, cache)
            if isinstance(w, NotificationsWidget):
                w.store = store
            widgets.append(w)
        pages.append(Page(name=page_cfg.get("name", "Page"), widgets=widgets))
    return pages


# ── Render ────────────────────────────────────────────────────────
def render_page(page: Page, frame_buf: FrameBuffer,
                store: Store | None = None, portrait: bool = False) -> bytes:
    """Render page to packed EPD buffer with optional toast overlay."""
    nodes = [w.layout(narrow=portrait) for w in page.widgets]
    toasts = build_toasts(store) if store and store.items else None
    cw, ch = (H, W) if portrait else (W, H)
    canvas = Canvas(cw, ch, nodes)
    img = canvas.render_with_overlays(toasts) if toasts else canvas.render()
    bio = BytesIO()
    img.save(bio, format='PNG')
    frame_buf.png = bio.getvalue()
    return pack(img, portrait)


# ── HTTP server ───────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def __init__(self, request, client_address, server, *,
                 store: Store, push_event: threading.Event,
                 pages: list[Page], page_index: list[int],
                 epd: EPD | MockEPD | None, frame_buf: FrameBuffer,
                 emulator_html: bytes) -> None:
        self._store = store
        self._push_event = push_event
        self._pages = pages
        self._page_index = page_index
        self._epd = epd
        self._frame_buf = frame_buf
        self._emulator_html = emulator_html
        super().__init__(request, client_address, server)

    def log_message(self, format, *args) -> None:  # noqa: ARG002, A002
        log.debug(format, *args)

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
                "notifications": len(self._store.items),
                "pages": [p.name for p in self._pages],
                "current_page": self._page_index[0],
            })
        elif self.path == "/frame":
            png = self._frame_buf.png
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
        elif self.path == "/" and self._emulator_html:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(self._emulator_html)))
            self.end_headers()
            self.wfile.write(self._emulator_html)
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
                self._store.push(n)
                self._push_event.set()
                log.info("← %s", n.title)
                self._json(200, {"ok": True})
            except (json.JSONDecodeError, KeyError) as e:
                self._json(400, {"error": str(e)})
        elif self.path == "/clear":
            self._store.clear()
            self._push_event.set()
            self._json(200, {"ok": True})
        elif self.path == "/buttons":
            epd = self._epd
            if not isinstance(epd, MockEPD):
                self._json(400, {"error": "buttons only available in mock mode"})
                return
            try:
                data = json.loads(self._read_body())
                keys = data.get("keys", [])
                if not isinstance(keys, list) or not keys:
                    raise ValueError("keys required")
                for entry in keys:
                    if isinstance(entry, int):
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
                            epd.press_key(k, hold_cycles=30)
                        else:
                            raise ValueError(f"unknown action: {action}")
                    else:
                        raise ValueError("each key must be int or {key, action}")
            except (json.JSONDecodeError, ValueError, KeyError) as e:
                self._json(400, {"error": str(e)})
                return
            self._push_event.set()
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})


# ── Main ──────────────────────────────────────────────────────────
def main() -> None:  # pragma: no cover
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

    frame_buf = FrameBuffer()
    push_event = threading.Event()
    cache = Cache(on_update=push_event)
    store = Store(data_dir / "notifications.json")
    epd: EPD | MockEPD = MockEPD(data_dir / "frames", frame_buf) if mock else EPD()

    pages = _build_pages(cfg, cache, store)
    if not pages:
        log.error("No pages configured")
        sys.exit(1)

    log.info("Loaded %d pages: %s", len(pages), ", ".join(p.name for p in pages))

    # Mutable page index shared with HTTP handler (single-element list)
    page_index = [0]

    # Load emulator HTML
    emulator_html = b""
    emulator_path = Path(__file__).parent / "emulator.html"
    if emulator_path.exists():
        emulator_html = emulator_path.read_bytes()
        log.info("Emulator UI at http://localhost:%d/", args.port)

    def handler_factory(request, client_address, server):
        return Handler(
            request, client_address, server,
            store=store, push_event=push_event, pages=pages,
            page_index=page_index, epd=epd, frame_buf=frame_buf,
            emulator_html=emulator_html,
        )

    httpd = ThreadingHTTPServer(("0.0.0.0", args.port), handler_factory)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log.info("Listening on :%d", args.port)

    epd.init()

    portrait = False
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
    buf = render_page(pages[page_index[0]], frame_buf, store)
    epd.display_base(buf)
    last_full = time.monotonic()
    last_render = last_full
    log.info("Dashboard ready — page: %s", pages[page_index[0]].name)

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
            page_index[0] = (page_index[0] + 1) % len(pages)
            log.info("Page: %s", pages[page_index[0]].name)
            need_redraw = True

        # Key4: portrait/landscape (tap) / force full refresh (hold)
        if pressed[3]:
            key4_down_at = time.monotonic()
        if released[3] and key4_down_at is not None:
            if time.monotonic() - key4_down_at >= LONG_PRESS:
                last_full = 0.0
            else:
                portrait = not portrait
                log.info("Layout: %s", "portrait" if portrait else "landscape")
            key4_down_at = None
            need_redraw = True

        # Auto-refresh based on minimum widget interval
        now = time.monotonic()
        min_interval = 60.0
        for w in pages[page_index[0]].widgets:
            min_interval = min(min_interval, w.refresh_interval)
        if now - last_render >= min_interval:
            need_redraw = True

        if not need_redraw:
            continue

        buf = render_page(pages[page_index[0]], frame_buf, store, portrait)
        last_render = time.monotonic()
        if now - last_full >= full_refresh_interval:
            epd.display_base(buf)
            last_full = now
        else:
            epd.display_partial(buf)
