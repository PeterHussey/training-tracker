"""Orchestrate fetch->normalize->compute->store. Pure computation path: no network.

Ingested reference metrics (LT, race predictions) enter through optional
payload arguments — the gateway fetchers call them in, not the pipeline.
"""

from profile import RunnerProfile

import pandas as pd

from metric_series import rows_from_series
from metrics import acwr, elevation, pmc, racepredict, threshold, trimp, vo2max, volume
from normalize import Activity
from store import MetricStore


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
        weekly = volume.weekly_distance(activities, group)
        rows += rows_from_series(
            f"volume.distance_{group}", weekly, "computed", params={"agg": "iso_week", "unit": "km"}
        )
        rows += rows_from_series(
            f"volume.rolling4wk_{group}",
            volume.rolling_4wk(weekly),
            "computed",
            params={"agg": "iso_week", "unit": "km"},
        )
        rows += rows_from_series(
            f"volume.wow_pct_{group}",
            volume.week_over_week_pct(weekly),
            "computed",
            params={"agg": "iso_week", "unit": "fraction"},
        )
        weekly_hours = volume.weekly_duration_hours(activities, group)
        rows += rows_from_series(
            f"volume.duration_{group}",
            weekly_hours,
            "computed",
            params={"agg": "iso_week", "unit": "h"},
        )

    # Duration rolling + week-over-week for the total (mirrors the distance chart
    # overlay/change views). Duration feeds all sports, including no-distance
    # cross-training, so the total is a true time-anchored load proxy.
    total_hours = volume.weekly_duration_hours(activities, "total")
    rows += rows_from_series(
        "volume.rolling4wk_duration_total",
        volume.rolling_4wk(total_hours),
        "computed",
        params={"agg": "iso_week", "unit": "h"},
    )
    rows += rows_from_series(
        "volume.wow_pct_duration_total",
        volume.week_over_week_pct(total_hours),
        "computed",
        params={"agg": "iso_week", "unit": "fraction"},
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
        rows += rows_from_series(
            "load.acwr_pct",
            acwr.history_percentile(acwr_s, window=180)["history_pct"],
            "computed",
            params={"window": 180},
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
                    hr_series = pd.Series(
                        [d["hr"] for d in dots],
                        index=pd.DatetimeIndex([pd.Timestamp(d["date"]) for d in dots]),
                    )
                    pace_series = pd.Series(
                        [d["speed_m_s"] for d in dots],
                        index=pd.DatetimeIndex([pd.Timestamp(d["date"]) for d in dots]),
                    )
                    dots_params_hr = {"unit": "bpm", "basis": "best_window_outdoor_running"}
                    dots_params_pace = {"unit": "m/s", "basis": "best_window_outdoor_running"}
                    rows += rows_from_series(hr_key, hr_series, "computed", params=dots_params_hr)
                    rows += rows_from_series(
                        pace_key, pace_series, "computed", params=dots_params_pace
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
