#!/usr/bin/env python3
"""Backfill activity names from live Garmin data. Usage (from v2/):

    ../.venv/bin/python backfill_names.py [--db PATH] [--start YYYY-MM-DD]
        [--end YYYY-MM-DD] [--apply]

Re-fetches the Garmin activity list over the store's full date range (in
90-day chunks), reads each `activityName`, and reports every row whose stored
name is missing or disagrees — e.g. activities saved before the name column
existed (the repeated-workout comparison feature requires names).

Default is a dry run: prints the planned updates and exits 0 without touching
the DB. Pass --apply to UPDATE the name column. Requires Garmin auth
(tokenstore or 1Password/env creds); run it in a terminal where those work,
not in a sandboxed agent session.
"""

import argparse
import sys
from datetime import date
from pathlib import Path

from backfill_sports import fetch_range
from gateway import GarminGateway
from store import MetricStore


def plan_name_updates(stored: dict[int, str | None], raw: list[dict]) -> list[tuple[int, str]]:
    """Pure: (activity_id, name) for rows whose stored name is missing/wrong.

    Rows absent from the fetch are skipped. A blank/absent activityName in the
    payload means the name stays NULL (no plan entry).
    """
    plans = []
    for summary in raw:
        aid = summary.get("activityId")
        if aid is None or int(aid) not in stored:
            continue
        name = summary.get("activityName")
        if not name:
            continue
        if stored[int(aid)] != name:
            plans.append((int(aid), name))
    return sorted(plans)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill activity names from Garmin")
    parser.add_argument("--db", default=str(Path(__file__).parent / "data" / "training.sqlite"))
    parser.add_argument("--start", default=None, help="default: earliest stored activity")
    parser.add_argument("--end", default=None, help="default: latest stored activity")
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args(argv)

    store = MetricStore(args.db)
    stored = {
        int(aid): name
        for aid, name in store.conn.execute("SELECT activity_id, name FROM activities").fetchall()
    }
    if not stored:
        print("[backfill_names] store is empty, nothing to do")
        store.close()
        return 0
    dates = [r[0] for r in store.conn.execute("SELECT activity_date FROM activities").fetchall()]
    start = date.fromisoformat(args.start) if args.start else date.fromisoformat(min(dates))
    end = date.fromisoformat(args.end) if args.end else date.fromisoformat(max(dates))

    gw = GarminGateway(cache_dir=Path("cache") / "app_cache")
    raw = fetch_range(gw, start, end)
    print(
        f"[backfill_names] fetched {len(raw)} activities (auth: {gw.auth_path})",
        file=sys.stderr,
    )

    plans = plan_name_updates(stored, raw)
    missing = sorted(aid for aid, name in stored.items() if name is None)
    print(
        f"[backfill_names] {len(stored)} stored rows, {len(plans)} name updates, "
        f"{len(set(missing) - {aid for aid, _ in plans})} still missing names"
    )
    for aid, name in plans:
        print(f"  id={aid} {stored[aid]} -> {name}")
    if not plans:
        store.close()
        return 0
    if not args.apply:
        print("[backfill_names] dry run — pass --apply to write")
        store.close()
        return 0

    store.conn.executemany(
        "UPDATE activities SET name = ? WHERE activity_id = ?",
        [(name, aid) for aid, name in plans],
    )
    store.conn.commit()
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
