#!/usr/bin/env python3
"""Health check — the probes a status review would otherwise re-run by hand.

Run: `.venv/bin/python health_check.py [--db PATH] [--only NAME] [--skip NAME]`

Prints one PASS/FAIL line per check and exits 1 if any check fails, so it can
be run from launchd and read in a log. The last line is a timestamped summary
("health check: 2026-10-21 06:30:05 PASS"), which the dashboard surfaces in
its sidebar so a silent failure is visible without reading the log.
Every check is non-destructive: the
ones that write do so against a COPY of the store, taken with sqlite3's
backup API (the live DB is in WAL mode, so a plain file copy is not a
consistent snapshot).

The checks are the ones the 2026-10-20 status review had to write as
throwaway probe scripts; they are here so the next review starts from a known
baseline instead of rediscovering it. The last one probes the dashboard
service itself, so a dead dashboard is a visible FAIL rather than a silent one.
One check reads the refresh job's own log: only that proves the daily timer
fired, which no manual kickstart can.
"""

import argparse
import sqlite3
import sys
import tempfile
import threading
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from profile import default_profile

import pandas as pd
import requests

import refresh
from gateway import GarminGateway
from metrics import volume
from store import MetricStore, default_db_path

REPO = Path(__file__).resolve().parent
SPEC_PATH = REPO / "docs" / "v2" / "metrics-spec.md"
CACHE_DIR = REPO / "cache" / "app_cache"
VOLUME_DAILY = "volume.distance_total_daily"
# The launchd dashboard plist pins this address and port.
DASHBOARD_HEALTH_URL = "http://127.0.0.1:8501/_stcore/health"
DASHBOARD_TIMEOUT_S = 5
LOGS_DIR = REPO / "logs"
# The refresh job's log mtime is the signal the refresh-schedule check reads.
REFRESH_LOG = LOGS_DIR / "refresh.log"
HEALTH_LOG_MAX_LINES = 500
# The refresh job runs daily at 05:00 and this check at 06:30, so a successful
# run is a couple of hours old by the time we look. A day of slack covers a
# machine that was asleep at 05:00.
REFRESH_MAX_AGE_H = 26


@dataclass
class Context:
    db_path: Path
    spec_path: Path
    refresh_log: Path = REFRESH_LOG


def _copy_db(src_path: Path) -> Path:
    """A consistent copy of the store in a temp dir (backup API, not a file copy)."""
    tmp = Path(tempfile.mkdtemp(prefix="tt-health-"))
    dst_path = tmp / "copy.sqlite"
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dst_path


def _gateway() -> GarminGateway:
    return GarminGateway(cache_dir=CACHE_DIR)


# --- the checks -----------------------------------------------------------


def check_store_mode(ctx: Context) -> str:
    """The live store must be in WAL with a busy timeout (criterion 12)."""
    store = MetricStore(ctx.db_path)
    try:
        mode = store.conn.execute("PRAGMA journal_mode").fetchone()[0]
        timeout = store.conn.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        store.close()
    if str(mode).lower() != "wal":
        raise AssertionError(f"journal_mode is {mode!r}, expected 'wal'")
    if timeout <= 0:
        raise AssertionError(f"busy_timeout is {timeout}, expected > 0")
    return f"journal_mode={mode} busy_timeout={timeout}"


def check_monthly_rollup(ctx: Context) -> str:
    """The current month must have a monthly volume bucket (2026-10-20 review)."""
    store = MetricStore(ctx.db_path)
    try:
        rows = store.conn.execute(
            "SELECT date, value FROM metric_series WHERE metric = ? ORDER BY date",
            (VOLUME_DAILY,),
        ).fetchall()
    finally:
        store.close()
    if not rows:
        raise AssertionError(f"no {VOLUME_DAILY} rows in the store")
    series = pd.Series(
        [v for _, v in rows], index=pd.to_datetime([d for d, _ in rows]), dtype=float
    )
    month = date.today().replace(day=1)
    days = [v for d, v in rows if d.startswith(month.isoformat()[:7])]
    if not days:
        return f"no activity yet in {month:%Y-%m} — no bucket expected"
    monthly = volume.resample_agg(series, "MS")
    label = pd.Timestamp(month)
    if label not in monthly.index:
        raise AssertionError(f"no monthly bucket for {month:%Y-%m} (the dropped-month regression)")
    expected = sum(days)
    if abs(monthly.loc[label] - expected) > 1e-6:
        raise AssertionError(f"{month:%Y-%m} bucket is {monthly.loc[label]}, expected {expected}")
    return f"{month:%Y-%m} bucket {monthly.loc[label]:.3f} over {len(days)} day(s)"


def check_spec_covers_families(ctx: Context) -> str:
    """Every stored metric family must be documented in the metric spec."""
    store = MetricStore(ctx.db_path)
    try:
        families = {
            m.split(".")[0]
            for (m,) in store.conn.execute("SELECT DISTINCT metric FROM metric_series")
        }
    finally:
        store.close()
    spec = ctx.spec_path.read_text()
    missing = sorted(f for f in families if f not in spec)
    if missing:
        raise AssertionError(f"undocumented in {ctx.spec_path.name}: {', '.join(missing)}")
    return f"{len(families)} families documented"


def check_concurrency(ctx: Context) -> str:
    """A reader must not be locked out by a writer (criterion 12), and
    concurrent readers on one store must each get a complete result set."""
    copy_path = _copy_db(ctx.db_path)
    writer = MetricStore(copy_path)
    try:
        writer.conn.execute("BEGIN IMMEDIATE")
        writer.conn.execute(
            "INSERT INTO metric_series (metric, date, value, source) "
            "VALUES ('health.check', '2020-01-01', 0, 'health')"
        )
        reader = MetricStore(copy_path)
        try:
            reader.load_activities()
        finally:
            reader.close()
    finally:
        writer.conn.rollback()
        writer.close()
    _check_concurrent_readers(copy_path)
    return "reader proceeded during a write; 4 concurrent readers saw complete rows"


def _check_concurrent_readers(path: Path) -> None:
    """Concurrent readers on one store must not corrupt each other's rows.

    One sqlite3.Connection shared across threads interleaves its
    execute()/fetchall() and returns duplicated or None rows — the bug that
    made load_runner_profile raise KeyError under Streamlit's threaded reruns.
    """
    store = MetricStore(path)
    store.save_runner_profile(default_profile(age=40, hrrest=60, sex="M"))
    failures: list[str] = []

    def reader(tag: str) -> None:
        for _ in range(20):
            try:
                p = store.load_runner_profile()
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{tag}: {type(exc).__name__}: {exc}")
                return
            if p is None or p.hrmax != 180 or p.hrrest != 60 or p.units != "metric":
                failures.append(f"{tag}: incomplete profile {p}")
                return

    try:
        threads = [threading.Thread(target=reader, args=(f"r{i}",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        store.close()
    if failures:
        raise AssertionError(f"concurrent readers corrupted the result set: {failures[:3]}")


def check_refresh(ctx: Context) -> str:
    """The refresh path must complete against a copy of the store."""
    copy_path = _copy_db(ctx.db_path)
    store = MetricStore(copy_path)
    try:
        rc = refresh._refresh(store)
        rows = store.conn.execute("SELECT COUNT(*) FROM metric_series").fetchone()[0]
    finally:
        store.close()
    if rc != 0:
        raise AssertionError(f"refresh returned {rc}")
    return f"refresh wrote {rows} metric rows"


def check_refresh_schedule(ctx: Context) -> str:
    """The scheduled refresh must actually fire (criterion 11).

    Every other piece of evidence about the refresh is a manual kickstart;
    only the job's own log proves the timer fired. The refresh writes one
    line to stdout per successful run, so a stale log means no run.
    """
    log = ctx.refresh_log
    if not log.exists():
        raise AssertionError(f"no scheduled refresh log at {log}")
    age_h = (datetime.now() - datetime.fromtimestamp(log.stat().st_mtime)).total_seconds() / 3600
    if age_h > REFRESH_MAX_AGE_H:
        raise AssertionError(
            f"last scheduled refresh was {age_h:.1f}h ago (limit {REFRESH_MAX_AGE_H}h) — {log}"
        )
    return f"last scheduled refresh {age_h:.1f}h ago"


def check_stored_vs_garmin(ctx: Context) -> str:
    """A few stored activities must still match what Garmin reports."""
    store = MetricStore(ctx.db_path)
    try:
        acts = [
            a
            for a in store.load_activities()
            if a.sport == "running" and a.avg_hr is not None and a.distance_m > 0
        ][-3:]
    finally:
        store.close()
    if not acts:
        raise AssertionError("no running activities with HR data to compare")
    gw = _gateway()
    compared = 0
    for a in acts:
        dto = gw.fetch_activity_summary(a.activity_id).get("summaryDTO") or {}
        if not dto:
            continue
        compared += 1
        if abs(float(dto.get("distance") or 0.0) - a.distance_m) > 1.0:
            raise AssertionError(
                f"{a.activity_id}: distance {dto.get('distance')} vs {a.distance_m}"
            )
        if abs(float(dto.get("duration") or 0.0) - a.duration_s) > 1.0:
            raise AssertionError(
                f"{a.activity_id}: duration {dto.get('duration')} vs {a.duration_s}"
            )
        hr = dto.get("averageHR")
        if hr is not None and abs(float(hr) - float(a.avg_hr)) > 1.0:
            raise AssertionError(f"{a.activity_id}: avg HR {hr} vs {a.avg_hr}")
    if not compared:
        raise AssertionError("could not fetch any Garmin summary to compare against")
    return f"{compared} activity(s) match Garmin"


def check_dashboard(ctx: Context) -> str:
    """The dashboard service must be up and healthy (criterion 10).

    A dead dashboard is the one failure nobody would otherwise notice: the
    refresh job still runs, the store still grows, and the app is simply gone.
    """
    try:
        resp = requests.get(DASHBOARD_HEALTH_URL, timeout=DASHBOARD_TIMEOUT_S)
    except requests.RequestException as exc:
        raise AssertionError(f"{DASHBOARD_HEALTH_URL} unreachable: {exc}") from exc
    if resp.status_code != 200:
        raise AssertionError(f"{DASHBOARD_HEALTH_URL} returned HTTP {resp.status_code}")
    body = resp.text.strip()
    if body != "ok":
        raise AssertionError(f"{DASHBOARD_HEALTH_URL} said {body!r}")
    return f"{DASHBOARD_HEALTH_URL} -> ok"


def trim_log(path: Path, keep: int | None = None) -> int:
    """Keep a log bounded: rewrite it with its last `keep` lines.

    launchd appends to these files, so truncating in place is safe — the
    next write lands at the end of the shortened file.
    """
    keep = HEALTH_LOG_MAX_LINES if keep is None else keep
    try:
        if not path.exists():
            return 0
        lines = path.read_text(errors="replace").splitlines()
        if len(lines) <= keep:
            return 0
        path.write_text("\n".join(lines[-keep:]) + "\n")
    except OSError:
        return 0
    return len(lines) - keep


CHECKS = [
    ("store-mode", check_store_mode),
    ("monthly-rollup", check_monthly_rollup),
    ("spec-covers-families", check_spec_covers_families),
    ("concurrency", check_concurrency),
    ("refresh", check_refresh),
    ("refresh-schedule", check_refresh_schedule),
    ("stored-vs-garmin", check_stored_vs_garmin),
    ("dashboard", check_dashboard),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Health check for the training-tracker store")
    parser.add_argument("--db", default=str(default_db_path()))
    parser.add_argument("--only", action="append", default=[], help="run only these checks")
    parser.add_argument("--skip", action="append", default=[], help="skip these checks")
    args = parser.parse_args(argv)

    ctx = Context(db_path=Path(args.db), spec_path=SPEC_PATH, refresh_log=LOGS_DIR / "refresh.log")
    trim_log(LOGS_DIR / "health.log")
    trim_log(LOGS_DIR / "dashboard.log")
    # Never the refresh log: its mtime is the signal the refresh-schedule
    # check reads, and rewriting it would make a dead refresh look fresh.
    trim_log(LOGS_DIR / "dashboard.err.log")
    failures = 0
    for name, fn in CHECKS:
        if args.only and name not in args.only:
            continue
        if name in args.skip:
            continue
        try:
            detail = fn(ctx)
        except Exception as exc:
            failures += 1
            print(f"FAIL {name}: {exc.__class__.__name__}: {exc}")
        else:
            print(f"PASS {name}: {detail}")
    status = "FAIL" if failures else "PASS"
    print(f"health check: {datetime.now():%Y-%m-%d %H:%M:%S} {status}")
    if failures:
        print(f"{failures} check(s) failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
