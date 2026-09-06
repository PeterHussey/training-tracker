"""SQLite persistence for the measurement layer."""

import json
import sqlite3
from datetime import date
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
  ele_gain_m         REAL,
  vo2max             REAL,
  avg_hr             REAL,
  max_hr             REAL,
  aerobic_te         REAL,
  anaerobic_te       REAL,
  avg_speed          REAL,
  fastest_split_1609 REAL,
  zone_s             TEXT NOT NULL DEFAULT '{}',
  route              TEXT NOT NULL DEFAULT '{}',
  name               TEXT
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


class MetricStore:
    def __init__(self, path):
        # Streamlit caches the store via st.cache_resource and reuses the same
        # connection across script reruns, each on a different thread — so the
        # connection must not be implicitly bound to a single thread.
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """In-place migrations for stores created before a schema change.

        `CREATE TABLE IF NOT EXISTS` leaves existing tables untouched, so a new
        column must be added via ALTER TABLE. Idempotent on already-migrated
        databases.
        """
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(activities)").fetchall()}
        if "name" not in cols:
            self.conn.execute("ALTER TABLE activities ADD COLUMN name TEXT")
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
                a.ele_gain_m,
                a.vo2max,
                a.avg_hr,
                a.max_hr,
                a.aerobic_te,
                a.anaerobic_te,
                a.avg_speed,
                a.fastest_split_1609,
                json.dumps(a.zone_s, sort_keys=True),
                json.dumps(
                    {"lat": a.lat, "lon": a.lon, "location": a.location, "device_id": a.device_id},
                    sort_keys=True,
                ),
                a.name,
            )
            for a in activities
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO activities (activity_id, activity_date, sport, "
            "distance_m, duration_s, ele_gain_m, vo2max, avg_hr, max_hr, aerobic_te, "
            "anaerobic_te, avg_speed, fastest_split_1609, zone_s, route, name) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
            "SELECT activity_id, activity_date, sport, distance_m, duration_s, "
            "ele_gain_m, vo2max, avg_hr, max_hr, aerobic_te, anaerobic_te, "
            "avg_speed, fastest_split_1609, zone_s, name FROM activities "
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
            ele_gain_m,
            vo2max,
            avg_hr,
            max_hr,
            aerobic_te,
            anaerobic_te,
            avg_speed,
            fastest_split_1609,
            zone_s,
            name,
        ) = row
        zones: dict[int, float] = {}
        if zone_s:
            try:
                zones = {int(k): float(v) for k, v in json.loads(zone_s).items()}
            except (json.JSONDecodeError, TypeError, ValueError):
                zones = {}
        return Activity(
            activity_id=int(activity_id),
            sport=sport,
            date=date.fromisoformat(activity_date),
            ts_ms=0,
            distance_m=float(distance_m),
            duration_s=float(duration_s),
            elapsed_s=0.0,
            zone_s=zones,
            ele_gain_m=ele_gain_m,
            vo2max=vo2max,
            avg_hr=avg_hr,
            max_hr=max_hr,
            aerobic_te=aerobic_te,
            anaerobic_te=anaerobic_te,
            avg_speed=avg_speed,
            fastest_split_1609=fastest_split_1609,
            name=name,
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
        )

    def close(self) -> None:
        self.conn.close()
