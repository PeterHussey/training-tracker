"""SQLite persistence for the measurement layer."""
import json
import sqlite3
from pathlib import Path

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
  route              TEXT NOT NULL DEFAULT '{}'
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
        self.conn = sqlite3.connect(str(path))
        self.conn.executescript(SCHEMA)

    def save_runner_profile(self, profile) -> None:
        rows = [
            ("hrmax", str(profile.hrmax), {"source": profile.hrmax_source}),
            ("hrrest", str(profile.hrrest), {}),
            ("sex", profile.sex, {}),
            ("birth_year", str(profile.birth_year), {}),
            ("lthr_manual", "" if profile.lthr_manual is None else str(profile.lthr_manual), {}),
            ("hr_zones", json.dumps({str(k): list(v) for k, v in sorted(profile.hr_zones.items())}), {}),
            ("units", profile.units, {}),
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO runner_profile (key, value, meta) VALUES (?, ?, ?)",
            [(k, v, json.dumps(m, sort_keys=True)) for k, v, m in rows],
        )
        self.conn.commit()

    def save_activities(self, activities) -> None:
        rows = [(
            a.activity_id, a.date.isoformat(), a.sport, a.distance_m, a.duration_s,
            a.ele_gain_m, a.vo2max, a.avg_hr, a.max_hr, a.aerobic_te, a.anaerobic_te,
            a.avg_speed, a.fastest_split_1609,
            json.dumps(a.zone_s, sort_keys=True),
            json.dumps({"lat": a.lat, "lon": a.lon, "location": a.location,
                        "device_id": a.device_id}, sort_keys=True),
        ) for a in activities]
        self.conn.executemany(
            "INSERT OR REPLACE INTO activities (activity_id, activity_date, sport, "
            "distance_m, duration_s, ele_gain_m, vo2max, avg_hr, max_hr, aerobic_te, "
            "anaerobic_te, avg_speed, fastest_split_1609, zone_s, route) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
        return [dict(zip(("metric", "date", "value", "source", "params", "flags"), row))
                for row in cur.fetchall()]

    def close(self) -> None:
        self.conn.close()