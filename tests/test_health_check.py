"""Tests for health_check.py — the checks, and the CLI that runs them."""

import os
import sys
import time
from datetime import date, datetime, timedelta

import pytest
import requests

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


def _spec_text(*keys: str) -> str:
    """A spec with the registry section the health check parses."""
    return "## 8. Registry metric keys\n\n```\n" + "\n".join(keys) + "\n```\n"


def test_spec_check_fails_for_an_undocumented_key(tmp_path):
    path = _seed(tmp_path, metrics=[("zzz_unknown.metric", "2026-01-01", 1.0)])
    ctx = _ctx(tmp_path, path, spec_text=_spec_text("volume.distance_total_daily"))
    with pytest.raises(AssertionError, match="zzz_unknown.metric"):
        health_check.check_spec_covers_keys(ctx)


def test_spec_check_passes_when_every_key_is_documented(tmp_path):
    path = _seed(tmp_path, metrics=[("volume.distance_total_daily", "2026-01-01", 1.0)])
    ctx = _ctx(tmp_path, path, spec_text=_spec_text("volume.distance_total_daily"))
    assert "1 stored keys documented" in health_check.check_spec_covers_keys(ctx)


def test_spec_check_fails_when_the_registry_section_is_missing(tmp_path):
    path = _seed(tmp_path, metrics=[("volume.distance_total_daily", "2026-01-01", 1.0)])
    ctx = _ctx(tmp_path, path, spec_text="no registry section here")
    with pytest.raises(AssertionError, match="missing from §8"):
        health_check.check_spec_covers_keys(ctx)


def test_the_spec_lists_every_key_the_pipeline_emits_for_the_fixture():
    """A metric whose key is not registered in §8 fails the nightly check, so
    the fixture pins the registry against the pipeline itself."""
    import json
    from pathlib import Path
    from profile import default_profile

    from normalize import from_summary
    from pipeline import compute_metric_rows

    fixtures = Path(__file__).parent / "fixtures"
    acts = [from_summary(a) for a in json.loads((fixtures / "activities_sample.json").read_text())]
    rows = compute_metric_rows(
        acts,
        default_profile(age=40, hrrest=60, sex="M"),
        json.loads((fixtures / "lactate_threshold.json").read_text()),
        json.loads((fixtures / "race_predictions.json").read_text()),
    )
    undocumented = sorted(
        {r["metric"] for r in rows} - health_check._spec_keys(health_check.SPEC_PATH)
    )
    assert undocumented == [], f"not in the spec's registry: {undocumented}"


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


class _Resp:
    """Minimal stand-in for a requests response."""

    def __init__(self, status_code: int = 200, text: str = "ok"):
        self.status_code = status_code
        self.text = text


def test_dashboard_check_passes_when_the_service_is_healthy(tmp_path, monkeypatch):
    monkeypatch.setattr(health_check.requests, "get", lambda url, timeout=None: _Resp())
    detail = health_check.check_dashboard(_ctx(tmp_path, _seed(tmp_path)))
    assert "-> ok" in detail


def test_dashboard_check_fails_when_the_service_is_unreachable(tmp_path, monkeypatch):
    def boom(url, timeout=None):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(health_check.requests, "get", boom)
    with pytest.raises(AssertionError, match="unreachable"):
        health_check.check_dashboard(_ctx(tmp_path, _seed(tmp_path)))


def test_dashboard_check_fails_when_the_service_is_not_healthy(tmp_path, monkeypatch):
    monkeypatch.setattr(health_check.requests, "get", lambda url, timeout=None: _Resp(500, "boom"))
    with pytest.raises(AssertionError, match="HTTP 500"):
        health_check.check_dashboard(_ctx(tmp_path, _seed(tmp_path)))

    monkeypatch.setattr(
        health_check.requests, "get", lambda url, timeout=None: _Resp(200, "not ok")
    )
    with pytest.raises(AssertionError, match="said"):
        health_check.check_dashboard(_ctx(tmp_path, _seed(tmp_path)))


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


# --- the scheduled refresh must actually fire ---


def test_refresh_schedule_check_passes_when_the_job_ran_recently(tmp_path):
    log = tmp_path / "refresh.log"
    log.write_text("refresh: 0 new activities, 38653 metric rows written\n")
    ctx = _ctx(tmp_path, _seed(tmp_path))
    ctx.refresh_log = log
    assert "refresh" in health_check.check_refresh_schedule(ctx)


def test_refresh_schedule_check_fails_when_the_log_is_stale(tmp_path):
    log = tmp_path / "refresh.log"
    log.write_text("refresh: 0 new activities\n")
    old = time.time() - 3 * 24 * 3600
    os.utime(log, (old, old))
    ctx = _ctx(tmp_path, _seed(tmp_path))
    ctx.refresh_log = log
    with pytest.raises(AssertionError, match="scheduled refresh"):
        health_check.check_refresh_schedule(ctx)


def test_refresh_schedule_check_fails_when_there_is_no_log(tmp_path):
    ctx = _ctx(tmp_path, _seed(tmp_path))
    ctx.refresh_log = tmp_path / "missing.log"
    with pytest.raises(AssertionError, match="no scheduled refresh log"):
        health_check.check_refresh_schedule(ctx)


def test_refresh_schedule_check_fails_when_a_refresh_is_wedged(tmp_path):
    """A start with no completion and 2 hours on the clock is not a slow run."""
    log = tmp_path / "refresh.log"
    started = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    log.write_text(f"refresh: 0 new activities, 1 metric rows written\n[refresh] start {started}\n")
    ctx = _ctx(tmp_path, _seed(tmp_path))
    ctx.refresh_log = log
    with pytest.raises(AssertionError, match="has not finished"):
        health_check.check_refresh_schedule(ctx)


def test_refresh_schedule_check_passes_while_a_refresh_is_running(tmp_path):
    log = tmp_path / "refresh.log"
    started = (datetime.now() - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
    log.write_text(f"refresh: 0 new activities, 1 metric rows written\n[refresh] start {started}\n")
    ctx = _ctx(tmp_path, _seed(tmp_path))
    ctx.refresh_log = log
    assert "has been running" in health_check.check_refresh_schedule(ctx)


def test_refresh_schedule_check_sees_the_completed_run_not_the_start(tmp_path):
    log = tmp_path / "refresh.log"
    started = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    log.write_text(
        f"[refresh] start {started}\n"
        "[refresh] backup written to /tmp/x.sqlite\n"
        "refresh: 2 new activities, 10 metric rows written\n"
    )
    ctx = _ctx(tmp_path, _seed(tmp_path))
    ctx.refresh_log = log
    assert "last scheduled refresh" in health_check.check_refresh_schedule(ctx)


def test_main_says_not_installed_when_there_is_no_store(tmp_path, monkeypatch, capsys):
    """A fresh clone has no data/training.sqlite; that is not seven failures."""
    monkeypatch.setattr(health_check, "LOGS_DIR", tmp_path)
    rc = health_check.main(["--db", str(tmp_path / "data" / "training.sqlite")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "not installed yet" in out
    assert out.strip().splitlines()[-1].endswith(" SKIP")


# --- the scheduled job must stay loaded ---


class _Proc:
    def __init__(self, returncode: int = 0):
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


@pytest.mark.skipif(sys.platform != "darwin", reason="launchd is macOS-only")
def test_refresh_job_check_passes_when_launchd_knows_the_job(tmp_path, monkeypatch):
    monkeypatch.setattr(health_check.subprocess, "run", lambda *a, **k: _Proc(0))
    assert "loaded" in health_check.check_refresh_job(_ctx(tmp_path, _seed(tmp_path)))


@pytest.mark.skipif(sys.platform != "darwin", reason="launchd is macOS-only")
def test_refresh_job_check_fails_when_the_job_is_not_loaded(tmp_path, monkeypatch):
    monkeypatch.setattr(health_check.subprocess, "run", lambda *a, **k: _Proc(113))
    with pytest.raises(AssertionError, match="not loaded"):
        health_check.check_refresh_job(_ctx(tmp_path, _seed(tmp_path)))


# --- the health log must stay bounded ---


def test_trim_log_keeps_the_last_lines(tmp_path):
    log = tmp_path / "health.log"
    log.write_text("\n".join(f"line {i}" for i in range(10)) + "\n")
    assert health_check.trim_log(log, keep=3) == 7
    assert log.read_text().splitlines() == ["line 7", "line 8", "line 9"]


def test_trim_log_leaves_a_short_log_alone(tmp_path):
    log = tmp_path / "health.log"
    log.write_text("one\ntwo\n")
    assert health_check.trim_log(log, keep=10) == 0
    assert log.read_text() == "one\ntwo\n"


def test_trim_log_ignores_a_missing_file(tmp_path):
    assert health_check.trim_log(tmp_path / "missing.log", keep=3) == 0


def test_main_trims_every_log_and_preserves_the_refresh_mtime(tmp_path, monkeypatch):
    # launchd appends to these files forever: the dashboard's error log took
    # fd errors to 89 KB in two days. The refresh log is trimmed too, but its
    # mtime is the signal the refresh-schedule check reads, so the stamp is
    # restored — a trim must not make a dead refresh look fresh.
    dash = tmp_path / "dashboard.err.log"
    dash.write_text("\n".join(f"line {i}" for i in range(10)) + "\n")
    refresh = tmp_path / "refresh.log"
    refresh.write_text("\n".join(f"line {i}" for i in range(10)) + "\n")
    stamp = 1_700_000_000.0
    os.utime(refresh, (stamp, stamp))
    health = tmp_path / "health.log"
    health.write_text("health check: PASS\n")
    # Every log the check touches lives under LOGS_DIR, so redirecting that
    # one constant keeps the test off the real logs/.
    monkeypatch.setattr(health_check, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(health_check, "HEALTH_LOG_MAX_LINES", 3)
    monkeypatch.setattr(health_check, "CHECKS", [("ok", lambda ctx: "fine")])
    health_check.main(["--db", str(_seed(tmp_path))])
    assert dash.read_text().splitlines() == ["line 7", "line 8", "line 9"]
    assert refresh.read_text().splitlines() == ["line 7", "line 8", "line 9"]
    assert refresh.stat().st_mtime == pytest.approx(stamp)
    assert health.read_text() == "health check: PASS\n"


def test_main_trims_the_health_log(tmp_path, monkeypatch):
    log = tmp_path / "health.log"
    log.write_text("\n".join(f"line {i}" for i in range(10)) + "\n")
    monkeypatch.setattr(health_check, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(health_check, "HEALTH_LOG_MAX_LINES", 3)
    monkeypatch.setattr(health_check, "CHECKS", [("ok", lambda ctx: "fine")])
    health_check.main(["--db", str(_seed(tmp_path))])
    assert log.read_text().splitlines() == ["line 7", "line 8", "line 9"]
