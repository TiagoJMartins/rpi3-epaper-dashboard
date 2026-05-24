#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyinfra"]
# ///
"""Provision and deploy e-Paper dashboard to Raspberry Pi.

Usage:
    uv run deploy.py
"""
from pathlib import Path

from pyinfra import host
from pyinfra.operations import apt, files, server, systemd

# ── System deps ────────────────────────────────────────────────────
apt.packages(
    name="Install build deps",
    packages=[
        "python3-dev", "gcc", "swig",
        "liblgpio-dev", "libjpeg-dev", "libfreetype-dev", "zlib1g-dev",
    ],
    _sudo=True,
    update=True,
)

# ── uv ─────────────────────────────────────────────────────────────
server.shell(
    name="Install uv if missing",
    commands=["test -f /home/tiago/.local/bin/uv || curl -LsSf https://astral.sh/uv/install.sh | sh"],
)

# ── User groups (GPIO + SPI without sudo) ──────────────────────────
server.shell(
    name="Ensure tiago in gpio,spi groups",
    commands=["usermod -aG gpio,spi tiago"],
    _sudo=True,
)

# ── Dashboard files ────────────────────────────────────────────────
files.put(
    name="Upload dashboard.py",
    src="dashboard.py",
    dest="/home/tiago/dashboard.py",
    user="tiago",
    group="tiago",
)

files.directory(
    name="Create fonts directory",
    path="/home/tiago/fonts",
    user="tiago",
    group="tiago",
)

for ttf in Path("fonts").glob("*.ttf"):
    files.put(
        name=f"Upload {ttf.name}",
        src=str(ttf),
        dest=f"/home/tiago/fonts/{ttf.name}",
        user="tiago",
        group="tiago",
    )

# ── Systemd units ──────────────────────────────────────────────────
units = [
    "epd-dashboard.service",
    "epd-dashboard-watcher.path",
    "epd-dashboard-watcher.service",
]
units_changed = False

for unit in units:
    result = files.put(
        name=f"Install {unit}",
        src=unit,
        dest=f"/etc/systemd/system/{unit}",
        user="root",
        group="root",
        mode="644",
        _sudo=True,
    )
    units_changed = units_changed or result.changed

if units_changed:
    systemd.daemon_reload(
        name="Reload systemd",
        _sudo=True,
    )

systemd.service(
    name="Enable and start dashboard",
    service="epd-dashboard.service",
    running=True,
    enabled=True,
    restarted=True,
    _sudo=True,
)

systemd.service(
    name="Enable file watcher",
    service="epd-dashboard-watcher.path",
    running=True,
    enabled=True,
    _sudo=True,
)
