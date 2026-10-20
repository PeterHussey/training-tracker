#!/usr/bin/env python3
"""Backfill average running cadence from live Garmin data. Usage (from root):

    .venv/bin/python backfill_cadence.py [--db PATH] [--start YYYY-MM-DD]
        [--end YYYY-MM-DD] [--apply]

Re-fetches the **live** Garmin activity list over the store's full date range
and re-reads each payload's `averageRunningCadenceInStepsPerMinute`, reporting
every row whose stored `avg_cadence` disagrees. Cadence is only carried by the
activity-list payload — the per-activity summaryDTO has no cadence field — so
no refresh mode revisits a stored activity and rows ingested before the column
existed stay NULL.

Default is a dry run: prints the planned updates and exits 0 without touching
the DB. Pass --apply to UPDATE the `avg_cadence` column only (a narrow UPDATE —
never `store.save_activities`, whose INSERT OR REPLACE would NULL the already
backfilled RPE/Feel/weather columns the list payload doesn't carry). Requires
Garmin auth (tokenstore or 1Password/env creds); run it in a terminal where
those work, not in a sandboxed agent session.
"""

import argparse
import sys
from datetime import date
from pathlib import Path

from backfill_sports import fetch_range
from gateway import GarminGateway
from normalize import cadence_of
from store import MetricStore


def plan_cadence_updates(
    stored: dict[int, float | None], raw: list[dict]
) -> list[tuple[int, float]]:
    """Pure: (activity_id, avg_cadence) for rows whose stored cadence disagrees.

    Rows absent from the fetch are skipped. A payload with no cadence plans
    nothing — data genuinely absent stays absent, and an existing stored value
    is never blanked.
    """
    plans = []
    for summary in raw:
        aid = summary.get("activityId")
        if aid is None:
            continue
        try:
            aid = int(aid)
        except (TypeError, ValueError):
            continue
        if aid not in stored:
            continue
        new = cadence_of(summary)
        if new is None:
            continue
        if stored[aid] != new:
            plans.append((aid, new))
    return sorted(plans)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill average running cadence from Garmin")
    parser.add_argument("--db", default=str(Path(__file__).parent / "data" / "training.sqlite"))
    parser.add_argument("--start", default=None, help="default: earliest stored activity")
    parser.add_argument("--end", default=None, help="default: latest stored activity")
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args(argv)

    store = MetricStore(args.db)
    stored = {
        int(aid): cad
        for aid, cad in store.conn.execute(
            "SELECT activity_id, avg_cadence FROM activities"
        ).fetchall()
    }
    if not stored:
        print("[backfill_cadence] store is empty, nothing to do")
        store.close()
        return 0
    dates = [r[0] for r in store.conn.execute("SELECT activity_date FROM activities").fetchall()]
    start = date.fromisoformat(args.start) if args.start else date.fromisoformat(min(dates))
    end = date.fromisoformat(args.end) if args.end else date.fromisoformat(max(dates))

    gw = GarminGateway(cache_dir=Path("cache") / "app_cache")
    raw = fetch_range(gw, start, end)
    print(
        f"[backfill_cadence] fetched {len(raw)} activities (auth: {gw.auth_path})",
        file=sys.stderr,
    )

    plans = plan_cadence_updates(stored, raw)
    nulls = sorted(aid for aid, cad in stored.items() if cad is None)
    print(
        f"[backfill_cadence] {len(stored)} stored rows, {len(plans)} avg_cadence updates, "
        f"{len(set(nulls) - {aid for aid, _ in plans})} still NULL after the plan"
    )
    for aid, cad in plans:
        print(f"  id={aid} {stored[aid]} -> {cad}")
    if not plans:
        store.close()
        return 0
    if not args.apply:
        print("[backfill_cadence] dry run — pass --apply to write")
        store.close()
        return 0

    store.conn.executemany(
        "UPDATE activities SET avg_cadence = ? WHERE activity_id = ?",
        [(cad, aid) for aid, cad in plans],
    )
    store.conn.commit()
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
