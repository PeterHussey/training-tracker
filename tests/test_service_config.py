"""Tests for the service plumbing: Streamlit config and launchd plists."""

import os
import plistlib
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_PLIST_KEYS = (
    "Label",
    "ProgramArguments",
    "RunAtLoad",
    "StandardOutPath",
    "StandardErrorPath",
)


def _plist(name: str) -> dict:
    path = ROOT / "deploy" / "launchd" / f"com.trainingtracker.{name}.plist"
    with open(path, "rb") as f:
        return plistlib.load(f)


def test_streamlit_config_pins_loopback_and_theme():
    cfg = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text())
    assert cfg["server"]["address"] == "127.0.0.1"
    assert cfg["theme"]["primaryColor"]


def test_dashboard_plist():
    p = _plist("dashboard")
    for key in REQUIRED_PLIST_KEYS:
        assert key in p
    args = " ".join(p["ProgramArguments"])
    assert "127.0.0.1" in args
    assert "dashboard.py" in args


def test_refresh_plist_runs_daily_at_5():
    p = _plist("refresh")
    for key in REQUIRED_PLIST_KEYS:
        assert key in p
    assert p["StartCalendarInterval"]["Hour"] == 5
    assert p["StartCalendarInterval"]["Minute"] == 0


def test_install_script_exists_and_is_executable():
    script = ROOT / "deploy" / "launchd" / "install.sh"
    assert script.exists()
    assert os.access(script, os.X_OK)
