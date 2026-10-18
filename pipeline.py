"""Orchestrate fetch->normalize->compute->store. Pure computation path: no network.

Ingested reference metrics (LT, race predictions) enter through optional
payload arguments — the gateway fetchers call them in, not the pipeline.
"""

import bisect
from profile import RunnerProfile

import pandas as pd

from metric_series import rows_from_series
from metrics import (
    acwr,
    age_grading,
    decoupling,
    effective_vo2max,
    elevation,
    gap,
    injury,
    marathon_shape,
    pmc,
    prognosis,
    racepredict,
    threshold,
    trimp,
    vdot_shape,
    vo2max,
    volume,
)
from normalize import Activity
from store import MetricStore

# Runalyze JD\Shape.php `VO2MAX_DAYS`: the shape window is 30 days.
VDOT_SHAPE_WINDOW_DAYS = 30


def _qualifying_window_days(
    activities: list[Activity], hrmax: float | None, index: pd.DatetimeIndex
) -> pd.DatetimeIndex:
    """Days in ``index`` whose trailing shape window holds a qualifying run.

    The port's emission rule: a date-keyed Runalyze series emits no row at all
    on a day whose 30-day window is empty, so a gap longer than the window
    stays a gap in the chart rather than a filler zero. Membership is decided
    exactly as ``vdot_shape.shape_at`` decides it -- same timestamp fallback
    and same end-of-day window boundary -- so the filter keeps precisely the
    days on which that function saw at least one qualifying activity.
    """
    stamps = sorted(vdot_shape._activity_ts(a) for a in vdot_shape.qualifying(activities, hrmax))
    if not stamps:
        return index[:0]

    kept = []
    for day in index:
        day_end = vdot_shape._day_end_ts(pd.Timestamp(day).to_pydatetime())
        window_start = day_end - VDOT_SHAPE_WINDOW_DAYS * 86_400
        position = bisect.bisect_left(stamps, window_start)
        if position < len(stamps) and stamps[position] <= day_end:
            kept.append(day)
    return pd.DatetimeIndex(kept)


def compute_metric_rows(
    activities: list[Activity],
    profile: RunnerProfile,
    lt_payload: dict | None = None,
    race_payload: dict | list | None = None,
    vo2max_payload: list[dict] | None = None,
    series_by_id: dict | None = None,
) -> list[dict]:
    """Compute every metric row for a session view. Pure: no DB, no network.

    All other computation in this module builds on this; it must stay
    behavior-identical to what run_pipeline writes to the metric_series table.
    """
    rows = []

    # Volume — weekly distance (km) + weekly duration (hours), 4-wk rolling,
    # week-over-week % (brief 4.1). Duration covers all sports including
    # cross-training, which has no distance but still burns time/energy.
    for group in volume.groups():
        daily = volume.daily_distance(activities, group)
        weekly = volume.resample_agg(daily, "W-SUN")
        rows += rows_from_series(
            f"volume.distance_{group}", weekly, "computed", params={"agg": "iso_week", "unit": "km"}
        )
        daily_hours = volume.daily_duration_hours(activities, group)
        weekly_hours = volume.resample_agg(daily_hours, "W-SUN")
        rows += rows_from_series(
            f"volume.duration_{group}",
            weekly_hours,
            "computed",
            params={"agg": "iso_week", "unit": "h"},
        )
        # Daily source rows (new source of truth, alongside existing weekly rows)
        if not daily.empty:
            rows += rows_from_series(
                f"volume.distance_{group}_daily",
                daily,
                "computed",
                params={"agg": "daily", "unit": "km"},
            )
        rows += rows_from_series(
            f"volume.duration_{group}_daily",
            daily_hours,
            "computed",
            params={"agg": "daily", "unit": "h"},
        )

    # Elevation — context only (brief 4.2)
    gain = elevation.daily_elevation_gain(activities, "running")
    rows += rows_from_series("elevation.daily_gain_running", gain, "computed", params={"unit": "m"})
    rows += rows_from_series(
        "elevation.rolling28d_running",
        elevation.rolling_28d(gain),
        "computed",
        params={"unit": "m"},
    )
    rows += rows_from_series(
        "elevation.gain_per_km_running",
        elevation.gain_per_km(activities, "running"),
        "computed",
        params={"unit": "m/km"},
    )

    # Grade-adjusted pace (running only) — energy-cost corrected speed
    # accounting for elevation gain/loss (brief 4.2, Minetti 2002).
    rows += rows_from_series(
        "gap.running",
        gap.gap_pace(activities),
        "computed",
        params={"unit": "m/s", "k": gap.K},
    )

    # Age grading (WMA 2023) — daily best graded pct over outdoor runs >= 3km.
    # Per-activity values are derived on the fly in session.py; only the daily
    # best persists (metric_series PK is (metric, date)).
    rows += rows_from_series(
        "age_graded.pct",
        age_grading.daily_best_graded(activities, profile),
        "computed",
        params={"unit": "fraction", "basis": "wma_2023"},
    )

    # Injury risk — longest safe run (single-run distance vs rolling 30d max).
    # Ratio > 1.10 = elevated risk, > 1.30 = high risk per injury study.
    ratio = injury.max_run_ratio(activities)
    if not ratio.empty:
        rows += rows_from_series(
            "injury.max_run_ratio",
            ratio,
            "computed",
            params={"unit": "ratio", "window_days": 30, "min_baseline": 7},
            flags={"basis": "running+treadmill"},
        )

    # HR load + PMC + ACWR (brief 1.2, 1.3, 1.1)
    # R15 scope: outdoor running, treadmill, and cross-training activities ALL
    # contribute HR load (TRIMP = duration * dHR, HR intensity — distance-irrelevant).
    # Per-sport daily Banister series feed the stacked TRIMP bar; a combined dense
    # daily total feeds the running-anchored PMC/ACWR windows so load is no longer
    # split apart across modalities.
    RUNNING = [a for a in activities if a.sport == "running"]
    TREADMILL = [a for a in activities if a.sport == "treadmill"]
    CROSS = [a for a in activities if a.sport == "cross"]
    ban_params = {
        "hrmax": profile.hrmax,
        "hrrest": profile.hrrest,
        "sex": profile.sex,
        "b": profile.banister_exponent(),
    }
    per_sport: list[tuple[str, str, pd.DataFrame]] = []
    for basis, sport_label, acts in (
        ("outdoor_running", "running", RUNNING),
        ("treadmill", "treadmill", TREADMILL),
        ("cross_training", "cross", CROSS),
    ):
        daily = trimp.daily_trimp(acts, profile)
        if daily.empty:
            continue
        per_sport.append((basis, sport_label, daily))
        rows += rows_from_series(
            f"load.banister_{sport_label}",
            daily["banister"][daily["banister"] > 0],
            "computed",
            params=ban_params,
            flags={"basis": basis, "hrmax_source": profile.hrmax_source},
        )
        rows += rows_from_series(
            f"load.edwards_{sport_label}", daily["edwards"], "computed", params={"basis": basis}
        )

    if per_sport:
        # Combined dense daily total across all HR-load sports.
        total_ban = pd.concat([d["banister"] for _, _, d in per_sport], axis=1, sort=False).sum(
            axis=1, min_count=1
        )
        total_edw = pd.concat([d["edwards"] for _, _, d in per_sport], axis=1, sort=False).sum(
            axis=1, min_count=1
        )
        ban = total_ban.reindex(
            pd.date_range(total_ban.index.min(), total_ban.index.max(), freq="D"), fill_value=0.0
        )
        rows += rows_from_series(
            "load.banister",
            total_ban[total_ban > 0],
            "computed",
            params=ban_params,
            flags={"basis": "combined", "hrmax_source": profile.hrmax_source},
        )
        rows += rows_from_series(
            "load.edwards", total_edw, "computed", params={"basis": "combined"}
        )
        rows += rows_from_series(
            "pmc", pmc.ctl_atl_tsb(ban), "computed", params={"tau_ctl": 42, "tau_atl": 7}
        )
        acwr_s = acwr.coupled_acwr(ban)
        rows += rows_from_series(
            "load.acwr", acwr_s, "computed", params={"acute": 7, "chronic": 28, "coupled": True}
        )

    # VO2max — ingested reference (brief 2.1). Uses the daily trend endpoint
    # /metrics-service/metrics/maxmet/daily/{start}/{end} (vo2MaxPreciseValue,
    # decimal). Empty only when the trend fetch returned nothing usable.
    rows += rows_from_series(
        "fitness.vo2max",
        vo2max.daily_vo2max_from_trend(vo2max_payload or []),
        "garmin_ingested",
        flags={"error_class": "firstbeat_estimate_5pct", "recompute": "no"},
    )

    # CS (approximate critical speed) — computed from fastestSplit_1609 (brief 2.2)
    rows += rows_from_series(
        "load.cs_approx",
        threshold.approx_cs_1609(activities),
        "computed",
        params={"basis": "fastestSplit_1609", "unit": "m/s"},
    )

    # LT + race predictions — ingested reference metrics, only when payloads supplied
    if lt_payload:
        lt = threshold.parse_lt(lt_payload)
        if lt.hr is not None and lt.date:
            rows += rows_from_series(
                "load.lt_hr",
                pd.Series([float(lt.hr)], index=pd.DatetimeIndex([pd.Timestamp(lt.date)])),
                "garmin_ingested",
                params={"unit": "bpm"},
                flags={"anchored": "hr", "error_class": "lt_hr_7pct"},
            )
        if lt.speed_m_s is not None and lt.date:
            rows += rows_from_series(
                "load.lt_pace",
                pd.Series([lt.speed_m_s], index=pd.DatetimeIndex([pd.Timestamp(lt.date)])),
                "garmin_ingested",
                params={"unit": "m/s"},
                flags={"anchored": "no", "error_class": "lt_pace_over_20pct"},
            )

    if race_payload:
        if isinstance(race_payload, list):
            # Daily history from /racepredictions/daily: one row per day present.
            for dist, series in racepredict.daily_predictions_from_trend(race_payload).items():
                if series.empty:
                    continue
                rows += rows_from_series(
                    f"race_{dist}",
                    series,
                    "garmin_ingested",
                    params={"unit": "s", "distance": dist},
                    flags={"error_class": "garmin_race_pred_maybe_optimistic"},
                )
        else:
            as_of = (
                race_payload.get("asOfDate")
                or race_payload.get("calendarDate")
                or pd.Timestamp.today().strftime("%Y-%m-%d")
            )
            for dist, secs in racepredict.parse_predictions(race_payload).items():
                if secs is None:
                    continue
                rows += rows_from_series(
                    f"race_{dist}",
                    pd.Series([float(secs)], index=pd.DatetimeIndex([pd.Timestamp(as_of)])),
                    "garmin_ingested",
                    params={"unit": "s", "distance": dist},
                    flags={"error_class": "garmin_race_pred_maybe_optimistic"},
                )

    # Best-effort LTHR anchors — emitted when Garmin LT payload missing
    has_lt_hr = lt_payload is not None and threshold.parse_lt(lt_payload).hr is not None
    if series_by_id is not None:
        anchors = threshold.best_effort_anchors(activities, series_by_id)
        if not has_lt_hr:
            for anchor_key, emit_key, w_s, factor in [
                ("w20", "best20", 1200, 0.95),
                ("w30", "best30", 1800, 0.97),
            ]:
                anchor = anchors[anchor_key]
                if anchor is not None:
                    dt = pd.Timestamp(anchor["date"])
                    params = {
                        "unit": "bpm",
                        "window_s": w_s,
                        "factor": factor,
                        "basis": "best_window_outdoor_running",
                    }
                    flags = {
                        "activity_id": anchor["activity_id"],
                        "error_class": "best_effort_estimate",
                    }
                    rows += rows_from_series(
                        f"load.lt_hr_{emit_key}",
                        pd.Series([anchor["proxy_hr"]], index=pd.DatetimeIndex([dt])),
                        "computed",
                        params=params,
                        flags=flags,
                    )
                    pace_params = {
                        "unit": "m/s",
                        "window_s": w_s,
                        "factor": factor,
                        "basis": "best_window_outdoor_running",
                    }
                    rows += rows_from_series(
                        f"load.lt_pace_{emit_key}",
                        pd.Series([anchor["speed_m_s"]], index=pd.DatetimeIndex([dt])),
                        "computed",
                        params=pace_params,
                        flags=flags,
                    )
            for dots_key, hr_key, pace_key in [
                ("dots20", "load.lt_effort_dots20_hr", "load.lt_effort_dots20_pace"),
                ("dots30", "load.lt_effort_dots30_hr", "load.lt_effort_dots30_pace"),
            ]:
                dots = anchors[dots_key]
                if dots:
                    idx = pd.DatetimeIndex([pd.Timestamp(d["date"]) for d in dots])
                    hr_series = pd.Series([d["hr"] for d in dots], index=idx)
                    pace_series = pd.Series([d["speed_m_s"] for d in dots], index=idx)
                    dots_params_hr = {"unit": "bpm", "basis": "best_window_outdoor_running"}
                    dots_params_pace = {"unit": "m/s", "basis": "best_window_outdoor_running"}
                    rows += rows_from_series(hr_key, hr_series, "computed", params=dots_params_hr)
                    rows += rows_from_series(
                        pace_key, pace_series, "computed", params=dots_params_pace
                    )

    # Aerobic decoupling — per-activity dots (brief 3.1). One dot per eligible
    # long run dated by activity; the route-cluster mean is emitted alongside
    # once the largest route cluster reaches 6 sessions (single-run values
    # are noise, so the dashboard only trends the mean).
    if series_by_id is not None:
        by_route: dict[tuple, list[tuple]] = {}
        for a in activities:
            if not decoupling.eligible_activity(a):
                continue
            entry = series_by_id.get(a.activity_id)
            if not entry:
                continue
            try:
                dec = decoupling.decoupling_timebased(*entry)
            except ValueError:
                continue
            rows += rows_from_series(
                "load.decoupling",
                pd.Series([dec], index=pd.DatetimeIndex([pd.Timestamp(a.date)])),
                "computed",
                params={"unit": "fraction"},
                flags={"activity_id": a.activity_id, "route_key": list(decoupling.route_key(a))},
            )
            by_route.setdefault(decoupling.route_key(a), []).append((a.date, dec))
        if by_route:
            best = max(by_route.values(), key=len)
            agg = decoupling.aggregate_decoupling([d for _, d in best])
            if agg is not None:
                latest = max(d for d, _ in best)
                rows += rows_from_series(
                    "load.decoupling_mean",
                    pd.Series([agg["mean"]], index=pd.DatetimeIndex([pd.Timestamp(latest)])),
                    "computed",
                    params={"unit": "fraction", "n": agg["n"], "stdev": agg["stdev"]},
                    flags={"basis": "route_cluster_mean"},
                )

    # Runalyze VDOT chain (spec docs/superpowers/specs/2026-10-17-...): 30-day
    # duration-weighted shape, the best-3 race corrector, marathon preparedness
    # and the race-prognosis table. Pure composition of the metrics/ port
    # modules -- no formula lives here. Races are the qualifying runs the user
    # tagged with Garmin's event type "race".
    hrmax = profile.hrmax
    runs = vdot_shape.qualifying(activities, hrmax)
    race_ratios: list[float] = []
    race_vdots: list[float] = []
    if runs and hrmax > 0:
        for a in runs:
            # `qualifying` already excludes missing HR; the check keeps the
            # division type-safe for mypy.
            if a.event_type != "race" or a.avg_hr is None:
                continue
            # Moving time (`duration_s`), never elapsed time (Review Focus 3).
            race_ratios.append(
                effective_vo2max.race_ratio(a.distance_km, a.duration_s, a.avg_hr / hrmax)
            )
            race_vdots.append(effective_vo2max.race_vdot_by_time(a.distance_km, a.duration_s))
    # The raw max of the best-3 ratios; 1.0 only when no race yields a positive
    # ratio (never floored at 1.0 -- a sub-1 factor is legitimate).
    corrector = effective_vo2max.corrector_factor(race_ratios, race_vdots=race_vdots)

    shape = vdot_shape.shape_series(
        activities, hrmax, window_days=VDOT_SHAPE_WINDOW_DAYS, corrector=corrector
    )
    if not shape.empty:
        # Drop empty-window days: a >30-day gap is a gap, never a zero row.
        shape = shape[_qualifying_window_days(activities, hrmax, shape.index)]
        rows += rows_from_series(
            "vdot.shape",
            shape,
            "computed",
            params={"window_days": VDOT_SHAPE_WINDOW_DAYS, "unit": "ml/kg/min"},
            flags={
                "basis": "duration_weighted_mean",
                "corrector": round(corrector, 6),
                "hrmax_source": profile.hrmax_source,
            },
        )

        # Marathon preparedness and race prognosis ride the same day set. The
        # targets resolve from the current (latest) corrected shape for every
        # as-of day; only the run windows are as-of each day
        # (BasicEndurance::asArray, spec 6).
        earliest_date = min(a.date for a in activities)
        target_vdot = float(shape.iloc[-1])
        shape_pct: list[float] = []
        raw_seconds: dict[float, list[float | None]] = {d: [] for d in prognosis.DISTS}
        damped_seconds: dict[float, list[float | None]] = {d: [] for d in prognosis.DISTS}
        for day, day_value in shape.items():
            as_of = pd.Timestamp(day).date()
            days_for_week_km = marathon_shape.clamped_week_days((as_of - earliest_date).days)
            history = [((as_of - a.date).days, a.distance_km) for a in runs if a.date <= as_of]
            pct = marathon_shape.percentage(history, target_vdot, days_for_week_km=days_for_week_km)
            shape_pct.append(pct)
            vdot_today = float(day_value)
            for distance in prognosis.DISTS:
                raw_seconds[distance].append(prognosis.prognosis_seconds(vdot_today, distance))
                damped_seconds[distance].append(
                    prognosis.prognosis_seconds(vdot_today, distance, marathon_shape_pct=pct)
                )

        rows += rows_from_series(
            "marathon.shape_pct",
            pd.Series(shape_pct, index=shape.index, dtype=float),
            "computed",
            params={
                "pct_week": marathon_shape.PERCENTAGE_WEEK_KM,
                "pct_lj": marathon_shape.PERCENTAGE_LONGJOGS,
                "unit": "pct",
            },
            flags={"basis": "basic_endurance", "target_vdot": round(target_vdot, 5)},
        )
        for distance in prognosis.DISTS:
            # `distance:g` keeps the plan's dotted keys (prognosis.1, .21.1);
            # the default str() of the float renders 1.0 / 10.0 / 50.0.
            key = f"prognosis.{distance:g}"
            params = {"distance_km": distance, "unit": "s"}
            rows += rows_from_series(
                key,
                pd.Series(raw_seconds[distance], index=shape.index, dtype=float),
                "computed",
                params=params,
                flags={"basis": "daniels_gilbert_bisection"},
            )
            rows += rows_from_series(
                f"{key}_damped",
                pd.Series(damped_seconds[distance], index=shape.index, dtype=float),
                "computed",
                params=params,
                flags={"basis": "daniels_gilbert_bisection", "damped_by": "marathon_shape"},
            )

    return rows


def persist_session_metrics(
    store: MetricStore,
    activities: list[Activity],
    profile: RunnerProfile,
    lt_payload: dict | None = None,
    race_payload: dict | list | None = None,
    vo2max_payload: list[dict] | None = None,
    series_by_id: dict | None = None,
) -> int:
    """Compute the session metric rows and persist them to an open store.

    The dashboard builds its in-memory view via compute_metric_rows but
    historically never wrote those rows back, leaving metric_series (and
    therefore race_* for direct DB readers) empty. Call this after the view
    is built so the store mirrors what the UI shows.     Returns rows written.
    """
    rows = compute_metric_rows(
        activities, profile, lt_payload, race_payload, vo2max_payload, series_by_id
    )
    store.save_metric_rows(rows)
    return len(rows)


def run_pipeline(
    activities: list[Activity],
    profile: RunnerProfile,
    out_db,
    lt_payload: dict | None = None,
    race_payload: dict | list | None = None,
    vo2max_payload: list[dict] | None = None,
    series_by_id: dict | None = None,
) -> dict[str, int]:
    store = MetricStore(out_db)
    store.save_runner_profile(profile)
    store.save_activities(activities)
    rows = compute_metric_rows(
        activities, profile, lt_payload, race_payload, vo2max_payload, series_by_id
    )
    store.save_metric_rows(rows)
    store.close()
    return {"metrics_written": len(rows), "activities": len(activities)}
