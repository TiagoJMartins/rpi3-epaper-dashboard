"""Tests for dashboard.widgets — all widget layout methods."""
from __future__ import annotations

import io
import json
import threading
import time
from unittest.mock import patch

import pytest

from dashboard.data import Cache
from dashboard.epd import HeaderBar, Node, Section, Text
from dashboard.notifications import Notification, Store
from dashboard.widgets import (
    ClockWidget, ClusterWidget, HomeAssistantWidget, NotificationsWidget,
    NowPlayingWidget, ProxmoxWidget, RadarrWidget, ServicesWidget,
    SonarrWidget, SystemWidget, WeatherWidget, Widget, WIDGET_TYPES,
    _ago, _truncate, build_toasts,
)


@pytest.fixture()
def cache():
    return Cache(on_update=threading.Event())


def _prime_cache(c: Cache, key: str, value):
    """Synchronously prime a cache entry."""
    ev = threading.Event()
    c.on_update = ev
    c.get(key, 9999, lambda: value)
    ev.wait(timeout=2)


# ── Helpers ───────────────────────────────────────────────────────
class TestAgo:
    def test_just_now(self):
        assert _ago(time.time()) == "agora"

    def test_minutes(self):
        assert _ago(time.time() - 120) == "2m"

    def test_hours(self):
        assert _ago(time.time() - 7200) == "2h"

    def test_days(self):
        assert _ago(time.time() - 172800) == "2d"


class TestTruncate:
    def test_short_string_unchanged(self):
        assert _truncate("hello") == "hello"

    def test_long_string_truncated(self):
        result = _truncate("a" * 50, max_len=10)
        assert len(result) == 10
        assert result.endswith("…")


# ── Widget base ───────────────────────────────────────────────────
class TestWidgetBase:
    def test_default_layout_is_spacer(self, cache):
        w = Widget({}, cache)
        node = w.layout()
        assert node is not None

    def test_refresh_interval_default(self, cache):
        w = Widget({}, cache)
        assert w.refresh_interval == 60.0

    def test_refresh_interval_custom(self, cache):
        w = Widget({"interval": 30.0}, cache)
        assert w.refresh_interval == 30.0


# ── ClockWidget ───────────────────────────────────────────────────
class TestClockWidget:
    def test_renders_header_bar(self, cache):
        w = ClockWidget({}, cache)
        node = w.layout()
        assert isinstance(node, HeaderBar)

    def test_with_weather(self, cache):
        _prime_cache(cache, "weather:41.0:-8.0", {
            "current": {"temperature_2m": 22.5, "weather_code": 0},
        })
        w = ClockWidget({"latitude": 41.0, "longitude": -8.0}, cache)
        node = w.layout()
        assert isinstance(node, HeaderBar)
        assert "22" in node.center


# ── WeatherWidget ─────────────────────────────────────────────────
class TestWeatherWidget:
    def test_no_data(self, cache):
        w = WeatherWidget({}, cache)
        node = w.layout()
        assert isinstance(node, Section)

    def test_with_data(self, cache):
        _prime_cache(cache, "weather:41.54:-8.41", {
            "current": {
                "temperature_2m": 18.0,
                "relative_humidity_2m": 65,
                "weather_code": 0,
                "wind_speed_10m": 12,
            },
            "daily": {
                "temperature_2m_max": [20, 22, 19],
                "temperature_2m_min": [12, 14, 11],
                "weather_code": [0, 1, 3],
                "time": ["2024-01-01", "2024-01-02", "2024-01-03"],
            },
        })
        w = WeatherWidget({}, cache)
        node = w.layout()
        assert isinstance(node, Section)


# ── SystemWidget ──────────────────────────────────────────────────
class TestSystemWidget:
    def test_renders_section(self, cache):
        with patch("dashboard.widgets.cpu_temp", return_value="45.0°C"), \
             patch("dashboard.widgets.mem_info", return_value=("4096M", "8192M", "50%")), \
             patch("dashboard.widgets.disk_info", return_value=("10.0G", "50.0G", "20%")), \
             patch("dashboard.widgets.uptime", return_value="2d 3h"):
            w = SystemWidget({}, cache)
            with patch("builtins.open", return_value=io.StringIO("cpu  100 0 0 900 0 0 0 0\n")):
                w._cpu.read()
            node = w.layout()
            assert isinstance(node, Section)


# ── ServicesWidget ────────────────────────────────────────────────
class TestServicesWidget:
    def test_no_items(self, cache):
        w = ServicesWidget({}, cache)
        node = w.layout()
        assert isinstance(node, Section)

    def test_with_services(self, cache):
        cfg = {
            "items": [
                {"name": "Web", "url": "http://example.com"},
            ],
        }
        _prime_cache(cache, "svc:http://example.com", True)
        w = ServicesWidget(cfg, cache)
        node = w.layout()
        assert isinstance(node, Section)


# ── NotificationsWidget ──────────────────────────────────────────
class TestNotificationsWidget:
    def test_no_store(self, cache):
        w = NotificationsWidget({}, cache)
        node = w.layout()
        assert isinstance(node, Section)

    def test_with_notifications(self, cache, tmp_path):
        store = Store(tmp_path / "n.json")
        store.push(Notification(title="Alert!", priority=5))
        store.push(Notification(title="Info", body="Some details here"))
        w = NotificationsWidget({}, cache)
        w.store = store
        node = w.layout()
        assert isinstance(node, Section)


# ── NowPlayingWidget ─────────────────────────────────────────────
class TestNowPlayingWidget:
    def test_not_configured(self, cache):
        w = NowPlayingWidget({}, cache)
        node = w.layout()
        assert isinstance(node, Section)

    def test_no_sessions(self, cache):
        cfg = {"url": "http://tautulli", "api_key": "test"}
        _prime_cache(cache, "tautulli:http://tautulli", {
            "response": {"data": {"sessions": []}},
        })
        w = NowPlayingWidget(cfg, cache)
        node = w.layout()
        assert isinstance(node, Section)

    def test_with_sessions(self, cache):
        cfg = {"url": "http://tautulli", "api_key": "test"}
        _prime_cache(cache, "tautulli:http://tautulli", {
            "response": {"data": {"sessions": [
                {"title": "Movie", "friendly_name": "User", "state": "playing", "player": "TV"},
            ]}},
        })
        w = NowPlayingWidget(cfg, cache)
        node = w.layout()
        assert isinstance(node, Section)


# ── SonarrWidget ──────────────────────────────────────────────────
class TestSonarrWidget:
    def test_not_configured(self, cache):
        w = SonarrWidget({}, cache)
        node = w.layout()
        assert isinstance(node, Section)

    def test_with_data(self, cache):
        cfg = {"url": "http://sonarr", "api_key": "test"}
        _prime_cache(cache, "sonarr:http://sonarr", [
            {"series": {"title": "Show"}, "seasonNumber": 1, "episodeNumber": 5},
        ])
        w = SonarrWidget(cfg, cache)
        node = w.layout()
        assert isinstance(node, Section)


# ── RadarrWidget ──────────────────────────────────────────────────
class TestRadarrWidget:
    def test_not_configured(self, cache):
        w = RadarrWidget({}, cache)
        node = w.layout()
        assert isinstance(node, Section)

    def test_with_data(self, cache):
        cfg = {"url": "http://radarr", "api_key": "test"}
        _prime_cache(cache, "radarr:http://radarr", [
            {"title": "Film", "year": 2024},
        ])
        w = RadarrWidget(cfg, cache)
        node = w.layout()
        assert isinstance(node, Section)


# ── ProxmoxWidget ─────────────────────────────────────────────────
class TestProxmoxWidget:
    def test_not_configured(self, cache):
        w = ProxmoxWidget({}, cache)
        node = w.layout()
        assert isinstance(node, Section)

    def test_with_data(self, cache):
        cfg = {"url": "http://pve", "node": "n1",
               "username": "u", "token_name": "t", "token_value": "v"}
        _prime_cache(cache, "pve:http://pve:n1", {
            "data": {
                "cpu": 0.25,
                "memory": {"used": 4 * (1 << 30), "total": 16 * (1 << 30)},
                "uptime": 172800,
            },
        })
        w = ProxmoxWidget(cfg, cache)
        node = w.layout()
        assert isinstance(node, Section)


# ── HomeAssistantWidget ───────────────────────────────────────────
class TestHomeAssistantWidget:
    def test_not_configured(self, cache):
        w = HomeAssistantWidget({}, cache)
        node = w.layout()
        assert isinstance(node, Section)

    def test_with_entity(self, cache):
        cfg = {"url": "http://ha", "token": "tok", "entities": ["sensor.temp"]}
        _prime_cache(cache, "ha:sensor.temp", {
            "attributes": {"friendly_name": "Temperature", "unit_of_measurement": "°C"},
            "state": "22.5",
        })
        w = HomeAssistantWidget(cfg, cache)
        node = w.layout()
        assert isinstance(node, Section)

    def test_entity_no_data(self, cache):
        cfg = {"url": "http://ha", "token": "tok", "entities": ["sensor.unknown"]}
        w = HomeAssistantWidget(cfg, cache)
        node = w.layout()
        assert isinstance(node, Section)


# ── ClusterWidget ─────────────────────────────────────────────────
class TestClusterWidget:
    def test_no_grafana_url(self, cache):
        w = ClusterWidget({}, cache)
        node = w.layout()
        assert isinstance(node, Section)

    def test_with_metrics(self, cache):
        cfg = {"grafana_url": "http://grafana"}
        _prime_cache(cache, "grafana_alerts:http://grafana", 2)
        for expr_prefix, val in [("instance:node_cpu", 0.15), ("1 - (node_memory", 0.6), ("1 - (node_filesystem", 0.3)]:
            # Find the actual key used
            for key_part in ["instance:node_cpu_utilisation:rate5m{job=\"node-exporter\"}",
                             "1 - (node_memory_MemAvailable_bytes{job=\"node-exporter\"} / node_memory_MemTotal_bytes{job=\"node-exporter\"})",
                             "1 - (node_filesystem_avail_bytes{job=\"node-exporter\",mountpoint=\"/\",fstype!=\"tmpfs\"} / node_filesystem_size_bytes{job=\"node-exporter\",mountpoint=\"/\",fstype!=\"tmpfs\"})"]:
                if key_part.startswith(expr_prefix):
                    _prime_cache(cache, f"prom:{key_part}", [
                        {"labels": {"instance": "node1:9100", "node": "node1"}, "value": val},
                    ])
        w = ClusterWidget(cfg, cache)
        node = w.layout()
        assert isinstance(node, Section)


# ── build_toasts ──────────────────────────────────────────────────
class TestBuildToasts:
    def test_empty_store(self, tmp_path):
        store = Store(tmp_path / "n.json")
        toasts = build_toasts(store)
        assert toasts == []

    def test_with_notifications(self, tmp_path):
        store = Store(tmp_path / "n.json")
        store.push(Notification(title="Alert", priority=5, body="Details"))
        store.push(Notification(title="Info"))
        toasts = build_toasts(store)
        assert len(toasts) == 2

    def test_max_three_toasts(self, tmp_path):
        store = Store(tmp_path / "n.json")
        for i in range(5):
            store.push(Notification(title=f"n{i}"))
        toasts = build_toasts(store)
        assert len(toasts) == 3


# ── Registry ──────────────────────────────────────────────────────
class TestWidgetRegistry:
    def test_all_widget_types_registered(self):
        expected = {
            "clock", "weather", "system", "services", "notifications",
            "now_playing", "sonarr", "radarr", "proxmox",
            "home_assistant", "cluster",
        }
        assert set(WIDGET_TYPES.keys()) == expected

    def test_all_are_widget_subclasses(self):
        for cls in WIDGET_TYPES.values():
            assert issubclass(cls, Widget)
