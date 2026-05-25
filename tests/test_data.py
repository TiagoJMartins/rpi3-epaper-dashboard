"""Tests for dashboard.data — fetch helpers and cache."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import pytest

from dashboard.data import Cache, check_service, fetch_grafana_alerts, fetch_json, prom_query


# ── Test HTTP server ──────────────────────────────────────────────
class _Handler(BaseHTTPRequestHandler):
    """Tiny handler for fetch tests. Response configured via class attrs."""
    response_code = 200
    response_body = b'{"ok": true}'
    response_headers: dict[str, str] = {}

    def do_GET(self) -> None:
        self.send_response(self.response_code)
        for k, v in self.response_headers.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.response_body)

    do_POST = do_GET
    do_HEAD = do_GET

    def log_message(self, *_): pass


@pytest.fixture()
def http_server():
    """Start a local HTTP server, yield its base URL, shut down after."""
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


# ── fetch_json ────────────────────────────────────────────────────
class TestFetchJson:
    def test_returns_parsed_json(self, http_server):
        _Handler.response_code = 200
        _Handler.response_body = b'{"answer": 42}'
        result = fetch_json(http_server)
        assert result == {"answer": 42}

    def test_returns_none_on_http_error(self, http_server):
        _Handler.response_code = 500
        _Handler.response_body = b'{}'
        result = fetch_json(http_server)
        # urllib raises on 500, so we get None
        assert result is None

    def test_returns_none_on_connection_error(self):
        result = fetch_json("http://127.0.0.1:1", timeout=0.5)
        assert result is None

    def test_returns_none_on_bad_json(self, http_server):
        _Handler.response_code = 200
        _Handler.response_body = b'not json'
        result = fetch_json(http_server)
        assert result is None

    def test_sends_post_body(self, http_server):
        _Handler.response_code = 200
        _Handler.response_body = b'{"received": true}'
        result = fetch_json(http_server, post_body=b'{"data": 1}')
        assert result == {"received": True}

    def test_sends_headers(self, http_server):
        _Handler.response_code = 200
        _Handler.response_body = b'{"ok": true}'
        result = fetch_json(http_server, headers={"X-Custom": "test"})
        assert result == {"ok": True}


# ── fetch_grafana_alerts ──────────────────────────────────────────
class TestFetchGrafanaAlerts:
    def test_counts_active_alerts(self, http_server):
        _Handler.response_code = 200
        _Handler.response_body = json.dumps([
            {"status": {"state": "active"}},
            {"status": {"state": "suppressed"}},
            {"status": {"state": "active"}},
        ]).encode()
        assert fetch_grafana_alerts(http_server) == 2

    def test_returns_zero_on_error(self):
        assert fetch_grafana_alerts("http://127.0.0.1:1") == 0

    def test_returns_zero_on_non_list(self, http_server):
        _Handler.response_code = 200
        _Handler.response_body = b'{"not": "a list"}'
        assert fetch_grafana_alerts(http_server) == 0


# ── prom_query ────────────────────────────────────────────────────
class TestPromQuery:
    def test_parses_prometheus_response(self, http_server):
        _Handler.response_code = 200
        _Handler.response_body = json.dumps({
            "results": {
                "A": {
                    "status": 200,
                    "frames": [{
                        "schema": {
                            "fields": [
                                {"name": "Time"},
                                {"name": "Value", "labels": {"instance": "node1:9100"}},
                            ],
                        },
                        "data": {
                            "values": [[1234567890], [0.42]],
                        },
                    }],
                },
            },
        }).encode()
        result = prom_query(http_server, 'up{job="test"}')
        assert result == [{"labels": {"instance": "node1:9100"}, "value": 0.42}]

    def test_returns_none_on_error(self):
        assert prom_query("http://127.0.0.1:1", "up") is None

    def test_returns_none_on_bad_status(self, http_server):
        _Handler.response_code = 200
        _Handler.response_body = json.dumps({
            "results": {"A": {"status": 500}},
        }).encode()
        assert prom_query(http_server, "up") is None


# ── check_service ─────────────────────────────────────────────────
class TestCheckService:
    def test_healthy_service(self, http_server):
        _Handler.response_code = 200
        _Handler.response_body = b'ok'
        assert check_service(http_server) is True

    def test_down_on_502(self, http_server):
        _Handler.response_code = 502
        assert check_service(http_server) is False

    def test_down_on_connection_refused(self):
        assert check_service("http://127.0.0.1:1") is False


# ── Cache ─────────────────────────────────────────────────────────
class TestCache:
    def test_first_get_returns_none_and_schedules_fetch(self):
        event = threading.Event()
        cache = Cache(on_update=event)
        result = cache.get("key1", 60, lambda: "value1")
        assert result is None
        # Wait for background fetch
        event.wait(timeout=2)
        result = cache.get("key1", 60, lambda: "value1_new")
        assert result == "value1"

    def test_returns_stale_while_refreshing(self):
        event = threading.Event()
        cache = Cache(on_update=event)
        # Prime the cache
        cache.get("k", 0.01, lambda: "v1")
        event.wait(timeout=2)
        event.clear()
        time.sleep(0.02)  # expire it
        # Should return stale "v1" and schedule refresh
        result = cache.get("k", 0.01, lambda: "v2")
        assert result == "v1"
        event.wait(timeout=2)
        result = cache.get("k", 60, lambda: "v3")
        assert result == "v2"

    def test_invalidate_all(self):
        event = threading.Event()
        cache = Cache(on_update=event)
        cache.get("a", 60, lambda: "va")
        event.wait(timeout=2)
        assert cache.get("a", 60, lambda: "new") == "va"
        cache.invalidate()
        assert cache.get("a", 60, lambda: "new") is None

    def test_invalidate_prefix(self):
        event = threading.Event()
        cache = Cache(on_update=event)
        cache.get("weather:1", 60, lambda: "w1")
        cache.get("svc:1", 60, lambda: "s1")
        event.wait(timeout=2)
        time.sleep(0.1)  # let both fetches complete
        cache.invalidate("weather:")
        assert cache.get("weather:1", 60, lambda: "w2") is None
        assert cache.get("svc:1", 60, lambda: "s2") == "s1"

    def test_failed_fetch_does_not_crash(self):
        event = threading.Event()
        cache = Cache(on_update=event)
        cache.get("bad", 60, lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        time.sleep(0.5)  # let thread finish
        # Key should still be fetchable
        cache.get("bad", 60, lambda: "recovered")
        event.wait(timeout=2)
        assert cache.get("bad", 60, lambda: "x") == "recovered"
