"""Tests for dashboard.system — system stat readers."""
from __future__ import annotations

import io
from unittest.mock import patch

from dashboard.system import CpuUsage, cpu_temp, disk_info, mem_info, uptime


class TestCpuTemp:
    def test_reads_and_formats(self):
        with patch("dashboard.system._read_stat", return_value="42500"):
            assert cpu_temp() == "42.5°C"

    def test_handles_zero(self):
        with patch("dashboard.system._read_stat", return_value="0"):
            assert cpu_temp() == "0.0°C"


class TestCpuUsage:
    def test_first_call_returns_ellipsis(self):
        cpu = CpuUsage()
        with patch("builtins.open", return_value=io.StringIO("cpu  100 0 0 900 0 0 0 0\n")):
            assert cpu.read() == "…"

    def test_second_call_returns_percentage(self):
        cpu = CpuUsage()
        # First: total=1000, idle=900
        with patch("builtins.open", return_value=io.StringIO("cpu  100 0 0 900 0 0 0 0\n")):
            cpu.read()
        # Second: total=2000, idle=1800 → d_idle=900, d_total=1000, used=10%
        with patch("builtins.open", return_value=io.StringIO("cpu  200 0 0 1800 0 0 0 0\n")):
            assert cpu.read() == "10%"

    def test_handles_os_error(self):
        cpu = CpuUsage()
        with patch("builtins.open", side_effect=OSError):
            assert cpu.read() == "?"

    def test_zero_delta_returns_zero(self):
        cpu = CpuUsage()
        data = "cpu  100 0 0 900 0 0 0 0\n"
        with patch("builtins.open", return_value=io.StringIO(data)):
            cpu.read()
        with patch("builtins.open", return_value=io.StringIO(data)):
            assert cpu.read() == "0%"


class TestMemInfo:
    def test_parses_meminfo(self):
        content = (
            "MemTotal:       16384 kB\n"
            "MemFree:         2048 kB\n"
            "MemAvailable:    8192 kB\n"
        )
        with patch("builtins.open", return_value=io.StringIO(content)):
            used, total, pct = mem_info()
            # MemTotal=16384kB, MemAvail=8192kB → used=8192kB = 8M (8192//1024)
            assert used == "8M"
            assert total == "16M"
            assert pct == "50%"

    def test_handles_os_error(self):
        with patch("builtins.open", side_effect=OSError):
            assert mem_info() == ("?", "?", "?")


class TestDiskInfo:
    def test_reads_statvfs(self):
        class FakeStat:
            f_blocks = 1000000
            f_frsize = 4096
            f_bavail = 500000
        with patch("os.statvfs", return_value=FakeStat()):
            used, total, pct = disk_info("/")
            assert "G" in used
            assert "G" in total
            assert "%" in pct

    def test_handles_os_error(self):
        with patch("os.statvfs", side_effect=OSError):
            assert disk_info() == ("?", "?", "?")


class TestUptime:
    def test_formats_days(self):
        with patch("dashboard.system._read_stat", return_value="172800.50"):
            assert uptime() == "2d 0h"

    def test_formats_hours(self):
        with patch("dashboard.system._read_stat", return_value="7200.00"):
            assert uptime() == "2h 0m"

    def test_formats_minutes(self):
        with patch("dashboard.system._read_stat", return_value="300.00"):
            assert uptime() == "5m"
