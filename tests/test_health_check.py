"""Tests for health_check.py — the checks, and the CLI that runs them."""

from datetime import date, timedelta

import pytest

import health_check
import refresh
from normalize import Activity
from store import MetricStore


def _act(aid: int, day: date, dist_m: float = 10000.0, dur_s: float = 3000.0) -> Activity:
    return Activity(
        activity_id=aid,
        sport="running",
        date=day,
        ts_ms=0,
        distance_m=dist_m,
        duration_s=dur_s,
        elapsed_s=dur_s,
        avg_hr=150.0,
        max_hr=170.0,
    )


def _seed(tmp_path, acts=None, metrics=()):
    """A store with activities and metric_series rows."""
    path = tmp_path / "t.sqlite"
    store = MetricStore(path)
    if acts:
        store.save_activities(acts)
    for metric, day, value in metrics:
        store.conn.execute(
            "INSERT OR REPLACE INTO metric_series (metric, date, value, source) "
            "VALUES (?, ?, ?, 'test')",
            (metric, day, value),
        )
    store.conn.commit()
    store.close()
    return path


def _ctx(tmp_path, path, spec_text: str | None = None) -> health_check.Context:
    spec = tmp_path / "spec.md"
    spec.write_text(spec_text if spec_text is not None else "volume pmc load race_5k")
    return health_check.Context(db_path=path, spec_path=spec)


def test_store_mode_check_passes_on_a_wal_store(tmp_path):
    path = _seed(tmp_path)
    detail = health_check.check_store_mode(_ctx(tmp_path, path))
    assert "wal" in detail


def test_monthly_rollup_check_passes_for_the_current_month(tmp_path):
    today = date.today()
    month = today.replace(day=1)
    path = _seed(
        tmp_path,
        metrics=[
            ("volume.distance_total_daily", month.isoformat(), 10.0),
            ("volume.distance_total_daily", (month + timedelta(days=1)).isoformat(), 5.5),
        ],
    )
    detail = health_check.check_monthly_rollup(_ctx(tmp_path, path))
    assert "15.500" in detail


def test_monthly_rollup_check_is_quiet_when_the_month_has_no_activity(tmp_path):
    last_month = (date.today().replace(day=1) - timedelta(days=1)).isoformat()
    path = _seed(tmp_path, metrics=[("volume.distance_total_daily", last_month, 10.0)])
    detail = health_check.check_monthly_rollup(_ctx(tmp_path, path))
    assert "no activity yet" in detail


def test_spec_check_fails_for_an_undocumented_family(tmp_path):
    path = _seed(tmp_path, metrics=[("zzz_unknown.metric", "2026-01-01", 1.0)])
    ctx = _ctx(tmp_path, path, spec_text="volume pmc")
    with pytest.raises(AssertionError, match="zzz_unknown"):
        health_check.check_spec_covers_families(ctx)


def test_spec_check_passes_when_every_family_is_documented(tmp_path):
    path = _seed(tmp_path, metrics=[("volume.distance_total_daily", "2026-01-01", 1.0)])
    ctx = _ctx(tmp_path, path, spec_text="volume")
    assert "1 families documented" in health_check.check_spec_covers_families(ctx)


def test_concurrency_check_passes(tmp_path):
    path = _seed(tmp_path, acts=[_act(1, date(2026, 8, 4))])
    detail = health_check.check_concurrency(_ctx(tmp_path, path))
    assert "reader proceeded" in detail


class _StubGw:
    """Gateway double: no activities, no trends."""

    def __init__(self, cache_dir=None):
        pass

    def fetch_activities(self, start, end):
        return []

    def fetch_lactate_threshold(self):
        return {}

    def fetch_race_predictions_trend(self, start, end):
        return []

    def fetch_vo2max_trend(self, start, end):
        return []


def test_refresh_check_passes_with_a_stub_gateway(tmp_path, monkeypatch):
    path = _seed(tmp_path, acts=[_act(1, date(2026, 8, 4))])
    monkeypatch.setattr(refresh, "GarminGateway", _StubGw)
    detail = health_check.check_refresh(_ctx(tmp_path, path))
    assert "refresh wrote" in detail


def test_stored_vs_garmin_check_passes_when_the_summary_matches(tmp_path, monkeypatch):
    act = _act(42, date(2026, 8, 4), dist_m=10000.0, dur_s=3000.0)
    path = _seed(tmp_path, acts=[act])

    class _Gw:
        def fetch_activity_summary(self, activity_id):
            return {"summaryDTO": {"distance": 10000.0, "duration": 3000.0, "averageHR": 150.0}}

    monkeypatch.setattr(health_check, "_gateway", lambda: _Gw())
    detail = health_check.check_stored_vs_garmin(_ctx(tmp_path, path))
    assert "1 activity(s) match Garmin" in detail


def test_stored_vs_garmin_check_fails_when_garmin_disagrees(tmp_path, monkeypatch):
    act = _act(42, date(2026, 8, 4), dist_m=10000.0, dur_s=3000.0)
    path = _seed(tmp_path, acts=[act])

    class _Gw:
        def fetch_activity_summary(self, activity_id):
            return {"summaryDTO": {"distance": 9000.0, "duration": 3000.0, "averageHR": 150.0}}

    monkeypatch.setattr(health_check, "_gateway", lambda: _Gw())
    with pytest.raises(AssertionError, match="distance"):
        health_check.check_stored_vs_garmin(_ctx(tmp_path, path))


def test_main_exits_non_zero_when_a_check_fails(tmp_path, monkeypatch, capsys):
    path = _seed(tmp_path)
    monkeypatch.setattr(
        health_check,
        "CHECKS",
        [("boom", lambda ctx: (_ for _ in ()).throw(RuntimeError("kaboom")))],
    )
    rc = health_check.main(["--db", str(path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL boom: RuntimeError: kaboom" in out


def test_main_exits_zero_when_every_check_passes(tmp_path, monkeypatch, capsys):
    path = _seed(tmp_path)
    monkeypatch.setattr(health_check, "CHECKS", [("ok", lambda ctx: "fine")])
    rc = health_check.main(["--db", str(path)])
    assert rc == 0
    assert "PASS ok: fine" in capsys.readouterr().out


def test_main_skips_checks_by_name(tmp_path, monkeypatch, capsys):
    path = _seed(tmp_path)
    monkeypatch.setattr(
        health_check,
        "CHECKS",
        [("a", lambda ctx: "fine"), ("b", lambda ctx: "fine")],
    )
    rc = health_check.main(["--db", str(path), "--only", "a"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS a" in out
    assert "PASS b" not in out


def test_main_prints_a_timestamped_summary_line(tmp_path, monkeypatch, capsys):
    """The summary line is what the dashboard reads out of logs/health.log."""
    path = _seed(tmp_path)
    monkeypatch.setattr(health_check, "CHECKS", [("ok", lambda ctx: "fine")])
    rc = health_check.main(["--db", str(path)])
    out = capsys.readouterr().out
    assert rc == 0
    line = out.strip().splitlines()[-1]
    assert line.startswith("health check: ")
    stamp, status = line[len("health check: ") :].rsplit(" ", 1)
    assert status == "PASS"
    assert len(stamp) == len("2026-10-21 06:30:05")


def test_main_summary_line_says_fail_when_a_check_fails(tmp_path, monkeypatch, capsys):
    path = _seed(tmp_path)
    monkeypatch.setattr(
        health_check,
        "CHECKS",
        [("boom", lambda ctx: (_ for _ in ()).throw(RuntimeError("kaboom")))],
    )
    rc = health_check.main(["--db", str(path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert out.strip().splitlines()[-1].endswith(" FAIL")
