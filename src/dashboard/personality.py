"""Pwnagotchi-inspired personality layer for the e-paper dashboard.

Adds mood faces, time-aware greetings, weather quips, varied empty-state
messages, and uptime milestone markers — all in Portuguese.
"""
from __future__ import annotations

import hashlib
from datetime import datetime


# ── Mood faces (pwnagotchi-style ASCII, sized for 1-bit e-paper) ──
# Kept short to fit in tight header/section labels.
FACE_HAPPY    = '(•‿•)'
FACE_COOL     = '(⌐■_■)'
FACE_CALM     = '(‿‿)'
FACE_WARM     = '(°_°)'
FACE_STRESSED = '(>_<)'
FACE_SAD      = '(•︵•)'
FACE_SLEEP    = '(-_-)z'


def system_mood(cpu_pct: float, down_count: int = 0) -> str:
    """Pick a mood face based on system health.

    cpu_pct: CPU percentage as a float (0-100), or -1 if unknown.
    down_count: number of services currently down.
    """
    if down_count >= 3:
        return FACE_SAD
    if down_count >= 1:
        return FACE_WARM
    if cpu_pct < 0:
        return FACE_CALM
    if cpu_pct >= 90:
        return FACE_STRESSED
    if cpu_pct >= 70:
        return FACE_WARM
    if cpu_pct < 20:
        return FACE_COOL
    return FACE_HAPPY


# ── Time-aware greetings ──────────────────────────────────────────
def greeting(now: datetime | None = None) -> str:
    """Portuguese greeting based on time of day."""
    h = (now or datetime.now()).hour
    if 5 <= h < 13:
        return 'Bom dia'
    if 13 <= h < 20:
        return 'Boa tarde'
    return 'Boa noite'


# ── Weather quips ─────────────────────────────────────────────────
def weather_quip(temp: float | None = None, code: int | None = None) -> str:
    """Short Portuguese quip for current weather conditions.

    Returns empty string when nothing notable.
    """
    if code is not None:
        if code in (95, 96, 99):
            return '⚡ Trovoada!'
        if code in (71, 73, 75, 77, 85, 86):
            return '❄ Neve?!'
        if code in (61, 63, 65, 66, 67, 80, 81, 82):
            return '☂ Leva guarda-chuva!'
        if code in (51, 53, 55, 56, 57):
            return '💧 Chuviscos'
    if temp is not None:
        if temp >= 35:
            return '🔥 Que calor!'
        if temp <= 0:
            return '🥶 Gelo!'
        if temp <= 5:
            return '🧥 Agasalha-te!'
    return ''

# ── Varied empty-state messages ───────────────────────────────────
def _pick(options: list[str], seed: str = '') -> str:
    """Deterministic-per-minute pick from options, so the display doesn't
    flicker between refreshes within the same minute."""
    minute_seed = datetime.now().strftime('%Y%m%d%H%M') + seed
    idx = int(hashlib.md5(minute_seed.encode()).hexdigest(), 16) % len(options)
    return options[idx]


_EMPTY_NOTIF = [
    'Sem notificações',
    'Tudo calmo',
    'Nada para reportar',
    'Caixa limpa ✓',
    'Silêncio total',
]

_EMPTY_PLAYING = [
    'Nada a reproduzir',
    'Silêncio no Plex',
    'Pausa criativa',
    'Ninguém a ver nada',
]

_ALL_SERVICES_UP = [
    'Tudo operacional ✓',
    'Sem dramas hoje',
    'Tudo a funcionar',
    'Zero problemas',
]


def empty_notifications() -> str:
    return _pick(_EMPTY_NOTIF, 'notif')


def empty_playing() -> str:
    return _pick(_EMPTY_PLAYING, 'play')


def all_services_up() -> str:
    return _pick(_ALL_SERVICES_UP, 'svc')
