"""System stat reading — /proc, /sys, and OS-level info."""
from __future__ import annotations

import os
from pathlib import Path


def _read_stat(path: str, default: str = "?") -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def cpu_temp() -> str:
    raw = _read_stat("/sys/class/thermal/thermal_zone0/temp", "0")
    return f"{int(raw) / 1000:.1f}°C"


class CpuUsage:
    """Stateful CPU usage tracker. Needs two calls to produce a reading."""

    __slots__ = ("_prev_idle", "_prev_total")

    def __init__(self) -> None:
        self._prev_idle: int | None = None
        self._prev_total: int | None = None

    def read(self) -> str:
        try:
            with open("/proc/stat") as f:
                parts = f.readline().split()
            idle = int(parts[4])
            total = sum(int(p) for p in parts[1:])
            if self._prev_idle is None:
                self._prev_idle, self._prev_total = idle, total
                return "…"
            d_idle = idle - self._prev_idle
            d_total = total - self._prev_total  # type: ignore[operator]
            self._prev_idle, self._prev_total = idle, total
            if d_total == 0:
                return "0%"
            return f"{100 * (1 - d_idle / d_total):.0f}%"
        except OSError:
            return "?"


def mem_info() -> tuple[str, str, str]:
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


def disk_info(mount: str = "/") -> tuple[str, str, str]:
    try:
        st = os.statvfs(mount)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - free
        pct = f"{100 * used / total:.0f}%" if total else "?"
        return f"{used // (1 << 30):.1f}G", f"{total // (1 << 30):.1f}G", pct
    except (OSError, AttributeError):
        return "?", "?", "?"


def uptime() -> str:
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
