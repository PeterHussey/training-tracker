"""Orchestrate fetch->normalize->compute->store. Pure computation path: no network.

Ingested reference metrics (LT, race predictions) enter through optional
payload arguments — the gateway fetchers call them in, not the pipeline.
"""
import pandas as pd

from metric_series import rows_from_series
from metrics import acwr, elevation, pmc, threshold, trimp, vo2max, volume
from metrics import racepredict
from normalize import Activity
from profile import RunnerProfile
from store import MetricStore


def run_pipeline(activities: list[Activity], profile: RunnerProfile, out_db,
                 lt_payload: dict | None = None, race_payload: dict | None = None) -> dict[str, int]:
    store = MetricStore(out_db)
    store.save_runner_profile(profile)
    store.save_activities(activities)

    rows = []

    # Volume — weekly distance (km), 4-wk rolling, week-over-week % (brief 4.1)
    for group in volume.groups():
        weekly = volume.weekly_distance(activities, group)
        rows += rows_from_series(f"volume.distance_{group}", weekly, "computed",
                                 params={"agg": "iso_week", "unit": "km"})
        rows += rows_from_series(f"volume.rolling4wk_{group}", volume.rolling_4wk(weekly), "computed",
                                 params={"agg": "iso_week", "unit": "km"})
        rows += rows_from_series(f"volume.wow_pct_{group}", volume.week_over_week_pct(weekly), "computed",
                                 params={"agg": "iso_week", "unit": "fraction"})

    # Elevation — context only (brief 4.2)
    gain = elevation.daily_elevation_gain(activities, "running")
    rows += rows_from_series("elevation.daily_gain_running", gain, "computed", params={"unit": "m"})
    rows += rows_from_series("elevation.rolling28d_running", elevation.rolling_28d(gain), "computed",
                             params={"unit": "m"})
    rows += rows_from_series("elevation.gain_per_km_running",
                             elevation.gain_per_km(activities, "running"), "computed",
                             params={"unit": "m/km"})

    # HR load + PMC + ACWR (brief 1.2, 1.3, 1.1)
    # Global Constraint: running metrics are computed on outdoor-`running`
    # activities only; treadmill/cycling/strength feed cross-training volume
    # (volume.groups()) only, never the HR-load anchors.
    RUNNING = [a for a in activities if a.sport == "running"]
    daily = trimp.daily_trimp(RUNNING, profile)
    if not daily.empty:
        ban = daily["banister"]
        # DENSIFY to calendar days (load 0 on rest days) so PMC/ACWR windows are
        # calendar windows, not active-day windows.
        ban = ban.reindex(pd.date_range(ban.index.min(), ban.index.max(), freq="D"), fill_value=0.0)
        rows += rows_from_series("load.banister",
                                 ban[ban > 0], "computed",
                                 params={"hrmax": profile.hrmax, "hrrest": profile.hrrest,
                                         "sex": profile.sex, "b": profile.banister_exponent()},
                                 flags={"hrmax_source": profile.hrmax_source})
        rows += rows_from_series("load.edwards", daily["edwards"], "computed")
        rows += rows_from_series("pmc", pmc.ctl_atl_tsb(ban), "computed",
                                 params={"tau_ctl": 42, "tau_atl": 7})
        acwr_s = acwr.coupled_acwr(ban)
        rows += rows_from_series("load.acwr", acwr_s, "computed",
                                 params={"acute": 7, "chronic": 28, "coupled": True})
        rows += rows_from_series("load.acwr_pct",
                                 acwr.history_percentile(acwr_s, window=180)["history_pct"], "computed",
                                 params={"window": 180})

    # VO2max — ingested reference (brief 2.1)
    rows += rows_from_series("fitness.vo2max", vo2max.daily_vo2max(activities), "garmin_ingested",
                             flags={"error_class": "firstbeat_estimate_5pct", "recompute": "no"})

    # CS (approximate critical speed) — computed from fastestSplit_1609 (brief 2.2)
    rows += rows_from_series("load.cs_approx", threshold.approx_cs_1609(activities), "computed",
                             params={"basis": "fastestSplit_1609", "unit": "m/s"})

    # LT + race predictions — ingested reference metrics, only when payloads supplied
    if lt_payload:
        lt = threshold.parse_lt(lt_payload)
        if lt.hr is not None and lt.date:
            rows += rows_from_series("load.lt_hr",
                                     pd.Series([float(lt.hr)],
                                               index=pd.DatetimeIndex([pd.Timestamp(lt.date)])),
                                     "garmin_ingested", params={"unit": "bpm"},
                                     flags={"anchored": "hr", "error_class": "lt_hr_7pct"})
        if lt.speed_m_s is not None and lt.date:
            rows += rows_from_series("load.lt_pace",
                                     pd.Series([lt.speed_m_s],
                                               index=pd.DatetimeIndex([pd.Timestamp(lt.date)])),
                                     "garmin_ingested", params={"unit": "m/s"},
                                     flags={"anchored": "no", "error_class": "lt_pace_over_20pct"})

    if race_payload:
        as_of = race_payload.get("asOfDate") or pd.Timestamp.today().strftime("%Y-%m-%d")
        for dist, secs in racepredict.parse_predictions(race_payload).items():
            if secs is None:
                continue
            rows += rows_from_series(f"race_{dist}",
                                     pd.Series([float(secs)],
                                               index=pd.DatetimeIndex([pd.Timestamp(as_of)])),
                                     "garmin_ingested", params={"unit": "s", "distance": dist},
                                     flags={"error_class": "garmin_race_pred_maybe_optimistic"})

    store.save_metric_rows(rows)
    store.close()
    return {"metrics_written": len(rows), "activities": len(activities)}