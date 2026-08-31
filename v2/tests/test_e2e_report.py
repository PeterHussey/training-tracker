# tests/test_e2e_report.py
import io
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

from e2e_report import (
    _fmt,
    e2e_checks,
    filter_period,
    in_period,
    load_data,
    run_report,
    week_start,
)

FIXTURES = Path(__file__).parent / "fixtures"
V2 = Path(__file__).parent.parent
PYTHON = Path(__file__).parent.parent.parent / ".venv" / "bin" / "python"


def _offline_data():
    return load_data("offline", FIXTURES)


def test_week_start_parses_iso_week():
    assert week_start("2026-W34") == date(2026, 8, 17)
    assert week_start("2026-W33") == date(2026, 8, 10)


def test_week_start_invalid():
    assert week_start("bogus") is None
    assert week_start("2026-08-01") is None


def test_in_period_day_and_week():
    since = date(2026, 7, 30)
    assert in_period("2026-08-20", since)
    assert not in_period("2026-07-01", since)
    assert in_period("2026-W34", since)
    assert not in_period("2026-W25", since)


def test_fmt_seconds_is_total_time_not_pace():
    assert _fmt(1405, {"unit": "s"}) == "23:25"
    assert _fmt(17179, {"unit": "s"}) == "4:46:19"
    assert _fmt(0.0, {"unit": "s"}) == "0:00"


def test_filter_period_filters_rows():
    rows = [{"date": "2026-08-01"}, {"date": "2026-08-24"}, {"date": "2026-W34"}]
    out = filter_period(rows, date(2026, 8, 20))
    assert [r["date"] for r in out] == ["2026-08-24", "2026-W34"]


def test_run_report_offline_ok(capsys):
    acts, lt, race, meta = _offline_data()
    rc = run_report(acts, lt, race, meta, date(2026, 8, 1))
    text = capsys.readouterr().out
    assert rc == 0
    for token in ("volume.distance_total", "load.banister", "pmc.ctl",
                  "fitness.vo2max", "race_5k", "E2E checks: PASS"):
        assert token in text
    assert "load.acwr" in text


def test_e2e_checks_flags_missing_metric():
    acts, lt, race, _ = _offline_data()
    metrics = {"fitness.vo2max": []}
    fails = e2e_checks(acts, metrics, 0, lt, race)
    assert any("no metrics" in f for f in fails)
    assert any("missing metric volume.distance_total" in f for f in fails)


def test_load_data_offline_reads_fixtures():
    acts, lt, race, meta = _offline_data()
    assert len(acts) == 20
    assert meta["source"] == "fixtures"
    assert lt["speed_and_heart_rate"]["heartRate"] == 168
    assert race["Run_5k"]["time"] == 1500000


def test_e2e_report_script_offline_smoke():
    proc = subprocess.run(
        [str(PYTHON), "e2e_report.py", "--data", "offline", "--since", "2026-08-01"],
        cwd=V2, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    for token in ("volume.distance_total", "volume.rolling4wk_running",
                  "elevation.gain_per_km_running", "load.banister", "load.edwards",
                  "pmc.ctl", "pmc.atl", "pmc.tsb", "load.acwr", "fitness.vo2max",
                  "load.cs_approx", "load.lt_hr", "load.lt_pace",
                  "race_5k", "race_10k", "race_half", "race_full",
                  "E2E checks: PASS"):
        assert token in proc.stdout, f"missing {token!r}"