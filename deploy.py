#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyinfra"]
# ///
"""Provision Raspberry Pi for the e-Paper dashboard.

Installs system deps, uv, fonts, systemd service.
The service runs via uvx from the GitHub repo — no local file sync needed.

Usage:
    uv run pyinfra rpi3 deploy.py
"""
from pathlib import Path

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

# ── Fonts ──────────────────────────────────────────────────────────
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

# ── Systemd service ───────────────────────────────────────────────
result = files.put(
    name="Install dashboard.service",
    src="dashboard.service",
    dest="/etc/systemd/system/dashboard.service",
    user="root",
    group="root",
    mode="644",
    _sudo=True,
)

if result.changed:
    systemd.daemon_reload(
        name="Reload systemd",
        _sudo=True,
    )

systemd.service(
    name="Enable and start dashboard",
    service="dashboard.service",
    running=True,
    enabled=True,
    restarted=True,
    _sudo=True,
)

# ── Tailscale Serve (HTTPS proxy → localhost:8080) ────────────────
server.shell(
    name="Configure Tailscale Serve for HTTPS",
    commands=["tailscale serve --bg --https=443 http://localhost:8080"],
    _sudo=True,
)
