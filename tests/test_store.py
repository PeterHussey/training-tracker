# tests/test_store.py
import threading
from datetime import date
from profile import RunnerProfile, default_profile

from normalize import Activity
from store import MetricStore

ACT = Activity(
    activity_id=1,
    sport="running",
    date=date(2026, 4, 1),
    ts_ms=0,
    distance_m=10000.0,
    duration_s=3600.0,
    elapsed_s=3600.0,
    avg_hr=150.0,
    max_hr=170.0,
    zone_s={1: 600.0, 2: 600.0, 3: 600.0, 4: 600.0, 5: 600.0},
    ele_gain_m=50.0,
    vo2max=46.0,
)


def test_store_roundtrip(tmp_path):
    db = tmp_path / "t.db"
    store = MetricStore(str(db))
    store.save_runner_profile(default_profile(age=40, hrrest=60, sex="M"))
    store.save_activities([ACT])
    store.save_metric_rows(
        [
            {
                "metric": "load.banister",
                "date": "2026-04-01",
                "value": 120.5,
                "source": "computed",
                "params": "{}",
                "flags": "{}",
            }
        ]
    )
    rows = store.read_metric("load.banister")
    assert rows[0]["value"] == 120.5
    assert rows[0]["source"] == "computed"
    store.close()


def test_schema_has_required_tables(tmp_path):
    store = MetricStore(str(tmp_path / "t.db"))
    tables = {
        r[0]
        for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert {"runner_profile", "activities", "metric_series"} <= tables
    store.close()


SECOND_ACT = Activity(
    activity_id=2,
    sport="cross",
    date=date(2026, 4, 2),
    ts_ms=0,
    distance_m=0.0,
    duration_s=2700.0,
    elapsed_s=2700.0,
    avg_hr=140.0,
    max_hr=160.0,
    zone_s={1: 400.0, 2: 800.0},
    vo2max=None,
    ele_gain_m=None,
)


def test_load_activities_roundtrip(tmp_path):
    db = tmp_path / "t.db"
    store = MetricStore(str(db))
    store.save_activities([ACT, SECOND_ACT])
    loaded = store.load_activities()
    store.close()
    assert len(loaded) == 2
    first, second = loaded[0], loaded[1]
    assert first.activity_id == 1
    assert first.sport == "running"
    assert first.date == date(2026, 4, 1)
    assert first.distance_m == 10000.0
    assert first.duration_s == 3600.0
    assert first.avg_hr == 150.0
    assert first.max_hr == 170.0
    assert first.vo2max == 46.0
    assert first.ele_gain_m == 50.0
    assert first.zone_s == {1: 600.0, 2: 600.0, 3: 600.0, 4: 600.0, 5: 600.0}
    assert second.sport == "cross"
    assert second.avg_hr == 140.0
    assert second.vo2max is None


def test_activity_name_roundtrip(tmp_path):
    db = tmp_path / "t.db"
    store = MetricStore(str(db))
    named = Activity(
        activity_id=77,
        sport="running",
        date=date(2026, 8, 19),
        ts_ms=0,
        distance_m=5684.0,
        duration_s=2148.0,
        elapsed_s=2249.0,
        avg_hr=137.0,
        max_hr=151.0,
        zone_s={},
        name="Winnetka - RF24 (Foundation Run)",
    )
    store.save_activities([named])
    loaded = store.load_activities()
    store.close()
    assert len(loaded) == 1
    assert loaded[0].name == "Winnetka - RF24 (Foundation Run)"
    assert loaded[0].code == "RF24"


def test_load_activities_empty(tmp_path):
    store = MetricStore(str(tmp_path / "t.db"))
    assert store.load_activities() == []
    store.close()


def test_load_runner_profile_roundtrip(tmp_path):
    db = tmp_path / "t.db"
    store = MetricStore(str(db))
    p = default_profile(age=40, hrrest=60, sex="M")  # age-predicted hrmax 180 + zones
    store.save_runner_profile(p)
    loaded = store.load_runner_profile()
    store.close()
    assert loaded is not None
    assert loaded.hrmax == p.hrmax == 180
    assert loaded.hrmax_source == "age_predicted"
    assert loaded.hrrest == 60
    assert loaded.sex == "M"
    assert loaded.birth_year == p.birth_year
    assert loaded.hr_zones == p.hr_zones
    assert loaded.units == "metric"
    assert loaded.selected_race == "5k"  # default when not persisted


def test_load_runner_profile_configured_roundtrip(tmp_path):
    db = tmp_path / "t.db"
    store = MetricStore(str(db))
    p = RunnerProfile(
        hrmax=185,
        hrrest=62,
        sex="F",
        birth_year=1990,
        lthr_manual=168,
        hrmax_source="configured",
        selected_race="half",
    )
    store.save_runner_profile(p)
    loaded = store.load_runner_profile()
    store.close()
    assert loaded is not None
    assert loaded.hrmax == 185
    assert loaded.hrmax_source == "configured"
    assert loaded.sex == "F"
    assert loaded.birth_year == 1990
    assert loaded.lthr_manual == 168
    assert loaded.selected_race == "half"


def test_load_runner_profile_selected_volume_agg_default(tmp_path):
    """selected_volume_agg defaults to 'weekly' when not persisted."""
    from profile import default_profile

    db = tmp_path / "t.db"
    store = MetricStore(str(db))
    p = default_profile(age=40, hrrest=60, sex="M")
    store.save_runner_profile(p)
    loaded = store.load_runner_profile()
    store.close()
    assert loaded is not None
    assert loaded.selected_volume_agg == "weekly"


def test_load_runner_profile_selected_volume_agg_roundtrip(tmp_path):
    """Persisted selected_volume_agg survives reload."""
    db = tmp_path / "t.db"
    store = MetricStore(str(db))
    p = RunnerProfile(
        hrmax=185,
        hrrest=62,
        sex="F",
        birth_year=1990,
        selected_volume_agg="monthly",
    )
    store.save_runner_profile(p)
    loaded = store.load_runner_profile()
    store.close()
    assert loaded is not None
    assert loaded.selected_volume_agg == "monthly"


def test_load_runner_profile_none(tmp_path):
    store = MetricStore(str(tmp_path / "t.db"))
    assert store.load_runner_profile() is None
    store.close()


def test_load_runner_profile_nondefault_roundtrip(tmp_path):
    db = tmp_path / "t.db"
    store = MetricStore(str(db))
    p = RunnerProfile(
        hrmax=190,
        hrrest=55,
        sex="F",
        birth_year=1985,
        units="imperial",
        hrmax_source="configured",
        lthr_manual=172,
    )
    store.save_runner_profile(p)
    loaded = store.load_runner_profile()
    store.close()
    assert loaded is not None
    assert loaded.hrmax == 190
    assert loaded.hrrest == 55
    assert loaded.sex == "F"
    assert loaded.birth_year == 1985
    assert loaded.units == "imperial"
    assert loaded.hrmax_source == "configured"
    assert loaded.lthr_manual == 172


def test_store_is_usable_from_another_thread(tmp_path):
    # Streamlit caches the MetricStore via st.cache_resource, so the SAME object
    # (and its sqlite connection) is reused across script reruns, each of which
    # runs on a different thread. A connection created on one thread must be
    # usable from the others, or the dashboard crashes with
    # sqlite3.ProgrammingError. The check only fires once more than the owning
    # thread touches the connection, so we exercise it from several threads.
    db = tmp_path / "t.db"

    created_holder = {}

    def create():
        s = MetricStore(str(db))
        s.save_runner_profile(default_profile(age=40, hrrest=60, sex="M"))
        created_holder["store"] = s

    t_create = threading.Thread(target=create)
    t_create.start()
    t_create.join()
    store = created_holder["store"]

    failures = []

    def use(tag):
        try:
            store.load_runner_profile()
        except Exception as e:  # noqa: BLE001
            failures.append(f"{tag}:{type(e).__name__}")

    threads = [threading.Thread(target=use, args=(f"run{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not failures, f"cross-thread use failed: {failures}"


def test_store_uses_wal_and_busy_timeout(tmp_path):
    # Criterion 12: the scheduled refresh writes while the dashboard reads.
    # WAL lets the reader proceed during a write; busy_timeout makes a blocked
    # writer wait instead of raising "database is locked" straight away.
    store = MetricStore(str(tmp_path / "t.db"))
    mode = store.conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    assert store.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_reader_is_not_blocked_by_an_open_writer(tmp_path):
    # A writer holding a transaction open must not lock out a reader.
    db = tmp_path / "t.db"
    writer = MetricStore(str(db))
    writer.conn.execute("BEGIN IMMEDIATE")
    writer.conn.execute(
        "INSERT INTO metric_series (metric, date, value, source) "
        "VALUES ('load.banister', '2026-08-20', 42.0, 'test')"
    )
    reader = MetricStore(str(db))
    # WAL: the reader sees the committed state, without blocking on the writer.
    assert reader.conn.execute("SELECT COUNT(*) FROM metric_series").fetchone()[0] == 0
    writer.conn.commit()
    assert reader.conn.execute("SELECT COUNT(*) FROM metric_series").fetchone()[0] == 1


def test_latest_activity_date_returns_newest(tmp_path):
    store = MetricStore(str(tmp_path / "t.db"))
    store.save_activities([ACT, SECOND_ACT])  # dates 2026-04-01 and 2026-04-02
    assert store.latest_activity_date() == date(2026, 4, 2)
    store.close()


def test_latest_activity_date_none_when_empty(tmp_path):
    store = MetricStore(str(tmp_path / "t.db"))
    assert store.latest_activity_date() is None
    store.close()


def _legacy_row(metric: str) -> dict:
    return {
        "metric": metric,
        "date": "2026-04-01",
        "value": 160.0,
        "source": "computed",
        "params": "{}",
        "flags": "{}",
    }


def test_prune_legacy_keys_deletes_only_listed(tmp_path):
    store = MetricStore(str(tmp_path / "t.db"))
    store.save_metric_rows(
        [
            _legacy_row("load.lt_hr_rolling"),
            _legacy_row("load.lt_pace_rolling"),
            _legacy_row("load.lt_hr_best20"),
            _legacy_row("load.banister"),
        ]
    )
    deleted = store.prune_legacy_keys(["load.lt_hr_rolling", "load.lt_pace_rolling"])
    assert deleted == 2
    assert store.read_metric("load.lt_hr_rolling") == []
    assert store.read_metric("load.lt_pace_rolling") == []
    assert len(store.read_metric("load.lt_hr_best20")) == 1
    assert len(store.read_metric("load.banister")) == 1
    store.close()


def test_prune_legacy_keys_empty_is_noop(tmp_path):
    store = MetricStore(str(tmp_path / "t.db"))
    assert store.prune_legacy_keys([]) == 0
    store.close()


def test_has_intervals_roundtrip(tmp_path):
    from dataclasses import replace

    db = str(tmp_path / "t.db")
    store = MetricStore(db)
    store.save_activities([ACT, replace(SECOND_ACT, has_intervals=True)])
    loaded = {a.activity_id: a for a in store.load_activities()}
    store.close()
    assert loaded[1].has_intervals is False
    assert loaded[2].has_intervals is True
    # Reopening a migrated DB preserves the flag.
    reopened = MetricStore(db)
    reloaded = {a.activity_id: a for a in reopened.load_activities()}
    reopened.close()
    assert reloaded[2].has_intervals is True


def test_migrate_prunes_rolling_keys_on_reopen(tmp_path):
    db = str(tmp_path / "t.db")
    store = MetricStore(db)
    store.save_metric_rows([_legacy_row("load.lt_hr_rolling"), _legacy_row("load.lt_hr_best20")])
    store.close()
    # Reopening triggers _migrate, which prunes the dead rolling keys.
    reopened = MetricStore(db)
    assert reopened.read_metric("load.lt_hr_rolling") == []
    assert len(reopened.read_metric("load.lt_hr_best20")) == 1
    reopened.close()


def test_elapsed_s_roundtrip(tmp_path):
    """elapsed_s must survive the store: decoupling eligibility gates on it,
    and DB-loaded activities previously came back with elapsed_s=0.0."""
    from dataclasses import replace

    db = tmp_path / "t.db"
    store = MetricStore(str(db))
    store.save_activities([replace(ACT, elapsed_s=4000.0)])
    loaded = store.load_activities()
    store.close()
    assert loaded[0].elapsed_s == 4000.0


def test_route_latlon_roundtrip(tmp_path):
    """Start lat/lon (saved in the route JSON) must load back: route
    clustering collapses to 'unknown' without them."""
    from dataclasses import replace

    db = tmp_path / "t.db"
    store = MetricStore(str(db))
    store.save_activities([replace(ACT, lat=42.42, lon=-71.28)])
    loaded = store.load_activities()
    store.close()
    assert loaded[0].lat == 42.42
    assert loaded[0].lon == -71.28


def test_migrate_adds_elapsed_backfilled_from_duration(tmp_path):
    """Pre-elapsed databases gain the column on open, backfilled from
    duration_s (conservative: duration <= elapsed always, so the
    eligibility gate never newly passes on backfilled rows)."""
    db = str(tmp_path / "t.db")
    conn = __import__("sqlite3").connect(db)
    conn.execute(
        "CREATE TABLE activities (activity_id INTEGER PRIMARY KEY, "
        "activity_date TEXT NOT NULL, sport TEXT NOT NULL, distance_m REAL, "
        "duration_s REAL, ele_gain_m REAL, vo2max REAL, avg_hr REAL, max_hr REAL, "
        "aerobic_te REAL, anaerobic_te REAL, avg_speed REAL, fastest_split_1609 REAL, "
        "zone_s TEXT NOT NULL DEFAULT '{}', route TEXT NOT NULL DEFAULT '{}', "
        "name TEXT, has_intervals INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO activities (activity_id, activity_date, sport, distance_m, "
        "duration_s) VALUES (9, '2026-03-01', 'running', 18000.0, 6300.0)"
    )
    conn.commit()
    conn.close()
    store = MetricStore(db)
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(activities)").fetchall()}
    assert "elapsed_s" in cols
    loaded = store.load_activities()
    store.close()
    assert loaded[0].elapsed_s == 6300.0


# The 9 RPE / Feel / weather columns added in 2026-10. Ordering matches the
# controller ruling: rpe_10 REAL, feel TEXT, wx_temp/wx_feels/wx_humidity/
# wx_wind REAL, wx_wind_dir/wx_desc/wx_unit TEXT.
RPE_WX_COLS = (
    "rpe_10",
    "feel",
    "wx_temp",
    "wx_feels",
    "wx_humidity",
    "wx_wind",
    "wx_wind_dir",
    "wx_desc",
    "wx_unit",
)


def test_rpe_weather_roundtrip_populated_and_null(tmp_path):
    """Populated values survive the store; NULLs stay None; REAL columns
    coerce int input to float (SQLite REAL affinity)."""
    from dataclasses import replace

    db = tmp_path / "t.db"
    store = MetricStore(str(db))
    full = replace(
        ACT,
        rpe_10=7,  # int in -> float out (REAL affinity)
        feel="Strong",
        wx_temp=71.6,
        wx_feels=70.2,
        wx_humidity=55.0,
        wx_wind=8.2,
        wx_wind_dir="NNE",
        wx_desc="Fair",
        wx_unit="F",
    )
    store.save_activities([full, SECOND_ACT])  # SECOND_ACT: all 9 fields None
    loaded = {a.activity_id: a for a in store.load_activities()}
    store.close()
    first = loaded[1]
    assert first.rpe_10 == 7.0
    assert isinstance(first.rpe_10, float)
    assert first.feel == "Strong"
    assert first.wx_temp == 71.6
    assert first.wx_feels == 70.2
    assert first.wx_humidity == 55.0
    assert first.wx_wind == 8.2
    assert first.wx_wind_dir == "NNE"
    assert first.wx_desc == "Fair"
    assert first.wx_unit == "F"
    second = loaded[2]
    for col in RPE_WX_COLS:
        assert getattr(second, col) is None, col


def test_migrate_adds_rpe_weather_columns_to_old_database(tmp_path):
    """A pre-change database (old activities schema) gains the 9 columns on
    open; existing rows load with NULLs and their old values survive."""
    db = str(tmp_path / "t.db")
    conn = __import__("sqlite3").connect(db)
    # Verbatim copy of the activities CREATE TABLE before RPE/weather.
    conn.execute(
        "CREATE TABLE activities ("
        "  activity_id        INTEGER PRIMARY KEY,"
        "  activity_date      TEXT NOT NULL,"
        "  sport              TEXT NOT NULL,"
        "  distance_m         REAL NOT NULL DEFAULT 0,"
        "  duration_s         REAL NOT NULL DEFAULT 0,"
        "  elapsed_s          REAL NOT NULL DEFAULT 0,"
        "  ele_gain_m         REAL,"
        "  vo2max             REAL,"
        "  avg_hr             REAL,"
        "  max_hr             REAL,"
        "  aerobic_te         REAL,"
        "  anaerobic_te       REAL,"
        "  avg_speed          REAL,"
        "  fastest_split_1609 REAL,"
        "  zone_s             TEXT NOT NULL DEFAULT '{}',"
        "  route              TEXT NOT NULL DEFAULT '{}',"
        "  name               TEXT,"
        "  has_intervals      INTEGER NOT NULL DEFAULT 0"
        ")"
    )
    conn.execute(
        "INSERT INTO activities (activity_id, activity_date, sport, distance_m, "
        "duration_s, elapsed_s, name) VALUES (9, '2026-03-01', 'running', 18000.0, "
        "6300.0, 6400.0, 'Old Row')"
    )
    conn.commit()
    conn.close()
    store = MetricStore(db)
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(activities)").fetchall()}
    assert set(RPE_WX_COLS) <= cols
    loaded = store.load_activities()
    store.close()
    assert len(loaded) == 1
    row = loaded[0]
    # Old values survive the migration.
    assert row.name == "Old Row"
    assert row.elapsed_s == 6400.0
    assert row.distance_m == 18000.0
    # New columns arrive NULL.
    for col in RPE_WX_COLS:
        assert getattr(row, col) is None, col


def test_event_type_roundtrip_and_overwrite(tmp_path):
    """The race tag survives save/load through the real path, including an
    INSERT OR REPLACE overwrite of the same activity (the refresh path
    re-saves known ids), and untagged rows stay None."""
    from dataclasses import replace

    db = str(tmp_path / "t.db")
    store = MetricStore(db)
    race = replace(ACT, event_type="race")
    store.save_activities([race, SECOND_ACT])
    loaded = {a.activity_id: a for a in store.load_activities()}
    assert loaded[1].event_type == "race"
    assert loaded[2].event_type is None
    # Refresh re-saves id 1: same activity_id, new distance. The tag must not
    # be lost by the replace.
    store.save_activities([replace(race, distance_m=10500.0)])
    again = {a.activity_id: a for a in store.load_activities()}
    store.close()
    assert again[1].event_type == "race"
    assert again[1].distance_m == 10500.0


def test_event_type_untagged_migrated_row_is_none(tmp_path):
    """A row saved without a tag reads back as None, not "" or "uncategorized"."""
    store = MetricStore(str(tmp_path / "t.db"))
    store.save_activities([ACT])
    loaded = store.load_activities()
    store.close()
    assert loaded[0].event_type is None


# The activities CREATE TABLE as it stood after the 2026-10 RPE/weather
# columns and before Task 7's event_type.
PRE_EVENT_TYPE_SCHEMA = (
    "CREATE TABLE activities ("
    "  activity_id        INTEGER PRIMARY KEY,"
    "  activity_date      TEXT NOT NULL,"
    "  sport              TEXT NOT NULL,"
    "  distance_m         REAL NOT NULL DEFAULT 0,"
    "  duration_s         REAL NOT NULL DEFAULT 0,"
    "  elapsed_s          REAL NOT NULL DEFAULT 0,"
    "  ele_gain_m         REAL,"
    "  vo2max             REAL,"
    "  avg_hr             REAL,"
    "  max_hr             REAL,"
    "  aerobic_te         REAL,"
    "  anaerobic_te       REAL,"
    "  avg_speed          REAL,"
    "  fastest_split_1609 REAL,"
    "  zone_s             TEXT NOT NULL DEFAULT '{}',"
    "  route              TEXT NOT NULL DEFAULT '{}',"
    "  name               TEXT,"
    "  has_intervals      INTEGER NOT NULL DEFAULT 0,"
    "  rpe_10             REAL,"
    "  feel               TEXT,"
    "  wx_temp            REAL,"
    "  wx_feels           REAL,"
    "  wx_humidity        REAL,"
    "  wx_wind            REAL,"
    "  wx_wind_dir        TEXT,"
    "  wx_desc            TEXT,"
    "  wx_unit            TEXT"
    ")"
)


def test_migrate_adds_event_type_to_old_database(tmp_path):
    """A pre-Task-7 database gains a nullable event_type column on open;
    existing rows load with None and their old values survive."""
    db = str(tmp_path / "t.db")
    conn = __import__("sqlite3").connect(db)
    conn.execute(PRE_EVENT_TYPE_SCHEMA)
    conn.execute(
        "INSERT INTO activities (activity_id, activity_date, sport, distance_m, "
        "duration_s, elapsed_s, name) VALUES (9, '2026-03-01', 'running', 18000.0, "
        "6300.0, 6400.0, 'Old Row')"
    )
    conn.commit()
    conn.close()
    store = MetricStore(db)
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(activities)").fetchall()}
    assert "event_type" in cols
    loaded = store.load_activities()
    store.close()
    assert len(loaded) == 1
    assert loaded[0].name == "Old Row"
    assert loaded[0].distance_m == 18000.0
    assert loaded[0].event_type is None


def test_migrate_event_type_idempotent_on_reopen(tmp_path):
    """Reopening an already-migrated DB does not duplicate the column and the
    saved tag survives the second open."""
    from dataclasses import replace

    db = str(tmp_path / "t.db")
    store = MetricStore(db)
    store.save_activities([replace(ACT, event_type="race")])
    store.close()
    reopened = MetricStore(db)
    names = [r[1] for r in reopened.conn.execute("PRAGMA table_info(activities)").fetchall()]
    assert names.count("event_type") == 1
    loaded = reopened.load_activities()
    reopened.close()
    assert loaded[0].event_type == "race"


def test_migrate_rpe_weather_idempotent_on_reopen(tmp_path):
    """Reopening an already-migrated DB is a no-op: each new column appears
    exactly once and saved values survive the second open."""
    db = str(tmp_path / "t.db")
    from dataclasses import replace

    store = MetricStore(db)
    store.save_activities([replace(ACT, rpe_10=6.5, feel="Weak", wx_temp=50.0)])
    store.close()
    reopened = MetricStore(db)
    cur = reopened.conn.execute("PRAGMA table_info(activities)")
    names = [r[1] for r in cur.fetchall()]
    for col in RPE_WX_COLS:
        assert names.count(col) == 1, f"{col} duplicated by re-migration"
    loaded = reopened.load_activities()
    reopened.close()
    assert loaded[0].rpe_10 == 6.5
    assert loaded[0].feel == "Weak"
    assert loaded[0].wx_temp == 50.0


# --- Task 5: avg_cadence column (stored, migrated, NULL for old rows) ---


def test_avg_cadence_roundtrip(tmp_path):
    """avg_cadence survives the store; missing values stay None."""
    from dataclasses import replace

    db = tmp_path / "t.db"
    store = MetricStore(str(db))
    store.save_activities([replace(ACT, avg_cadence=178.5), SECOND_ACT])
    loaded = {a.activity_id: a for a in store.load_activities()}
    store.close()
    assert loaded[1].avg_cadence == 178.5
    assert loaded[2].avg_cadence is None


def test_migrate_adds_avg_cadence_to_old_database(tmp_path):
    """A pre-Task-5 database (current schema minus avg_cadence) gains the
    nullable column on open; existing rows keep NULL and old values survive."""
    db = str(tmp_path / "t.db")
    conn = __import__("sqlite3").connect(db)
    conn.execute(
        "CREATE TABLE activities ("
        "  activity_id        INTEGER PRIMARY KEY,"
        "  activity_date      TEXT NOT NULL,"
        "  sport              TEXT NOT NULL,"
        "  distance_m         REAL NOT NULL DEFAULT 0,"
        "  duration_s         REAL NOT NULL DEFAULT 0,"
        "  elapsed_s          REAL NOT NULL DEFAULT 0,"
        "  ele_gain_m         REAL,"
        "  vo2max             REAL,"
        "  avg_hr             REAL,"
        "  max_hr             REAL,"
        "  aerobic_te         REAL,"
        "  anaerobic_te       REAL,"
        "  avg_speed          REAL,"
        "  fastest_split_1609 REAL,"
        "  zone_s             TEXT NOT NULL DEFAULT '{}',"
        "  route              TEXT NOT NULL DEFAULT '{}',"
        "  name               TEXT,"
        "  has_intervals      INTEGER NOT NULL DEFAULT 0,"
        "  rpe_10             REAL,"
        "  feel               TEXT,"
        "  wx_temp            REAL,"
        "  wx_feels           REAL,"
        "  wx_humidity        REAL,"
        "  wx_wind            REAL,"
        "  wx_wind_dir        TEXT,"
        "  wx_desc            TEXT,"
        "  wx_unit            TEXT,"
        "  event_type         TEXT"
        ")"
    )
    conn.execute(
        "INSERT INTO activities (activity_id, activity_date, sport, distance_m, "
        "duration_s, elapsed_s, name) VALUES (9, '2026-03-01', 'running', 18000.0, "
        "6300.0, 6400.0, 'Old Row')"
    )
    conn.commit()
    conn.close()
    store = MetricStore(db)
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(activities)").fetchall()}
    assert "avg_cadence" in cols
    loaded = store.load_activities()
    store.close()
    assert len(loaded) == 1
    assert loaded[0].name == "Old Row"
    assert loaded[0].avg_cadence is None


def test_migrate_avg_cadence_idempotent_on_reopen(tmp_path):
    """Reopening an already-migrated DB does not duplicate the column."""
    db = str(tmp_path / "t.db")
    store = MetricStore(db)
    store.close()
    reopened = MetricStore(db)
    names = [r[1] for r in reopened.conn.execute("PRAGMA table_info(activities)").fetchall()]
    reopened.close()
    assert names.count("avg_cadence") == 1
