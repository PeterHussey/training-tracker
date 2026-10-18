"""Tests for backfill_event_types.py: the pure planner and the CLI repair path."""

import backfill_event_types
from backfill_event_types import plan_event_type_updates
from store import MetricStore


def _summary(aid, type_key=None, *, with_event_type=True):
    s = {"activityId": aid}
    if with_event_type:
        if type_key is None or isinstance(type_key, dict):
            s["eventType"] = type_key if isinstance(type_key, dict) else {}
        elif type_key is False:  # non-dict payload (malformed shape)
            s["eventType"] = "race"
        else:
            s["eventType"] = {"typeKey": type_key}
    return s


def test_plan_writes_race_over_null():
    stored: dict[int, str | None] = {1: None}
    raw: list[dict] = [_summary(1, "race")]
    assert plan_event_type_updates(stored, raw) == [(1, "race")]


def test_plan_writes_uncategorized_over_null():
    stored: dict[int, str | None] = {1: None}
    raw: list[dict] = [_summary(1, "uncategorized")]
    assert plan_event_type_updates(stored, raw) == [(1, "uncategorized")]


def test_plan_mirrors_downgrade_race_to_uncategorized():
    stored: dict[int, str | None] = {1: "race"}
    raw: list[dict] = [_summary(1, "uncategorized")]
    assert plan_event_type_updates(stored, raw) == [(1, "uncategorized")]


def test_plan_skips_equal_value():
    stored: dict[int, str | None] = {1: "race"}
    raw: list[dict] = [_summary(1, "race")]
    assert plan_event_type_updates(stored, raw) == []


def test_plan_skips_absent_empty_or_malformed_event_type():
    stored: dict[int, str | None] = {1: None, 2: None, 3: None, 4: None}
    raw: list[dict] = [
        _summary(1, with_event_type=False),  # key absent from the payload
        _summary(2, {}),  # eventType without typeKey
        _summary(3, False),  # eventType is not a dict
        _summary(4, ""),  # empty-string typeKey
    ]
    assert plan_event_type_updates(stored, raw) == []


def test_plan_skips_unknown_or_non_int_activity_id():
    stored: dict[int, str | None] = {1: None, 2: None}
    raw: list[dict] = [
        {"activityId": 999, "eventType": {"typeKey": "race"}},  # not in store
        {"activityId": None, "eventType": {"typeKey": "race"}},  # absent id
        {"eventType": {"typeKey": "race"}},  # no activityId key
        {"activityId": "not-a-number", "eventType": {"typeKey": "race"}},  # non-int-able
    ]
    assert plan_event_type_updates(stored, raw) == []


def test_plan_output_is_sorted_by_activity_id():
    stored: dict[int, str | None] = {3: None, 1: None, 2: None}
    raw: list[dict] = [_summary(3, "race"), _summary(1, "race"), _summary(2, "uncategorized")]
    assert plan_event_type_updates(stored, raw) == [
        (1, "race"),
        (2, "uncategorized"),
        (3, "race"),
    ]


# --- main() tests: no network; fetch_range is monkeypatched in the script's
# --- module namespace and GarminGateway is stubbed (its constructor resolves
# --- 1Password/tokenstore auth).


class _FakeGw:
    auth_path = "test"


def _patch_fetch(monkeypatch, payload):
    calls: list = []

    def fake_fetch_range(gw, start, end):
        calls.append((start, end))
        return list(payload)

    monkeypatch.setattr(backfill_event_types, "GarminGateway", lambda cache_dir: _FakeGw())
    monkeypatch.setattr(backfill_event_types, "fetch_range", fake_fetch_range)
    return calls


def _seed(store, rows):
    store.conn.executemany(
        "INSERT INTO activities (activity_id, activity_date, sport, name, rpe_10, "
        "feel, wx_temp, event_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    store.conn.commit()
    return store


def test_main_dry_run_no_write_and_fetch_window_matches_store(tmp_path, monkeypatch, capsys):
    store = _seed(
        MetricStore(str(tmp_path / "t.sqlite")),
        [(1, "2024-01-01", "running", None, None, None, None, None)],
    )
    store.conn.execute(
        "INSERT INTO activities (activity_id, activity_date, sport, event_type)"
        " VALUES (2, '2024-03-05', 'running', NULL)"
    )
    store.conn.commit()
    calls = _patch_fetch(monkeypatch, [_summary(1, "race")])

    rc = backfill_event_types.main(["--db", str(tmp_path / "t.sqlite")])

    out = capsys.readouterr().out
    assert rc == 0
    assert calls == [(backfill_event_types.date(2024, 1, 1), backfill_event_types.date(2024, 3, 5))]
    assert (
        store.conn.execute("SELECT event_type FROM activities WHERE activity_id = 1").fetchone()[0]
        is None
    )
    assert "dry run" in out
    assert "1 event_type updates" in out


def test_main_apply_writes_event_type_only(tmp_path, monkeypatch, capsys):
    store = _seed(
        MetricStore(str(tmp_path / "t.sqlite")),
        [(1, "2024-03-05", "running", "Morning Run", 6.5, "Good", 12.0, None)],
    )
    before = store.conn.execute(
        "SELECT name, sport, rpe_10, feel, wx_temp FROM activities WHERE activity_id = 1"
    ).fetchone()
    calls = _patch_fetch(monkeypatch, [_summary(1, "race")])

    rc = backfill_event_types.main(["--db", str(tmp_path / "t.sqlite"), "--apply"])

    assert rc == 0
    assert calls  # fetched (a repair without a fetch would be a no-op test)
    after = store.conn.execute(
        "SELECT name, sport, rpe_10, feel, wx_temp FROM activities WHERE activity_id = 1"
    ).fetchone()
    event_type = store.conn.execute(
        "SELECT event_type FROM activities WHERE activity_id = 1"
    ).fetchone()[0]
    assert event_type == "race"
    # Ruling-2 regression: a swap back to store.save_activities would NULL
    # rpe_10/feel/wx_* because the activity-list payload carries none of them.
    assert after == before == ("Morning Run", "running", 6.5, "Good", 12.0)
    assert "dry run" not in capsys.readouterr().out


def test_main_empty_store_exits_before_fetch(tmp_path, monkeypatch, capsys):
    MetricStore(str(tmp_path / "t.sqlite"))
    calls = _patch_fetch(monkeypatch, [])

    rc = backfill_event_types.main(["--db", str(tmp_path / "t.sqlite")])

    out = capsys.readouterr().out
    assert rc == 0
    assert calls == []
    assert "store is empty" in out


def test_main_start_end_overrides_honoured(tmp_path, monkeypatch, capsys):
    _seed(
        MetricStore(str(tmp_path / "t.sqlite")),
        [
            (1, "2024-01-01", "running", None, None, None, None, None),
            (2, "2024-06-01", "running", None, None, None, None, None),
        ],
    )
    calls = _patch_fetch(monkeypatch, [])

    rc = backfill_event_types.main(
        ["--db", str(tmp_path / "t.sqlite"), "--start", "2024-02-01", "--end", "2024-02-20"]
    )

    assert rc == 0
    assert calls == [
        (backfill_event_types.date(2024, 2, 1), backfill_event_types.date(2024, 2, 20))
    ]
