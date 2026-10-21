#!/usr/bin/env python3
"""Backfill Garmin event types (race tags) from live Garmin data. Usage (from root):

    .venv/bin/python backfill_event_types.py [--db PATH] [--start YYYY-MM-DD]
        [--end YYYY-MM-DD] [--apply]

Re-fetches the **live** Garmin activity list over the store's full date range
and re-reads each payload's `eventType.typeKey` (e.g. "race",
"uncategorized"), reporting every row whose stored `event_type` disagrees —
e.g. races tagged in Garmin Connect after the activity was first ingested
here (no refresh mode revisits a stored activity, so such tags never arrive
on their own). The live list is the only source: the cached
`activity_summary_{id}.json` payloads predate the tagging and would restore
stale values. Garmin is mirrored exactly, downgrades included — a stored
"race" that Garmin now reports as "uncategorized" is planned too, with the
dry run as the review gate.

Default is a dry run: prints the planned updates and exits 0 without touching
the DB. Pass --apply to UPDATE the `event_type` column only (a narrow UPDATE —
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
from normalize import event_type_of
from store import MetricStore


def plan_event_type_updates(
    stored: dict[int, str | None], raw: list[dict]
) -> list[tuple[int, str]]:
    """Pure: (activity_id, event_type) for rows whose stored tag disagrees.

    Rows absent from the fetch, or an absent/id-unknown summary, are skipped.
    An absent/empty/malformed `eventType` in the payload plans nothing — data
    genuinely absent stays absent, and this is the fail-safe against a payload
    that omits the key.
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
        new = event_type_of(summary)
        if new is None:
            continue
        if stored[aid] != new:
            plans.append((aid, new))
    return sorted(plans)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill Garmin event types (race tags) from Garmin"
    )
    parser.add_argument("--db", default=str(Path(__file__).parent / "data" / "training.sqlite"))
    parser.add_argument("--start", default=None, help="default: earliest stored activity")
    parser.add_argument("--end", default=None, help="default: latest stored activity")
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args(argv)

    store = MetricStore(args.db)
    stored = {
        int(aid): et
        for aid, et in store.conn.execute(
            "SELECT activity_id, event_type FROM activities"
        ).fetchall()
    }
    if not stored:
        print("[backfill_event_types] store is empty, nothing to do")
        store.close()
        return 0
    dates = [r[0] for r in store.conn.execute("SELECT activity_date FROM activities").fetchall()]
    start = date.fromisoformat(args.start) if args.start else date.fromisoformat(min(dates))
    end = date.fromisoformat(args.end) if args.end else date.fromisoformat(max(dates))

    try:
        gw = GarminGateway(cache_dir=Path("cache") / "app_cache")
        raw = fetch_range(gw, start, end)
    except Exception as exc:
        print(
            f"[backfill_event_types] Garmin fetch failed ({exc.__class__.__name__}: {exc})",
            file=sys.stderr,
        )
        store.close()
        return 1
    print(
        f"[backfill_event_types] fetched {len(raw)} activities (auth: {gw.auth_path})",
        file=sys.stderr,
    )

    plans = plan_event_type_updates(stored, raw)
    nulls = sorted(aid for aid, et in stored.items() if et is None)
    print(
        f"[backfill_event_types] {len(stored)} stored rows, {len(plans)} event_type updates, "
        f"{len(set(nulls) - {aid for aid, _ in plans})} still NULL after the plan"
    )
    for aid, et in plans:
        print(f"  id={aid} {stored[aid]} -> {et}")
    if not plans:
        store.close()
        return 0
    if not args.apply:
        print("[backfill_event_types] dry run — pass --apply to write")
        store.close()
        return 0

    store.conn.executemany(
        "UPDATE activities SET event_type = ? WHERE activity_id = ?",
        [(et, aid) for aid, et in plans],
    )
    store.conn.commit()
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
