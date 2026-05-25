"""Data fetching and caching."""
from __future__ import annotations

import json
import logging
import ssl
import threading
import time
from typing import Any, Callable
from urllib.request import Request, urlopen

log = logging.getLogger("epd-dash")

# Reusable SSL context for internal HTTPS (self-signed certs)
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def fetch_json(url: str, timeout: float = 5.0, headers: dict[str, str] | None = None,
               post_body: bytes | None = None) -> Any:
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


def fetch_grafana_alerts(grafana_url: str) -> int:
    """Return count of active Grafana alerts."""
    data = fetch_json(f"{grafana_url}/api/alertmanager/grafana/api/v2/alerts")
    if isinstance(data, list):
        return sum(1 for a in data if a.get("status", {}).get("state") == "active")
    return 0


def prom_query(grafana_url: str, expr: str) -> list[dict] | None:
    """Run an instant Prometheus query via Grafana proxy."""
    payload = {
        "queries": [{
            "refId": "A",
            "datasourceId": 1,
            "expr": expr,
            "instant": True,
        }],
    }
    data = fetch_json(
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


_SERVICE_DOWN_CODES = {502, 503, 504}


def check_service(url: str) -> bool:
    """Check if a service is reachable. 502/503/504 and connection failures = down."""
    ctx = _ssl_ctx if url.startswith("https") else None
    for method in ("HEAD", "GET"):
        try:
            req = Request(url, method=method)
            with urlopen(req, timeout=5, context=ctx) as resp:
                return resp.status not in _SERVICE_DOWN_CODES
        except Exception as e:
            code = getattr(e, 'code', None)
            if code is not None:
                return code not in _SERVICE_DOWN_CODES
            if method == "HEAD":
                continue
            return False
    return False


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
        self.on_update = on_update

    def get(self, key: str, ttl: float, fetch: Callable[[], Any]) -> Any:
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if entry is not None:
                ts, val = entry
                if now - ts < ttl:
                    return val
                self._schedule(key, fetch)
                return val
            self._schedule(key, fetch)
            return None

    def _schedule(self, key: str, fetch: Callable[[], Any]) -> None:
        if key in self._pending:
            return
        self._pending.add(key)
        threading.Thread(target=self._do_fetch, args=(key, fetch), daemon=True).start()

    def _do_fetch(self, key: str, fetch: Callable[[], Any]) -> None:
        try:
            val = fetch()
            with self._lock:
                self._data[key] = (time.monotonic(), val)
            if self.on_update:
                self.on_update.set()
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
