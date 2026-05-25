"""Tests for dashboard.personality — mood faces, greetings, quips."""
from __future__ import annotations

from datetime import datetime

from dashboard.personality import (
    FACE_CALM,
    FACE_COOL,
    FACE_HAPPY,
    FACE_SAD,
    FACE_STRESSED,
    FACE_WARM,
    all_services_up,
    empty_notifications,
    empty_playing,
    greeting,
    system_mood,
    weather_quip,
)


# ── system_mood ──────────────────────────────────────────────────
class TestSystemMood:
    def test_low_cpu_is_cool(self):
        assert system_mood(5) == FACE_COOL

    def test_normal_cpu_is_happy(self):
        assert system_mood(50) == FACE_HAPPY

    def test_high_cpu_is_warm(self):
        assert system_mood(75) == FACE_WARM

    def test_extreme_cpu_is_stressed(self):
        assert system_mood(95) == FACE_STRESSED

    def test_unknown_cpu_is_calm(self):
        assert system_mood(-1) == FACE_CALM

    def test_services_down_overrides_cpu(self):
        assert system_mood(10, down_count=1) == FACE_WARM
        assert system_mood(10, down_count=3) == FACE_SAD


# ── greeting ─────────────────────────────────────────────────────
class TestGreeting:
    def test_morning(self):
        assert greeting(datetime(2024, 1, 1, 8, 0)) == 'Bom dia'

    def test_afternoon(self):
        assert greeting(datetime(2024, 1, 1, 15, 0)) == 'Boa tarde'

    def test_night(self):
        assert greeting(datetime(2024, 1, 1, 22, 0)) == 'Boa noite'

    def test_late_night(self):
        assert greeting(datetime(2024, 1, 1, 2, 0)) == 'Boa noite'

    def test_boundary_5am(self):
        assert greeting(datetime(2024, 1, 1, 5, 0)) == 'Bom dia'

    def test_boundary_13h(self):
        assert greeting(datetime(2024, 1, 1, 13, 0)) == 'Boa tarde'

    def test_boundary_20h(self):
        assert greeting(datetime(2024, 1, 1, 20, 0)) == 'Boa noite'


# ── weather_quip ─────────────────────────────────────────────────
class TestWeatherQuip:
    def test_thunderstorm(self):
        assert '⚡' in weather_quip(code=95)

    def test_snow(self):
        assert weather_quip(code=71) != ''

    def test_rain(self):
        assert 'guarda-chuva' in weather_quip(code=63)

    def test_hot(self):
        assert 'calor' in weather_quip(temp=38)

    def test_freezing(self):
        assert weather_quip(temp=-2) != ''

    def test_cold(self):
        assert 'Agasalha' in weather_quip(temp=3)

    def test_normal_weather_empty(self):
        assert weather_quip(temp=20, code=1) == ''

    def test_no_args_empty(self):
        assert weather_quip() == ''


# ── varied messages ──────────────────────────────────────────────
class TestVariedMessages:
    def test_empty_notifications_returns_string(self):
        msg = empty_notifications()
        assert isinstance(msg, str) and len(msg) > 0

    def test_empty_playing_returns_string(self):
        msg = empty_playing()
        assert isinstance(msg, str) and len(msg) > 0

    def test_all_services_up_returns_string(self):
        msg = all_services_up()
        assert isinstance(msg, str) and len(msg) > 0
