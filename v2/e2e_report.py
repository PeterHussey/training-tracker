#!/usr/bin/env python3
"""E2E metric report: run the full measurement pipeline and report every metric
for a recent period. Usage (from v2/):

    ../.venv/bin/python e2e_report.py [--data auto|live|offline] [--since YYYY-MM-DD]
        [--days N] [--fetch-from YYYY-MM-DD] [--fetch-to YYYY-MM-DD] [--out DB]

--data auto attempts a live Garmin fetch (tokenstore migration or op/env creds)
and falls back to frozen fixtures when auth/network fails. Exit code 0 = all E2E
assertions passed; 1 = a check failed. The DB lives in a temp dir unless --out
is given, so nothing is written into the repo.
"""
import argparse
import io
import json
import re
import sys
import tempfile
from collections import OrderedDict
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from gateway import GarminGateway
from metrics import racepredict, threshold
from normalize import from_summary
from pipeline import run_pipeline
from profile import default_profile
from store import MetricStore

WEEK_RE = re.compile(r"(\d{4})-W(\d{1,2})")
DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
DEFAULT_FIXTURES = Path(__file__).parent / "tests" / "fixtures"
EXPECTED_METRICS = [
    "volume.distance_total", "load.banister", "load.edwards",
    "pmc.ctl", "pmc.atl", "pmc.tsb", "fitness.vo2max", "load.cs_approx",
]
CONDITIONAL_METRICS = {
    "load.acwr": "requires a >=28d outdoor-running span for the chronic window",
    "load.acwr_pct": "requires a >=28d outdoor-running span for the chronic window",
    "load.lt_hr": "requires a measured lactate threshold in the payload (live LT record was empty)",
    "load.lt_pace": "requires a measured lactate threshold in the payload (live LT record was empty)",
    "race_5k": "requires non-null race predictions in the payload",
    "race_10k": "requires non-null race predictions in the payload",
    "race_half": "requires non-null race predictions in the payload",
    "race_full": "requires non-null race predictions in the payload",
}


def week_start(label: str, monday: bool = True) -> date | None:
    """ISO-week label -> Monday date. Returns None for non-week labels."""
    m = WEEK_RE.match(label.strip())
    if not m:
        return None
    try:
        return date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
    except (ValueError, TypeError):
        return None


def in_period(row_date: str, since: date) -> bool:
    """True when a row's date falls on/after `since`.

    Daily rows compare by calendar date; ISO-week rows are included when their
    week end (Sunday) is >= since.
    """
    if DAY_RE.fullmatch(row_date):
        return date.fromisoformat(row_date) >= since
    ws = week_start(row_date)
    if ws is not None:
        return ws + timedelta(days=6) >= since
    return False


def filter_period(rows: list[dict], since: date) -> list[dict]:
    return [r for r in rows if in_period(str(r["date"]), since)]


def gather_metrics(store: MetricStore) -> dict[str, list[dict]]:
    cur = store.conn.execute("SELECT DISTINCT metric FROM metric_series ORDER BY metric")
    return OrderedDict((row[0], store.read_metric(row[0])) for row in cur.fetchall())


def running_span_days(activities) -> int | None:
    running_days = sorted({a.date for a in activities if a.sport == "running"})
    if not running_days:
        return None
    return (running_days[-1] - running_days[0]).days


def e2e_checks(activities, metrics: dict[str, list[dict]], metrics_written: int,
               lt_payload: dict | None, race_payload: dict | None) -> list[str]:
    failures: list[str] = []
    if metrics_written <= 0:
        failures.append("no metrics written")
    for name in EXPECTED_METRICS:
        if not metrics.get(name):
            failures.append(f"missing metric {name}")
    running_days = {a.date for a in activities if a.sport == "running"}
    for name in ("load.banister", "load.edwards"):
        for row in metrics.get(name, []):
            d = pd.Timestamp(row["date"]).date()
            if d not in running_days:
                failures.append(f"{name} row on {row['date']} has no outdoor running day")
    span = running_span_days(activities)
    acwr_present = bool(metrics.get("load.acwr"))
    if acwr_present != (span is not None and span >= 28):
        failures.append(f"load.acwr presence ({acwr_present}) contradicts running span {span}")
    if lt_payload:
        lt = threshold.parse_lt(lt_payload)
        if lt.hr is not None and lt.date and not metrics.get("load.lt_hr"):
            failures.append("missing load.lt_hr despite lt payload")
    if race_payload:
        preds = racepredict.parse_predictions(race_payload)
        if any(v is not None for v in preds.values()) and not metrics.get("race_5k"):
            failures.append("missing race_5k despite race payload")
    return failures


def _hhmm(value: float) -> str:
    total = int(round(value))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt(value: float, params: dict) -> str:
    unit = params.get("unit")
    if unit == "km":
        return f"{value:.2f} km"
    if unit == "m":
        return f"{value:.0f} m"
    if unit == "m/km":
        return f"{value:.1f} m/km"
    if unit in ("m/s", "m_s") or "m/s" in str(unit):
        return f"{value:.2f} m/s"
    if unit == "s":
        return _hhmm(value)
    if unit == "bpm":
        return f"{value:.0f} bpm"
    if unit == "fraction":
        return f"{value * 100:.1f}%"
    return f"{value:.2f}"


def render(store: MetricStore, activities, metrics, meta, since, failures, out) -> None:
    span = running_span_days(activities) or 0
    running_days = sorted({a.date for a in activities if a.sport == "running"})
    min_d = min((a.date for a in activities), default=None)
    max_d = max((a.date for a in activities), default=None)
    print("==", "E2E metric report", "=" * 20, file=out)
    print(f"data source : {meta['source']}   auth: {meta.get('auth', 'n/a')}", file=out)
    print(f"activities  : {len(activities)}   running days: {len(running_days)}   "
          f"running span: {span}d   data span: {min_d}..{max_d}", file=out)
    print(f"period      : >= {since} (ISO-week rows included by week end)", file=out)
    print("", file=out)
    for name, rows in metrics.items():
        shown = filter_period(rows, since)
        print(f"--- {name} ({len(shown)}/{len(rows)} in period) ---", file=out)
        if not shown:
            print("    (no rows in period)", file=out)
        for r in shown[-10:]:
            params = _safe_json(r.get("params"))
            flags = _safe_json(r.get("flags"))
            print(f"    {r['date']}  {_fmt(r['value'], params):<14} "
                  f"source={r.get('source')}"
                  + (f"  params={r.get('params')}" if params else "")
                  + (f"  flags={r.get('flags')}" if flags else ""), file=out)
        print("", file=out)
    for name, reason in CONDITIONAL_METRICS.items():
        if name not in metrics:
            print(f"--- {name} (no rows) ---", file=out)
            print(f"    (conditional: {reason})", file=out)
            print("", file=out)
    if failures:
        print("E2E checks: FAIL", file=out)
        for f in failures:
            print(f"  - {f}", file=out)
    else:
        print("E2E checks: PASS", file=out)


def _safe_json(text) -> dict:
    if not text:
        return {}
    try:
        d = json.loads(text)
        return d if isinstance(d, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def load_data(mode: str, fixtures: Path = DEFAULT_FIXTURES,
              fetch_from: str | None = None, fetch_to: str | None = None):
    """Return (activities, lt_payload, race_payload, meta) for the chosen source."""
    if mode == "offline":
        acts = [from_summary(a) for a in json.loads((fixtures / "activities_sample.json").read_text())]
        lt = json.loads((fixtures / "lactate_threshold.json").read_text())
        race = json.loads((fixtures / "race_predictions.json").read_text())
        return acts, lt, race, {"source": "fixtures", "auth": "n/a"}
    if mode == "live":
        return _load_live(fetch_from, fetch_to)
    try:
        return _load_live(fetch_from, fetch_to, cache_dir=Path("cache/e2e_cache"))
    except Exception as exc:
        print(f"[e2e] live fetch failed ({exc.__class__.__name__}: {exc}); falling back to fixtures",
              file=sys.stderr)
        return load_data("offline", fixtures)


def _load_live(fetch_from: str | None, fetch_to: str | None, cache_dir=Path("cache/e2e_cache")):
    gw = GarminGateway(cache_dir=cache_dir)
    start = fetch_from or (date.today() - timedelta(days=90)).isoformat()
    end = fetch_to or date.today().isoformat()
    raw = gw.fetch_activities(start, end)
    if not raw:
        raise RuntimeError("live fetch returned no activities")
    acts = [from_summary(a) for a in raw]
    lt = gw.fetch_lactate_threshold() or {}
    race = gw.fetch_race_predictions() or {}
    return acts, lt, race, {"source": "live", "auth": gw.auth_path}


def run_report(activities, lt_payload, race_payload, meta, since: date,
               out=None, out_db: str | None = None) -> int:
    import os
    out = out if out is not None else sys.stdout
    tmp_db = out_db
    cleanup = False
    if tmp_db is None:
        fd, tmp_db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        cleanup = True
    try:
        result = run_pipeline(activities, default_profile(age=40, hrrest=60, sex="M"),
                              tmp_db, lt_payload=lt_payload, race_payload=race_payload)
        store = MetricStore(tmp_db)
        metrics = gather_metrics(store)
        failures = e2e_checks(activities, metrics, result["metrics_written"],
                              lt_payload, race_payload)
        render(store, activities, metrics, meta, since, failures, out)
        store.close()
        return 1 if failures else 0
    finally:
        if cleanup:
            for p in (tmp_db,):
                try:
                    Path(p).unlink()
                except FileNotFoundError:
                    pass


def _main(argv: list[str] | None = None, out=None) -> int:
    import os
    parser = argparse.ArgumentParser(description="E2E metric report for a recent period")
    parser.add_argument("--data", choices=["auto", "live", "offline"], default="auto",
                        help="data source (default: live with fixture fallback)")
    parser.add_argument("--since", type=str, default=None,
                        help="report cutoff YYYY-MM-DD (default: trailing --days from data max)")
    parser.add_argument("--days", type=int, default=28, help="trailing days for --since")
    parser.add_argument("--fetch-from", type=str, default=None)
    parser.add_argument("--fetch-to", type=str, default=None)
    parser.add_argument("--out", type=str, default=None, help="persist DB at path")
    parser.add_argument("--fixtures", type=str, default=str(DEFAULT_FIXTURES))
    args = parser.parse_args(argv)

    acts, lt, race, meta = load_data(args.data, Path(args.fixtures),
                                     args.fetch_from, args.fetch_to)
    max_d = max(a.date for a in acts)
    since = date.fromisoformat(args.since) if args.since else max_d - timedelta(days=args.days)
    print(f"[e2e] source={meta['source']} activities={len(acts)} period>= {since}",
          file=out or sys.stderr)
    out_buf = io.StringIO()
    rc = run_report(acts, lt, race, meta, since, out=out_buf, out_db=args.out)
    (out or sys.stdout).write(out_buf.getvalue())
    return rc


if __name__ == "__main__":
    sys.exit(_main())