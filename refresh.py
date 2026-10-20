"""Scheduled Garmin refresh — CLI entry point for the launchd job.

Fetches new activities from Garmin, writes them to the local store and
recomputes the metric series: the same steps as the dashboard's
"Refresh from Garmin" button, without Streamlit.

Run: `.venv/bin/python refresh.py`
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from profile import default_profile

from gateway import FETCH_DAYS, GarminGateway, compute_fetch_start
from normalize import from_summary
from pipeline import persist_session_metrics
from store import MetricStore, default_db_path

CACHE_DIR = Path(__file__).resolve().parent / "cache" / "app_cache"


def main() -> int:
    path = default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    store = MetricStore(path)
    try:
        return _refresh(store)
    except Exception as exc:
        # The launchd job reads this line in logs/refresh.err.log — a stack
        # trace there buries the cause and reads like a crash.
        print(f"[refresh] Garmin fetch failed ({exc.__class__.__name__}: {exc})", file=sys.stderr)
        return 1
    finally:
        store.close()


def _refresh(store: MetricStore) -> int:
    end = date.today()
    start = compute_fetch_start(end, store.latest_activity_date(), FETCH_DAYS)
    gw = GarminGateway(cache_dir=CACHE_DIR)
    raw = gw.fetch_activities(start.isoformat(), end.isoformat())
    acts = [from_summary(a) for a in raw]
    if acts:
        store.save_activities(acts)

    profile = store.load_runner_profile() or default_profile()
    # Trend payloads are always full-window daily lists, not the incremental
    # activities window — a short window would give the charts 1-2 points.
    trend_start = end - timedelta(days=FETCH_DAYS)
    lt = gw.fetch_lactate_threshold() or {}
    race = gw.fetch_race_predictions_trend(trend_start.isoformat(), end.isoformat()) or []
    vo2 = gw.fetch_vo2max_trend(trend_start.isoformat(), end.isoformat()) or []
    rows = persist_session_metrics(store, store.load_activities(), profile, lt, race, vo2)
    print(f"refresh: {len(acts)} new activities, {rows} metric rows written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
