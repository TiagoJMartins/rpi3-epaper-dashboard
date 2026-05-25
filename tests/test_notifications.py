"""Tests for dashboard.notifications — Notification and Store."""
from __future__ import annotations

import json

import pytest

from dashboard.notifications import Notification, Store


class TestNotification:
    def test_defaults(self):
        n = Notification(title="test")
        assert n.body == ""
        assert n.priority == 3
        assert n.read is False
        assert n.ts > 0

    def test_custom_fields(self):
        n = Notification(title="t", body="b", priority=5, ts=1.0, read=True)
        assert n.title == "t"
        assert n.body == "b"
        assert n.priority == 5
        assert n.ts == 1.0
        assert n.read is True


class TestStore:
    def test_push_and_items(self, tmp_path):
        store = Store(tmp_path / "n.json")
        store.push(Notification(title="first"))
        store.push(Notification(title="second"))
        assert len(store.items) == 2
        assert store.items[0].title == "second"  # LIFO

    def test_persistence(self, tmp_path):
        path = tmp_path / "n.json"
        store = Store(path)
        store.push(Notification(title="persisted"))
        # Reload
        store2 = Store(path)
        assert len(store2.items) == 1
        assert store2.items[0].title == "persisted"

    def test_dismiss(self, tmp_path):
        store = Store(tmp_path / "n.json")
        store.push(Notification(title="a"))
        store.push(Notification(title="b"))
        store.dismiss(0)  # dismiss top
        assert len(store.items) == 1
        assert store.items[0].title == "a"

    def test_dismiss_out_of_range(self, tmp_path):
        store = Store(tmp_path / "n.json")
        store.push(Notification(title="a"))
        store.dismiss(5)  # no-op
        assert len(store.items) == 1

    def test_clear(self, tmp_path):
        store = Store(tmp_path / "n.json")
        store.push(Notification(title="a"))
        store.push(Notification(title="b"))
        store.clear()
        assert len(store.items) == 0

    def test_max_items(self, tmp_path):
        store = Store(tmp_path / "n.json", max_items=3)
        for i in range(5):
            store.push(Notification(title=f"n{i}"))
        assert len(store.items) == 3
        assert store.items[0].title == "n4"

    def test_handles_corrupt_file(self, tmp_path):
        path = tmp_path / "n.json"
        path.write_text("not valid json!!!")
        store = Store(path)
        assert store.items == []

    def test_handles_missing_file(self, tmp_path):
        store = Store(tmp_path / "nonexistent.json")
        assert store.items == []

    def test_save_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "deep" / "nested" / "n.json"
        store = Store(path)
        store.push(Notification(title="test"))
        assert path.exists()
