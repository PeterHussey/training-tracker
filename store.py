"""SQLite persistence for the measurement layer."""

import contextlib
import json
import os
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path
from profile import RunnerProfile

from normalize import Activity

SCHEMA = """
CREATE TABLE IF NOT EXISTS runner_profile (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  meta  TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS activities (
  activity_id        INTEGER PRIMARY KEY,
  activity_date      TEXT NOT NULL,
  sport              TEXT NOT NULL,
  distance_m         REAL NOT NULL DEFAULT 0,
  duration_s         REAL NOT NULL DEFAULT 0,
  elapsed_s          REAL NOT NULL DEFAULT 0,
  ele_gain_m         REAL,
  vo2max             REAL,
  avg_hr             REAL,
  max_hr             REAL,
  aerobic_te         REAL,
  anaerobic_te       REAL,
  avg_speed          REAL,
  fastest_split_1609 REAL,
  avg_cadence        REAL,
   zone_s             TEXT NOT NULL DEFAULT '{}',
   route              TEXT NOT NULL DEFAULT '{}',
   name               TEXT,
   has_intervals      INTEGER NOT NULL DEFAULT 0,
   rpe_10             REAL,
   feel               TEXT,
   wx_temp            REAL,
   wx_feels           REAL,
   wx_humidity        REAL,
   wx_wind            REAL,
   wx_wind_dir        TEXT,
   wx_desc            TEXT,
   wx_unit            TEXT,
   event_type         TEXT
);
CREATE TABLE IF NOT EXISTS metric_series (
  metric TEXT NOT NULL,
  date   TEXT NOT NULL,
  value  REAL NOT NULL,
  source TEXT NOT NULL,
  params TEXT NOT NULL DEFAULT '{}',
  flags  TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (metric, date)
);
CREATE INDEX IF NOT EXISTS idx_metric ON metric_series (metric, date);
"""

# Schema migrations, applied in order when a store is opened. Each entry is
# (version, method name); the version is written to PRAGMA user_version once
# the migration has run, so an already-migrated store does no work on later
# opens. Never edit a shipped migration — add a new one instead.
_MIGRATIONS = ((1, "_migrate_v1"),)
SCHEMA_VERSION = max(version for version, _ in _MIGRATIONS)


def default_db_path() -> Path:
    """DB location: $TRAINING_DB, else data/training.sqlite beside the repo."""
    return Path(
        os.environ.get("TRAINING_DB")
        or Path(__file__).resolve().parent / "data" / "training.sqlite"
    )


class MetricStore:
    def __init__(self, path):
        self._path = str(path)
        self._local = threading.local()
        self._connections: list[tuple[threading.Thread, sqlite3.Connection]] = []
        self._connections_lock = threading.Lock()
        self.conn.executescript(SCHEMA)
        self._apply_migrations()

    @property
    def schema_version(self) -> int:
        """The store's schema version (PRAGMA user_version)."""
        return int(self.conn.execute("PRAGMA user_version").fetchone()[0])

    @property
    def conn(self) -> sqlite3.Connection:
        """This thread's connection to the database.

        One connection shared across threads corrupts result sets: concurrent
        execute()/fetchall() on the same sqlite3.Connection interleaves and
        returns duplicated or None rows (a KeyError in load_runner_profile).
        Each thread gets its own connection to the same file instead — SQLite
        handles the concurrency (WAL lets readers run during a write,
        busy_timeout makes a blocked writer wait instead of failing).
        """
        self._reap_dead_connections()
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self._path, check_same_thread=False)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=5000")
            self._local.conn = c
            with self._connections_lock:
                self._connections.append((threading.current_thread(), c))
        return c

    def _reap_dead_connections(self) -> None:
        """Close the connections of threads that have finished.

        Streamlit runs each session on its own thread and each fragment on a
        pool thread, and both die with the session while the store — a cached
        resource — lives on. A connection left open by a finished thread holds
        file descriptors (two each) and the launchd agent's limit is 256, so
        without this the dashboard stops accepting connections after enough
        sessions ("Too many open files" in the dashboard's error log).
        """
        with self._connections_lock:
            alive = []
            for thread, c in self._connections:
                if thread.is_alive():
                    alive.append((thread, c))
                    continue
                with contextlib.suppress(sqlite3.Error):
                    c.close()
            self._connections = alive

    @property
    def path(self) -> Path:
        return Path(self._path)

    def snapshot(self, dest_dir: Path | None = None, keep: int = 7) -> Path:
        """Copy the store to a timestamped file and keep the newest `keep`.

        Called before a mutating refresh so a bad write can be undone. Uses
        sqlite3's backup API: the live DB is in WAL mode, so a plain file copy
        would not be a consistent snapshot.
        """
        target_dir = Path(dest_dir) if dest_dir is not None else self.path.parent / "backups"
        target_dir.mkdir(parents=True, exist_ok=True)
        dest = target_dir / f"training-{datetime.now():%Y%m%d-%H%M%S}.sqlite"
        src = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True)
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        stale = sorted(target_dir.glob("training-*.sqlite"))
        for old in stale[: max(len(stale) - keep, 0)]:
            old.unlink()
        return dest

    def _apply_migrations(self) -> None:
        """Bring the store up to SCHEMA_VERSION, once.

        PRAGMA user_version records which migrations have already run, so an
        already-migrated store does no work on every later open.
        """
        current = self.schema_version
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"store schema v{current} is newer than this code (v{SCHEMA_VERSION}) — "
                "update the app or restore an older backup"
            )
        for version, method in _MIGRATIONS:
            if version <= current:
                continue
            getattr(self, method)()
            self.conn.execute(f"PRAGMA user_version = {version}")
            self.conn.commit()

    def _migrate_v1(self) -> None:
        """Migration 1: the columns added after the original schema.

        `CREATE TABLE IF NOT EXISTS` leaves existing tables untouched, so a new
        column must be added via ALTER TABLE. Idempotent on already-migrated
        databases.
        """
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(activities)").fetchall()}
        if "name" not in cols:
            self.conn.execute("ALTER TABLE activities ADD COLUMN name TEXT")
        if "has_intervals" not in cols:
            self.conn.execute(
                "ALTER TABLE activities ADD COLUMN has_intervals INTEGER NOT NULL DEFAULT 0"
            )
        if "elapsed_s" not in cols:
            self.conn.execute("ALTER TABLE activities ADD COLUMN elapsed_s REAL")
            # Backfill from duration_s: duration <= elapsed always, so the
            # decoupling eligibility gate (elapsed >= 90 min) never newly
            # passes on backfilled rows; true values arrive on next refresh.
            self.conn.execute(
                "UPDATE activities SET elapsed_s = duration_s WHERE elapsed_s IS NULL"
            )
        # RPE / Feel / weather columns: arrive NULL and stay NULL until the
        # summary/weather backfill rewrites them (no conservative backfill
        # needed — absent data genuinely is absent).
        if "rpe_10" not in cols:
            self.conn.execute("ALTER TABLE activities ADD COLUMN rpe_10 REAL")
        if "feel" not in cols:
            self.conn.execute("ALTER TABLE activities ADD COLUMN feel TEXT")
        if "wx_temp" not in cols:
            self.conn.execute("ALTER TABLE activities ADD COLUMN wx_temp REAL")
        if "wx_feels" not in cols:
            self.conn.execute("ALTER TABLE activities ADD COLUMN wx_feels REAL")
        if "wx_humidity" not in cols:
            self.conn.execute("ALTER TABLE activities ADD COLUMN wx_humidity REAL")
        if "wx_wind" not in cols:
            self.conn.execute("ALTER TABLE activities ADD COLUMN wx_wind REAL")
        if "wx_wind_dir" not in cols:
            self.conn.execute("ALTER TABLE activities ADD COLUMN wx_wind_dir TEXT")
        if "wx_desc" not in cols:
            self.conn.execute("ALTER TABLE activities ADD COLUMN wx_desc TEXT")
        if "wx_unit" not in cols:
            self.conn.execute("ALTER TABLE activities ADD COLUMN wx_unit TEXT")
        # Garmin event type (e.g. "race"): nullable, captured only from the
        # activity-list fetch at ingestion — a row that predates the column
        # keeps NULL until backfill_event_types.py re-reads it from Garmin;
        # absent data genuinely is absent.
        if "event_type" not in cols:
            self.conn.execute("ALTER TABLE activities ADD COLUMN event_type TEXT")
        # Garmin average cadence (steps/min): nullable, captured only for rows
        # loaded through from_summary since this column existed; a row that
        # predates the column keeps NULL (absent data genuinely is absent).
        if "avg_cadence" not in cols:
            self.conn.execute("ALTER TABLE activities ADD COLUMN avg_cadence REAL")
        # Best-effort LTHR anchors replaced the rolling-mean fallback: prune
        # the dead keys so stale rows don't linger in old databases. These
        # keys are never written again, so unconditional pruning is safe.
        self.prune_legacy_keys(["load.lt_hr_rolling", "load.lt_pace_rolling"])
        # Weekly rolling/wow volume overlays are now computed in the dashboard
        # from the daily series; the stored copies are dead. Keep the weekly
        # distance/duration rows (KPI + e2e report still read them).
        self.prune_legacy_keys(
            [
                *[
                    f"volume.rolling4wk_{g}"
                    for g in ("total", "running", "treadmill", "cross", "strength")
                ],
                *[
                    f"volume.wow_pct_{g}"
                    for g in ("total", "running", "treadmill", "cross", "strength")
                ],
                "volume.rolling4wk_duration_total",
                "volume.wow_pct_duration_total",
            ]
        )
        self.conn.commit()

    def save_runner_profile(self, profile) -> None:
        rows = [
            ("hrmax", str(profile.hrmax), {"source": profile.hrmax_source}),
            ("hrrest", str(profile.hrrest), {}),
            ("sex", profile.sex, {}),
            ("birth_year", str(profile.birth_year), {}),
            ("lthr_manual", "" if profile.lthr_manual is None else str(profile.lthr_manual), {}),
            (
                "hr_zones",
                json.dumps({str(k): list(v) for k, v in sorted(profile.hr_zones.items())}),
                {},
            ),
            (
                "units",
                profile.units,
                {},
            ),
            (
                "selected_race",
                profile.selected_race,
                {},
            ),
            (
                "selected_volume_agg",
                profile.selected_volume_agg,
                {},
            ),
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO runner_profile (key, value, meta) VALUES (?, ?, ?)",
            [(k, v, json.dumps(m, sort_keys=True)) for k, v, m in rows],
        )
        self.conn.commit()

    def save_activities(self, activities) -> None:
        rows = [
            (
                a.activity_id,
                a.date.isoformat(),
                a.sport,
                a.distance_m,
                a.duration_s,
                a.elapsed_s,
                a.ele_gain_m,
                a.vo2max,
                a.avg_hr,
                a.max_hr,
                a.aerobic_te,
                a.anaerobic_te,
                a.avg_speed,
                a.fastest_split_1609,
                a.avg_cadence,
                json.dumps(a.zone_s, sort_keys=True),
                json.dumps(
                    {"lat": a.lat, "lon": a.lon, "location": a.location, "device_id": a.device_id},
                    sort_keys=True,
                ),
                a.name,
                1 if a.has_intervals else 0,
                a.rpe_10,
                a.feel,
                a.wx_temp,
                a.wx_feels,
                a.wx_humidity,
                a.wx_wind,
                a.wx_wind_dir,
                a.wx_desc,
                a.wx_unit,
                a.event_type,
            )
            for a in activities
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO activities (activity_id, activity_date, sport, "
            "distance_m, duration_s, elapsed_s, ele_gain_m, vo2max, avg_hr, max_hr, aerobic_te, "
            "anaerobic_te, avg_speed, fastest_split_1609, avg_cadence, zone_s, route, name, "
            "has_intervals, "
            "rpe_10, feel, wx_temp, wx_feels, wx_humidity, wx_wind, wx_wind_dir, wx_desc, "
            "wx_unit, event_type) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self.conn.commit()

    def save_metric_rows(self, rows) -> None:
        if not rows:
            return
        self.conn.executemany(
            "INSERT OR REPLACE INTO metric_series (metric, date, value, source, params, flags) "
            "VALUES (:metric, :date, :value, :source, :params, :flags)",
            rows,
        )
        self.conn.commit()

    def read_metric(self, metric: str) -> list[dict]:
        cur = self.conn.execute(
            "SELECT metric, date, value, source, params, flags FROM metric_series "
            "WHERE metric = ? ORDER BY date",
            (metric,),
        )
        return [
            dict(zip(("metric", "date", "value", "source", "params", "flags"), row, strict=False))
            for row in cur.fetchall()
        ]

    def activities_fingerprint(self) -> str:
        """Cheap change detector for the activities table.

        Columns + row count + elapsed total: changes on migration, refresh
        (new rows), and value rewrites (backfilled elapsed replaced by true
        values). Lets callers cache loaded Activity objects and reload only
        when the store actually changed.
        """
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(activities)").fetchall()]
        total = "COALESCE(SUM(COALESCE(elapsed_s, duration_s)), 0)"
        if "elapsed_s" not in cols:
            total = "COALESCE(SUM(duration_s), 0)"
        n, tot = self.conn.execute(f"SELECT COUNT(*), {total} FROM activities").fetchone()
        return f"{'|'.join(cols)}#{n}#{tot}"

    def latest_activity_date(self) -> date | None:
        """Date of the most recent activity persisted, or None when the store is empty.

        Used to bound an incremental Garmin refresh to only-new pages instead of
        re-paginating the full history every run (which can exceed the dashboard's
        90s guard for accounts with many years of activities).
        """
        row = self.conn.execute("SELECT MAX(activity_date) FROM activities").fetchone()
        if not row or row[0] is None:
            return None
        return date.fromisoformat(row[0])

    def earliest_activity_date(self) -> date | None:
        """Date of the earliest activity persisted, or None when the store is empty.

        Used for historical data fetch to determine the date range for older activities.
        """
        row = self.conn.execute("SELECT MIN(activity_date) FROM activities").fetchone()
        if not row or row[0] is None:
            return None
        return date.fromisoformat(row[0])

    def load_activities(self) -> list[Activity]:
        rows = self.conn.execute(
            "SELECT activity_id, activity_date, sport, distance_m, duration_s, elapsed_s, "
            "ele_gain_m, vo2max, avg_hr, max_hr, aerobic_te, anaerobic_te, "
            "avg_speed, fastest_split_1609, avg_cadence, zone_s, route, name, has_intervals, "
            "rpe_10, feel, wx_temp, wx_feels, wx_humidity, wx_wind, wx_wind_dir, wx_desc, "
            "wx_unit, event_type FROM activities "
            "ORDER BY activity_date, activity_id"
        ).fetchall()
        return [self._activity_from_row(r) for r in rows]

    @staticmethod
    def _activity_from_row(row) -> Activity:
        (
            activity_id,
            activity_date,
            sport,
            distance_m,
            duration_s,
            elapsed_s,
            ele_gain_m,
            vo2max,
            avg_hr,
            max_hr,
            aerobic_te,
            anaerobic_te,
            avg_speed,
            fastest_split_1609,
            avg_cadence,
            zone_s,
            route_s,
            name,
            has_intervals,
            rpe_10,
            feel,
            wx_temp,
            wx_feels,
            wx_humidity,
            wx_wind,
            wx_wind_dir,
            wx_desc,
            wx_unit,
            event_type,
        ) = row
        zones: dict[int, float] = {}
        if zone_s:
            try:
                zones = {int(k): float(v) for k, v in json.loads(zone_s).items()}
            except (json.JSONDecodeError, TypeError, ValueError):
                zones = {}
        route: dict = {}
        if route_s:
            try:
                route = json.loads(route_s)
            except (json.JSONDecodeError, TypeError, ValueError):
                route = {}
        duration = float(duration_s)
        # Pre-elapsed databases backfill elapsed from duration; a stored 0.0
        # (Garmin omits elapsedDuration) falls back the same way.
        elapsed = float(elapsed_s) if elapsed_s else duration
        return Activity(
            activity_id=int(activity_id),
            sport=sport,
            date=date.fromisoformat(activity_date),
            ts_ms=0,
            distance_m=float(distance_m),
            duration_s=duration,
            elapsed_s=elapsed,
            zone_s=zones,
            ele_gain_m=ele_gain_m,
            vo2max=vo2max,
            avg_hr=avg_hr,
            max_hr=max_hr,
            aerobic_te=aerobic_te,
            anaerobic_te=anaerobic_te,
            avg_speed=avg_speed,
            fastest_split_1609=fastest_split_1609,
            avg_cadence=avg_cadence,
            name=name,
            has_intervals=bool(has_intervals),
            event_type=event_type,
            rpe_10=rpe_10,
            feel=feel,
            wx_temp=wx_temp,
            wx_feels=wx_feels,
            wx_humidity=wx_humidity,
            wx_wind=wx_wind,
            wx_wind_dir=wx_wind_dir,
            wx_desc=wx_desc,
            wx_unit=wx_unit,
            lat=route.get("lat"),
            lon=route.get("lon"),
            location=route.get("location"),
            device_id=route.get("device_id"),
        )

    def load_runner_profile(self) -> RunnerProfile | None:
        rows = self.conn.execute("SELECT key, value, meta FROM runner_profile").fetchall()
        if not rows:
            return None
        data = {k: v for k, v, _ in rows}
        hrmax_meta: dict = {}
        for k, _v, m in rows:
            if k == "hrmax" and m:
                try:
                    hrmax_meta = json.loads(m)
                except json.JSONDecodeError:
                    hrmax_meta = {}
        zones: dict[int, tuple[int, int]] = {}
        raw_zones = data.get("hr_zones", "")
        if raw_zones:
            try:
                zones = {int(k): tuple(v) for k, v in json.loads(raw_zones).items()}
            except (json.JSONDecodeError, TypeError, ValueError):
                zones = {}
        lthr_raw = data.get("lthr_manual", "")
        return RunnerProfile(
            hrmax=int(data["hrmax"]),
            hrrest=int(data["hrrest"]),
            sex=data["sex"],
            birth_year=int(data["birth_year"]),
            lthr_manual=int(lthr_raw) if lthr_raw else None,
            hr_zones=zones,
            units=data.get("units", "metric"),
            hrmax_source=hrmax_meta.get("source", "configured"),
            selected_race=data.get("selected_race", "5k"),
            selected_volume_agg=data.get("selected_volume_agg", "weekly"),
        )

    def prune_legacy_keys(self, keys: list[str]) -> int:
        """Delete metric_series rows for the given metric keys. Returns rows deleted."""
        if not keys:
            return 0
        placeholders = ",".join("?" for _ in keys)
        # Skip the DELETE when nothing matches: a DELETE opens a write
        # transaction, which would contend with a concurrent writer (criterion 12).
        found = self.conn.execute(
            f"SELECT 1 FROM metric_series WHERE metric IN ({placeholders}) LIMIT 1", keys
        ).fetchone()
        if not found:
            return 0
        cur = self.conn.execute(f"DELETE FROM metric_series WHERE metric IN ({placeholders})", keys)
        self.conn.commit()
        return cur.rowcount

    def close(self) -> None:
        with self._connections_lock:
            conns, self._connections = self._connections, []
        for _thread, c in conns:
            with contextlib.suppress(sqlite3.Error):
                c.close()
        self._local = threading.local()
