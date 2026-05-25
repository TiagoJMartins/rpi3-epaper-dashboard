"""Tests for dashboard.dashboard — HTTP handler, render pipeline, config."""
from __future__ import annotations

import json
import threading
import time
from http.server import ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

import pytest
import yaml

from dashboard.dashboard import (
    DEFAULT_CONFIG, Handler, Page, _build_pages, _load_config, render_page,
)
from dashboard.data import Cache
from dashboard.display import EPD, FrameBuffer, MockEPD, W, H
from dashboard.notifications import Notification, Store
from dashboard.widgets import Widget, ClockWidget


# ── Config ────────────────────────────────────────────────────────
class TestLoadConfig:
    def test_loads_valid_yaml(self, tmp_path):
        cfg = {"pages": [{"name": "Test", "widgets": [{"type": "clock"}]}]}
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump(cfg))
        result = _load_config(path)
        assert result["pages"][0]["name"] == "Test"

    def test_falls_back_on_missing(self, tmp_path):
        result = _load_config(tmp_path / "nope.yaml")
        assert result == DEFAULT_CONFIG

    def test_falls_back_on_invalid(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text("not: valid: yaml: [")
        result = _load_config(path)
        assert result == DEFAULT_CONFIG

    def test_falls_back_on_no_pages_key(self, tmp_path):
        path = tmp_path / "nopages.yaml"
        path.write_text(yaml.dump({"widgets": []}))
        result = _load_config(path)
        assert result == DEFAULT_CONFIG


class TestBuildPages:
    def test_builds_pages_from_config(self, tmp_path):
        cache = Cache()
        store = Store(tmp_path / "n.json")
        cfg = {
            "pages": [
                {"name": "P1", "widgets": [{"type": "clock"}]},
                {"name": "P2", "widgets": [{"type": "system"}]},
            ],
        }
        pages = _build_pages(cfg, cache, store)
        assert len(pages) == 2
        assert pages[0].name == "P1"
        assert len(pages[0].widgets) == 1

    def test_skips_unknown_widget(self, tmp_path):
        cache = Cache()
        store = Store(tmp_path / "n.json")
        cfg = {"pages": [{"name": "P", "widgets": [{"type": "nonexistent"}]}]}
        pages = _build_pages(cfg, cache, store)
        assert len(pages[0].widgets) == 0

    def test_wires_notification_store(self, tmp_path):
        cache = Cache()
        store = Store(tmp_path / "n.json")
        cfg = {"pages": [{"name": "P", "widgets": [{"type": "notifications"}]}]}
        pages = _build_pages(cfg, cache, store)
        from dashboard.widgets import NotificationsWidget
        w = pages[0].widgets[0]
        assert isinstance(w, NotificationsWidget)
        assert w.store is store


# ── Render pipeline ───────────────────────────────────────────────
class TestRenderPage:
    def test_produces_epd_buffer(self, tmp_path):
        cache = Cache()
        fb = FrameBuffer()
        store = Store(tmp_path / "n.json")
        w = ClockWidget({}, cache)
        page = Page(name="Test", widgets=[w])
        buf = render_page(page, fb, store)
        from dashboard.display import BUF_SIZE
        assert len(buf) == BUF_SIZE

    def test_updates_frame_buffer(self, tmp_path):
        cache = Cache()
        fb = FrameBuffer()
        w = ClockWidget({}, cache)
        page = Page(name="Test", widgets=[w])
        assert fb.png == b""
        render_page(page, fb)
        assert len(fb.png) > 0

    def test_portrait_mode(self, tmp_path):
        cache = Cache()
        fb = FrameBuffer()
        w = ClockWidget({}, cache)
        page = Page(name="Test", widgets=[w])
        landscape = render_page(page, fb)
        portrait = render_page(page, fb, portrait=True)
        assert landscape != portrait

    def test_with_toasts(self, tmp_path):
        cache = Cache()
        fb = FrameBuffer()
        store = Store(tmp_path / "n.json")
        store.push(Notification(title="Toast!"))
        w = ClockWidget({}, cache)
        page = Page(name="Test", widgets=[w])
        buf = render_page(page, fb, store)
        from dashboard.display import BUF_SIZE
        assert len(buf) == BUF_SIZE


# ── HTTP Handler ──────────────────────────────────────────────────
@pytest.fixture()
def server_env(tmp_path):
    """Set up a full Handler environment and return (base_url, store, epd, pages)."""
    cache = Cache()
    fb = FrameBuffer()
    store = Store(tmp_path / "n.json")
    push_event = threading.Event()
    epd = MockEPD(tmp_path / "frames", fb)
    pages = _build_pages(DEFAULT_CONFIG, cache, store)
    page_index = [0]

    # Render initial frame so /frame has data
    render_page(pages[0], fb, store)

    emulator_html = b"<html>emulator</html>"

    def factory(request, client_address, server):
        return Handler(
            request, client_address, server,
            store=store, push_event=push_event, pages=pages,
            page_index=page_index, epd=epd, frame_buf=fb,
            emulator_html=emulator_html,
        )

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), factory)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    class Env:
        base_url = f"http://127.0.0.1:{port}"
        def shutdown(self):
            httpd.shutdown()

    env = Env()
    env.store = store
    env.epd = epd
    env.pages = pages
    env.page_index = page_index
    env.push_event = push_event
    yield env
    env.shutdown()


def _get(url: str) -> tuple[int, bytes]:
    try:
        with urlopen(url, timeout=5) as resp:
            return resp.status, resp.read()
    except Exception as e:
        return getattr(e, 'code', 0), getattr(e, 'read', lambda: b"")()


def _post(url: str, body: dict) -> tuple[int, dict]:
    data = json.dumps(body).encode()
    req = Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except Exception as e:
        code = getattr(e, 'code', 0)
        body_bytes = getattr(e, 'read', lambda: b'{}')()
        return code, json.loads(body_bytes) if body_bytes else {}


class TestHandlerStatus:
    def test_status_endpoint(self, server_env):
        code, body = _get(f"{server_env.base_url}/status")
        assert code == 200
        data = json.loads(body)
        assert "pages" in data
        assert "current_page" in data
        assert "notifications" in data

    def test_frame_endpoint(self, server_env):
        code, body = _get(f"{server_env.base_url}/frame")
        assert code == 200
        assert len(body) > 0
        # Verify it's a valid PNG
        assert body[:4] == b'\x89PNG'

    def test_emulator_html(self, server_env):
        code, body = _get(f"{server_env.base_url}/")
        assert code == 200
        assert b"emulator" in body

    def test_404(self, server_env):
        code, _ = _get(f"{server_env.base_url}/nonexistent")
        assert code == 404


class TestHandlerPush:
    def test_push_notification(self, server_env):
        code, data = _post(f"{server_env.base_url}/push", {"title": "Test"})
        assert code == 200
        assert data["ok"] is True
        assert len(server_env.store.items) == 1
        assert server_env.store.items[0].title == "Test"

    def test_push_with_body_and_priority(self, server_env):
        code, data = _post(f"{server_env.base_url}/push", {
            "title": "Alert", "body": "Details", "priority": 5,
        })
        assert code == 200
        n = server_env.store.items[0]
        assert n.body == "Details"
        assert n.priority == 5

    def test_push_missing_title(self, server_env):
        code, data = _post(f"{server_env.base_url}/push", {"body": "no title"})
        assert code == 400

    def test_clear_notifications(self, server_env):
        server_env.store.push(Notification(title="a"))
        code, data = _post(f"{server_env.base_url}/clear", {})
        assert code == 200
        assert len(server_env.store.items) == 0


class TestHandlerButtons:
    def test_press_button(self, server_env):
        code, data = _post(f"{server_env.base_url}/buttons", {"keys": [0]})
        assert code == 200
        assert data["ok"] is True

    def test_long_press(self, server_env):
        code, data = _post(f"{server_env.base_url}/buttons", {
            "keys": [{"key": 1, "action": "long_press"}],
        })
        assert code == 200

    def test_invalid_key(self, server_env):
        code, data = _post(f"{server_env.base_url}/buttons", {"keys": [5]})
        assert code == 400

    def test_empty_keys(self, server_env):
        code, data = _post(f"{server_env.base_url}/buttons", {"keys": []})
        assert code == 400

    def test_unknown_action(self, server_env):
        code, data = _post(f"{server_env.base_url}/buttons", {
            "keys": [{"key": 0, "action": "dance"}],
        })
        assert code == 400


class TestHandlerCors:
    def test_options_returns_cors_headers(self, server_env):
        req = Request(f"{server_env.base_url}/push", method="OPTIONS")
        with urlopen(req, timeout=5) as resp:
            assert resp.status == 204
            assert resp.headers.get("Access-Control-Allow-Origin") == "*"
