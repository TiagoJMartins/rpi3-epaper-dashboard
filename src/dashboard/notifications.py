"""Notification store — persistent queue of push notifications."""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


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
