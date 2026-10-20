"""Tests for backfill_cadence.py: the pure planner and the CLI repair path."""

import backfill_cadence
from backfill_cadence import plan_cadence_updates
from store import MetricStore


def _summary(aid, cadence=None, *, with_cadence=True):
    s = {"activityId": aid}
    if with_cadence and cadence is not None:
        s["averageRunningCadenceInStepsPerMinute"] = cadence
    return s


def test_plan_writes_cadence_over_null():
    stored: dict[int, float | None] = {1: None}
    raw: list[dict] = [_summary(1, 184.59)]
    assert plan_cadence_updates(stored, raw) == [(1, 184.59)]


def test_plan_skips_equal_value():
    stored: dict[int, float | None] = {1: 184.59}
    raw: list[dict] = [_summary(1, 184.59)]
    assert plan_cadence_updates(stored, raw) == []


def test_plan_skips_absent_cadence_and_never_blanks_a_stored_value():
    stored: dict[int, float | None] = {1: 175.0, 2: None}
    raw: list[dict] = [_summary(1, with_cadence=False), _summary(2, with_cadence=False)]
    assert plan_cadence_updates(stored, raw) == []


def test_plan_skips_non_numeric_cadence():
    stored: dict[int, float | None] = {1: None}
    raw: list[dict] = [_summary(1, "fast")]
    assert plan_cadence_updates(stored, raw) == []


def test_plan_skips_unknown_or_non_int_activity_id():
    stored: dict[int, float | None] = {1: None, 2: None}
    raw: list[dict] = [
        _summary(999, 180.0),  # not in store
        _summary(None, 180.0),  # absent id
        {"averageRunningCadenceInStepsPerMinute": 180.0},  # no activityId key
        _summary("not-a-number", 180.0),  # non-int-able
    ]
    assert plan_cadence_updates(stored, raw) == []


def test_plan_output_is_sorted_by_activity_id():
    stored: dict[int, float | None] = {3: None, 1: None, 2: None}
    raw: list[dict] = [_summary(3, 180.0), _summary(1, 175.0), _summary(2, 178.0)]
    assert plan_cadence_updates(stored, raw) == [(1, 175.0), (2, 178.0), (3, 180.0)]


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

    monkeypatch.setattr(backfill_cadence, "GarminGateway", lambda cache_dir: _FakeGw())
    monkeypatch.setattr(backfill_cadence, "fetch_range", fake_fetch_range)
    return calls


def _seed(store, rows):
    store.conn.executemany(
        "INSERT INTO activities (activity_id, activity_date, sport, name, rpe_10, "
        "feel, wx_temp, avg_cadence) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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
        "INSERT INTO activities (activity_id, activity_date, sport, avg_cadence)"
        " VALUES (2, '2024-03-05', 'running', NULL)"
    )
    store.conn.commit()
    calls = _patch_fetch(monkeypatch, [_summary(1, 184.59)])

    rc = backfill_cadence.main(["--db", str(tmp_path / "t.sqlite")])

    out = capsys.readouterr().out
    assert rc == 0
    assert calls == [(backfill_cadence.date(2024, 1, 1), backfill_cadence.date(2024, 3, 5))]
    assert (
        store.conn.execute("SELECT avg_cadence FROM activities WHERE activity_id = 1").fetchone()[0]
        is None
    )
    assert "dry run" in out
    assert "1 avg_cadence updates" in out


def test_main_apply_writes_avg_cadence_only(tmp_path, monkeypatch, capsys):
    store = _seed(
        MetricStore(str(tmp_path / "t.sqlite")),
        [(1, "2024-03-05", "running", "Morning Run", 6.5, "Good", 12.0, None)],
    )
    before = store.conn.execute(
        "SELECT name, sport, rpe_10, feel, wx_temp FROM activities WHERE activity_id = 1"
    ).fetchone()
    calls = _patch_fetch(monkeypatch, [_summary(1, 184.59)])

    rc = backfill_cadence.main(["--db", str(tmp_path / "t.sqlite"), "--apply"])

    assert rc == 0
    assert calls  # fetched (a repair without a fetch would be a no-op test)
    after = store.conn.execute(
        "SELECT name, sport, rpe_10, feel, wx_temp FROM activities WHERE activity_id = 1"
    ).fetchone()
    cadence = store.conn.execute(
        "SELECT avg_cadence FROM activities WHERE activity_id = 1"
    ).fetchone()[0]
    assert cadence == 184.59
    # Ruling-2 regression: a swap back to store.save_activities would NULL
    # rpe_10/feel/wx_* because the activity-list payload carries none of them.
    assert after == before == ("Morning Run", "running", 6.5, "Good", 12.0)
    assert "dry run" not in capsys.readouterr().out


def test_main_empty_store_exits_before_fetch(tmp_path, monkeypatch, capsys):
    MetricStore(str(tmp_path / "t.sqlite"))
    calls = _patch_fetch(monkeypatch, [])

    rc = backfill_cadence.main(["--db", str(tmp_path / "t.sqlite")])

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

    rc = backfill_cadence.main(
        ["--db", str(tmp_path / "t.sqlite"), "--start", "2024-02-01", "--end", "2024-02-20"]
    )

    assert rc == 0
    assert calls == [(backfill_cadence.date(2024, 2, 1), backfill_cadence.date(2024, 2, 20))]
