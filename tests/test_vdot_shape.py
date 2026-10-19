r"""Golden tests for the ``JD\Shape`` port (daily effective VO2max + 30-day shape).

Numbers come from the upstream ``JD\ShapeTest``. That test inserts the
per-activity ``vo2max`` column straight into SQLite, so the ShapeTest goldens
that pin a stored value stub :func:`metrics.vdot_shape.activity_vo2max` (the
per-activity estimate itself is Task 3's concern); every other test drives the
real pace+HR path. Activities are synthetic -- no fixture file.
"""

from datetime import datetime, time, timedelta

import pandas as pd
import pytest

from metrics import effective_vo2max, vdot_shape
from normalize import Activity

HRMAX = 185.0
NOW = datetime(2026, 10, 17, 12, 0)  # as-of reference; its day ends 23:59:59


def _activity(
    activity_id: int = 1,
    *,
    sport: str = "running",
    day: datetime = NOW,
    distance_m: float = 5000.0,
    duration_s: float = 1500.0,
    avg_hr: float | None = 150.0,
    ele_gain_m: float | None = None,
    ele_loss_m: float | None = None,
) -> Activity:
    """Qualifying-by-default activity: 5 km in 25 min (200 m/min) at 150 bpm."""
    return Activity(
        activity_id=activity_id,
        sport=sport,
        date=day.date(),
        ts_ms=int(day.timestamp() * 1000),
        distance_m=distance_m,
        duration_s=duration_s,
        elapsed_s=duration_s,
        avg_hr=avg_hr,
        max_hr=None,
        ele_gain_m=ele_gain_m,
        ele_loss_m=ele_loss_m,
    )


def _stub_values(monkeypatch: pytest.MonkeyPatch, values: dict[int, float]) -> None:
    """Pin per-activity values, as ``ShapeTest`` pins the ``vo2max`` column."""

    def fake(
        a: Activity, hrmax: float, corrector: float = 1.0, *, use_elevation: bool = False
    ) -> float:
        return values[a.activity_id] * corrector

    monkeypatch.setattr(vdot_shape, "activity_vo2max", fake)


def _effective(distance_km: float, seconds: float, avg_hr: float) -> float:
    """Expected uncorrected value for a pace+HR triple at ``HRMAX``."""
    return effective_vo2max.from_pace_and_hr(distance_km, seconds, avg_hr / HRMAX)


# --- ShapeTest goldens (stored values pinned) -------------------------------


def test_shape_weighted_mean_same_day(monkeypatch):
    # ShapeTest::testSimpleCalculation: s = 1, 2, 1 s with vo2max 50, 60, 80 on
    # one day -> (50*1 + 60*2 + 80*1) / 4 = 62.5 (duration-weighted, not a mean).
    _stub_values(monkeypatch, {1: 50.0, 2: 60.0, 3: 80.0})
    runs = [
        _activity(1, duration_s=1.0, distance_m=5.0),
        _activity(2, duration_s=2.0, distance_m=10.0),
        _activity(3, duration_s=1.0, distance_m=5.0),
    ]
    assert vdot_shape.shape_at(runs, NOW, HRMAX) == 62.5


def test_use_zero_row_dropped(monkeypatch):
    # ShapeTest::testUseFlag: the row Runalyze flags off (`use_vo2max` = 0) is
    # ignored. Its analogue here is a row without a usable duration.
    _stub_values(monkeypatch, {1: 50.0, 2: 80.0})
    counted = _activity(1, duration_s=1.0, distance_m=5.0)
    dropped = _activity(2, duration_s=0.0, distance_m=0.0)

    assert [a.activity_id for a in vdot_shape.qualifying([counted, dropped], HRMAX)] == [1]
    assert vdot_shape.shape_at([counted, dropped], NOW, HRMAX) == 50.0


def test_outside_window_dropped(monkeypatch):
    # ShapeTest::testVO2maxDays: a 10-day-old row is outside a 5-day window.
    _stub_values(monkeypatch, {1: 50.0, 2: 80.0})
    fresh = _activity(1)
    old = _activity(2, day=NOW - timedelta(days=10))

    assert vdot_shape.shape_at([fresh, old], NOW, HRMAX, window_days=5) == 50.0
    # the default 30-day window still holds both; equal durations -> plain mean
    assert vdot_shape.shape_at([fresh, old], NOW, HRMAX) == 65.0

    # The window is [day end - window_days, day end] in timestamps, exactly as
    # Shape.php's `time BETWEEN ($time - days*DAY_IN_S) AND $time`: an activity
    # 5 days earlier at 12:00 misses the boundary, one 4 days earlier is inside.
    fifth = _activity(2, day=NOW - timedelta(days=5))
    assert vdot_shape.shape_at([fresh, fifth], NOW, HRMAX, window_days=5) == 50.0
    fourth = _activity(2, day=NOW - timedelta(days=4))
    assert vdot_shape.shape_at([fresh, fourth], NOW, HRMAX, window_days=5) == 65.0


def test_corrector_scales(monkeypatch):
    # ShapeTest::testCorrector: 50 * 0.9 = 45.
    _stub_values(monkeypatch, {1: 50.0})
    assert vdot_shape.shape_at([_activity(1)], NOW, HRMAX, corrector=0.9) == 45.0


# --- Empty windows and guards ----------------------------------------------


def test_empty_window_is_zero_not_filler():
    assert vdot_shape.shape_at([], NOW, HRMAX) == 0.0

    # An activity older than the window contributes nothing: 0.0 is the real
    # aggregate of an empty window, never a carried-forward value.
    stale = [_activity(1, day=NOW - timedelta(days=31))]
    assert vdot_shape.shape_at(stale, NOW, HRMAX) == 0.0

    # Nothing qualifies -> no rows at all rather than a fabricated zero row.
    assert vdot_shape.shape_series([], HRMAX).empty
    assert vdot_shape.shape_series([_activity(1, sport="cross")], HRMAX).empty


def test_hr_over_hrmax_excluded():
    too_high = _activity(1, avg_hr=190.0)  # above HRmax -> no valid value
    at_max = _activity(2, avg_hr=HRMAX)  # inclusive boundary
    ok = _activity(3, avg_hr=150.0)

    assert [a.activity_id for a in vdot_shape.qualifying([too_high, at_max, ok], HRMAX)] == [
        2,
        3,
    ]
    assert vdot_shape.shape_at([too_high], NOW, HRMAX) == 0.0
    assert vdot_shape.shape_at([too_high, ok], NOW, HRMAX) == pytest.approx(
        vdot_shape.activity_vo2max(ok, HRMAX)
    )


def test_zero_value_row_does_not_dilute():
    # Shape.php sums `s*use_vo2max*(vo2max > 0)`, so a row Runalyze would store
    # with vo2max = 0 -- here a failed HR strap, avg_hr = 0 -- is not counted.
    valid = _activity(1, avg_hr=150.0)
    no_hr = _activity(2, avg_hr=0.0)

    assert vdot_shape.activity_vo2max(no_hr, HRMAX) == 0.0
    assert [a.activity_id for a in vdot_shape.qualifying([valid, no_hr], HRMAX)] == [1, 2]
    assert vdot_shape.shape_at([valid, no_hr], NOW, HRMAX) == vdot_shape.shape_at(
        [valid], NOW, HRMAX
    )


def test_series_without_hrmax_or_runs_is_empty():
    runs = [_activity(1)]

    assert vdot_shape.qualifying(runs, None) == []
    assert vdot_shape.shape_series(runs, None).empty
    assert vdot_shape.shape_at(runs, NOW, None) == 0.0


# --- Real pace+HR path ------------------------------------------------------


def test_shape_at_is_duration_weighted_mean():
    short = _activity(1, distance_m=4000.0, duration_s=1200.0, avg_hr=160.0)
    long = _activity(2, distance_m=10_000.0, duration_s=3600.0, avg_hr=145.0)
    total = short.duration_s + long.duration_s
    expected = (
        vdot_shape.activity_vo2max(short, HRMAX) * short.duration_s
        + vdot_shape.activity_vo2max(long, HRMAX) * long.duration_s
    ) / total

    assert vdot_shape.shape_at([short, long], NOW, HRMAX) == pytest.approx(
        round(expected, 5), rel=1e-12
    )


def test_corrector_scales_computed_shape():
    runs = [_activity(1, distance_m=5000.0, duration_s=1500.0, avg_hr=150.0)]
    base = vdot_shape.shape_at(runs, NOW, HRMAX)

    # The corrector is applied per activity, before the 5-decimal rounding, so
    # the corrected shape may differ from `0.9 * base` by up to half a quantum.
    assert base > 0.0
    assert vdot_shape.shape_at(runs, NOW, HRMAX, corrector=0.9) == pytest.approx(
        0.9 * base, abs=1e-5
    )


def test_qualifying_velocity_guard():
    slow = _activity(1, distance_m=1000.0, duration_s=1200.0)  # 50 m/min, inclusive
    too_slow = _activity(2, distance_m=1000.0, duration_s=1201.0)
    fast = _activity(3, distance_m=1000.0, duration_s=60.0)  # 1000 m/min, inclusive
    too_fast = _activity(4, distance_m=1000.0, duration_s=59.0)
    no_distance = _activity(5, distance_m=0.0)
    cross = _activity(6, sport="cross")
    treadmill = _activity(7, sport="treadmill")
    everything = [slow, too_slow, fast, too_fast, no_distance, cross, treadmill]

    assert [a.activity_id for a in vdot_shape.qualifying(everything, HRMAX)] == [1, 3, 7]


def test_elevation_variant_goes_up_or_down():
    flat = _activity(1, distance_m=10_000.0, duration_s=3000.0, avg_hr=150.0)
    uphill = _activity(
        2, distance_m=10_000.0, duration_s=3000.0, avg_hr=150.0, ele_gain_m=100.0, ele_loss_m=0.0
    )
    downhill = _activity(
        3, distance_m=10_000.0, duration_s=3000.0, avg_hr=150.0, ele_gain_m=0.0, ele_loss_m=100.0
    )

    flat_value = vdot_shape.activity_vo2max(flat, HRMAX)
    # off by default, even for an activity that carries elevation
    assert vdot_shape.activity_vo2max(uphill, HRMAX) == pytest.approx(flat_value)
    assert vdot_shape.activity_vo2max(uphill, HRMAX, use_elevation=False) == pytest.approx(
        flat_value
    )

    up = vdot_shape.activity_vo2max(uphill, HRMAX, use_elevation=True)
    down = vdot_shape.activity_vo2max(downhill, HRMAX, use_elevation=True)
    assert up > flat_value > down

    # Runalyze's DistanceModifier corrects the *distance*: +2 m per metre up,
    # -1 m per metre down (VO2max.php), so 10 km becomes 10.2 / 9.9 km.
    assert up == pytest.approx(_effective(10.2, 3000.0, 150.0))
    assert down == pytest.approx(_effective(9.9, 3000.0, 150.0))


def test_missing_descent_falls_back_to_ascent():
    """Runalyze's no-route fallback: a single elevation value serves both terms.

    Without route data Runalyze passes the activity's elevation for BOTH up and
    down, so ``ele_loss_m=None`` must equal an explicit same-valued descent --
    with the old ``0.0`` fallback this overshoots (+2 m/m climbed, no offset).
    """
    gain_only = _activity(1, ele_gain_m=100.0, ele_loss_m=None)
    gain_and_loss = _activity(2, ele_gain_m=100.0, ele_loss_m=100.0)
    flat = _activity(3)

    assert vdot_shape.activity_vo2max(gain_only, HRMAX, use_elevation=True) == pytest.approx(
        vdot_shape.activity_vo2max(gain_and_loss, HRMAX, use_elevation=True)
    )
    # (2 * 100 + -1 * 100) / 1000 = 0.1 km of corrected distance, not 0.2.
    v = vdot_shape.activity_vo2max(gain_only, HRMAX, use_elevation=True)
    assert v > vdot_shape.activity_vo2max(flat, HRMAX)
    assert v == pytest.approx(_effective(5000.0 / 1000.0 + 0.1, 1500.0, 150.0))


def test_golden_elevation_corrected_run_2026_10_18():
    """Owner's Runalyze shows 42.98 for the 2026-10-18 "Concord - ER9" run.

    Our port with Garmin's own ascent/descent gives 42.973; the uncorrected
    value (what we printed before the elevation correction) is ~42.64.
    """
    a = _activity(
        1,
        distance_m=14824.75,
        duration_s=5403.0,
        avg_hr=143.0,
        ele_gain_m=93.26,
        ele_loss_m=93.29,
    )
    corrected = vdot_shape.activity_vo2max(a, 184.0, use_elevation=True)
    uncorrected = vdot_shape.activity_vo2max(a, 184.0, use_elevation=False)

    assert corrected == pytest.approx(42.97, abs=0.02)
    assert corrected != pytest.approx(uncorrected)

    series_on = vdot_shape.shape_series([a], 184.0, use_elevation=True)
    series_off = vdot_shape.shape_series([a], 184.0, use_elevation=False)
    assert (series_on != series_off).any()


# --- Series emission (Ruling 6) --------------------------------------------


def test_series_covers_each_day_and_fills_gaps_with_zero():
    window = 5
    runs = [
        _activity(1, day=datetime(2026, 10, 1, 7, 0)),
        _activity(2, day=datetime(2026, 10, 4, 7, 0)),
        _activity(3, day=datetime(2026, 10, 20, 7, 0)),
    ]
    series = vdot_shape.shape_series(runs, HRMAX, window_days=window)

    assert series.name == "vdot_shape"
    assert series.index[0] == pd.Timestamp("2026-10-01")
    assert series.index[-1] == pd.Timestamp("2026-10-20")
    assert len(series) == 20  # one row per calendar day, no gaps
    assert not series.isna().any()

    assert series.loc[pd.Timestamp("2026-10-04")] > 0.0
    # Oct 9 is five days after the Oct 4 run: its window is empty -> real 0.0
    assert series.loc[pd.Timestamp("2026-10-09")] == 0.0

    for day in series.index:
        expected = vdot_shape.shape_at(
            runs, datetime.combine(day.date(), time(12, 0)), HRMAX, window_days=window
        )
        assert series.loc[day] == expected


def test_series_spans_first_qualifying_run_to_last_activity():
    activities = [
        _activity(1, sport="cross", day=datetime(2026, 10, 1, 7, 0)),
        _activity(2, day=datetime(2026, 10, 3, 7, 0)),
        _activity(
            3,
            sport="strength",
            day=datetime(2026, 10, 6, 7, 0),
            distance_m=0.0,
            avg_hr=None,
        ),
    ]
    series = vdot_shape.shape_series(activities, HRMAX, window_days=1)

    # nothing before the first qualifying run
    assert series.index[0] == pd.Timestamp("2026-10-03")
    # ... but one row per day through the last activity's date
    assert series.index[-1] == pd.Timestamp("2026-10-06")
    assert len(series) == 4

    assert series.loc[pd.Timestamp("2026-10-03")] > 0.0
    assert series.loc[pd.Timestamp("2026-10-04")] == 0.0
