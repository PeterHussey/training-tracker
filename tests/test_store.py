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
