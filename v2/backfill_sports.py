#!/usr/bin/env python3
"""Backfill activity sports from live Garmin data. Usage (from v2/):

    ../.venv/bin/python backfill_sports.py [--db PATH] [--start YYYY-MM-DD]
        [--end YYYY-MM-DD] [--apply]

Re-fetches the Garmin activity list over the store's full date range (in
365-day chunks), recomputes `sport` via normalize.sport_of, and reports every
row whose stored sport disagrees — e.g. strength sessions saved as "cross"
before the strength_training classifier existed.

Default is a dry run: prints the change matrix and exits 0 without touching
the DB. Pass --apply to UPDATE the sport column and regenerate metric_series
(exactly what the dashboard would persist, using the cached trend payloads).
Requires Garmin auth (tokenstore or 1Password/env creds); run it in a
terminal where those work, not in a sandboxed agent session.
"""

import argparse
import json
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from gateway import GarminGateway
from normalize import sport_of
from pipeline import persist_session_metrics
from store import MetricStore

CHUNK_DAYS = 90


def plan_updates(stored: dict[int, str], raw: list[dict]) -> list[tuple[int, str, str]]:
    """Pure: (activity_id, old_sport, new_sport) for rows Garmin disagrees with.

    Rows absent from the fetch, or without a classifiable typeKey, are skipped.
    """
    plans = []
    for summary in raw:
        aid = summary.get("activityId")
        if aid is None or int(aid) not in stored:
            continue
        tk = (summary.get("activityType") or {}).get("typeKey", "")
        if not tk:
            continue
        new = sport_of(summary)
        if stored[int(aid)] != new:
            plans.append((int(aid), stored[int(aid)], new))
    return sorted(plans)


def fetch_range(gw: GarminGateway, start: date, end: date) -> list[dict]:
    """Fetch the activity list in 365-day chunks (oldest first)."""
    out: list[dict] = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS - 1), end)
        print(
            f"[backfill] fetching {chunk_start}..{chunk_end} ...",
            file=sys.stderr,
            flush=True,
        )
        out += gw.fetch_activities(chunk_start.isoformat(), chunk_end.isoformat()) or []
        chunk_start = chunk_end + timedelta(days=1)
    seen: dict[int, dict] = {}
    for a in out:
        if a.get("activityId") is not None:
            seen[int(a["activityId"])] = a
    return list(seen.values())


def load_cached_trends(cache_dir: Path):
    def load(name: str):
        try:
            return json.loads((cache_dir / name).read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def latest(pattern: str):
        try:
            cands = [p for p in cache_dir.glob(pattern) if p.is_file()]
        except OSError:
            return None
        if not cands:
            return None
        cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return load(cands[0].name)

    lt = load("lactate_threshold.json")
    race = latest("race_predictions_trend_*.json") or load("race_predictions.json")
    vo2 = latest("vo2max_trend_*.json")
    return (
        lt,
        race if isinstance(race, (dict, list)) else None,
        vo2 if isinstance(vo2, list) else None,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill activity sports from Garmin")
    parser.add_argument("--db", default=str(Path(__file__).parent / "data" / "training.sqlite"))
    parser.add_argument("--start", default=None, help="default: earliest stored activity")
    parser.add_argument("--end", default=None, help="default: latest stored activity")
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args(argv)

    store = MetricStore(args.db)
    stored = {
        int(aid): sport
        for aid, sport in store.conn.execute("SELECT activity_id, sport FROM activities").fetchall()
    }
    if not stored:
        print("[backfill] store is empty, nothing to do")
        return 0
    dates = [r[0] for r in store.conn.execute("SELECT activity_date FROM activities").fetchall()]
    start = date.fromisoformat(args.start) if args.start else date.fromisoformat(min(dates))
    end = date.fromisoformat(args.end) if args.end else date.fromisoformat(max(dates))

    gw = GarminGateway(cache_dir=Path("cache") / "app_cache")
    raw = fetch_range(gw, start, end)
    print(f"[backfill] fetched {len(raw)} activities (auth: {gw.auth_path})", file=sys.stderr)

    seen_ids = {int(a["activityId"]) for a in raw if a.get("activityId") is not None}
    missing = sorted(set(stored) - seen_ids)
    if missing:
        print(
            f"[backfill] {len(missing)} stored rows outside the fetch window "
            f"(left unverified): {missing[:10]}{'...' if len(missing) > 10 else ''}"
        )
    plans = plan_updates(stored, raw)
    matrix = Counter((old, new) for _, old, new in plans)
    print(f"[backfill] {len(stored)} stored rows, {len(plans)} sport changes:")
    for (old, new), n in sorted(matrix.items()):
        print(f"  {old} -> {new}: {n}")
    for aid, old, new in plans:
        print(f"  id={aid} {old} -> {new}")
    if not plans:
        return 0
    if not args.apply:
        print("[backfill] dry run — pass --apply to write")
        return 0

    store.conn.executemany(
        "UPDATE activities SET sport = ? WHERE activity_id = ?",
        [(new, aid) for aid, _old, new in plans],
    )
    store.conn.commit()
    lt, race, vo2 = load_cached_trends(Path("cache") / "app_cache")
    store.conn.execute("DELETE FROM metric_series")
    store.conn.commit()
    acts = store.load_activities()
    n = persist_session_metrics(store, acts, store.load_runner_profile(), lt, race, vo2)
    print(f"[backfill] applied {len(plans)} sport updates, regenerated {n} metric rows")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
