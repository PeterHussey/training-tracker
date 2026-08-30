# Garmin Field → v2 Metric Mapping

> Generated source of truth: `v2/garmin_fields.py`. Endpoints: activity list,
> activity details, lactate threshold, race predictions, training status,
> user settings / heart-rate zones.

## 1. Endpoints used

**Activity list** (`activitylist-service/activities/search/activities`) lists
all activities in a calendar window. It carries the per-activity summary fields
(volume, elevation, HR averages, TE, splits) that drive most v2 series before
any per-sample work is done.

**Activity details** (`activity-service/{id}/details`) returns the per-sample
series (heart rate, speed, distance, altitude, workout step) plus lap summaries.
This is one request **per activity**, is rate-limit aware, and is fetched
**lazily** — only when a metric (TRIMP from HR, decoupling, critical speed)
actually needs the samples. Everything else stays at the cheap list level.

**Performance endpoints** cover lactate threshold (`lt`), race predictions
(`race_predictions`), and training status/load (`training_status`). These are
`garminconnect` convenience endpoints and are far less frequent than per-run
requests.

**Profile endpoints** provide user settings and HR-zone config
(`user_settings`, `heart_rate_zones`). These are read rarely (once per profile
load) and cached; the HR-zone thresholds must be persisted alongside any
seconds-in-zone series.

## 2. Activity list fields

| Garmin field | Units | Maps to metric(s) | Limitations |
| --- | --- | --- | --- |
| activityId | id | volume |  |
| activityUUID | uuid | volume |  |
| activityType.typeKey | enum | volume, cross_training | Nested under 'activityType'; absent typeKey means classification falls back to sportTypeId. |
| sportTypeId | id | volume, cross_training | Backup for classification when typeKey is missing; mapping table maintained in normalize.py. |
| startTimeLocal | ISO datetime | volume | Drives local calendar day/week bucketing. Timezone id recorded separately in timeZoneId. |
| beginTimestamp | epoch ms | volume |  |
| distance | m | volume, elevation | 0.0 for indoor_cycling; absent for strength. Convert to km for human-facing series. |
| duration | s | volume, trimp_banister, trimp_edwards, acwr | Use duration not elapsedDuration for TRIMP denominators; elapsedDuration adds pause time. |
| elapsedDuration | s | volume | Can materially exceed duration on stop-and-go sessions; used for decoupling eligibility (>=90 min sustained). |
| movingDuration | s | volume | Not always present; fall back to duration. |
| averageHR | bpm | trimp_banister, cross_training | Optional-sensor dependent (chest strap preferred). Missing on manually-uploaded strength data -> activity excluded from HR-based load. |
| maxHR | bpm | trimp_banister | Reported even when elevation is missing; excluded from inference when spikes look sensor-artifactual (not enforced in v2). |
| hrTimeInZone_1 | s | trimp_edwards | Seconds-in-zone buckets depend on the device HR-zone config. Reproducibility requires persisting RunnerProfile.hr_zones alongside the series. |
| hrTimeInZone_2 | s | trimp_edwards | Seconds-in-zone buckets depend on the device HR-zone config. Reproducibility requires persisting RunnerProfile.hr_zones alongside the series. |
| hrTimeInZone_3 | s | trimp_edwards | Seconds-in-zone buckets depend on the device HR-zone config. Reproducibility requires persisting RunnerProfile.hr_zones alongside the series. |
| hrTimeInZone_4 | s | trimp_edwards | Seconds-in-zone buckets depend on the device HR-zone config. Reproducibility requires persisting RunnerProfile.hr_zones alongside the series. |
| hrTimeInZone_5 | s | trimp_edwards | Seconds-in-zone buckets depend on the device HR-zone config. Reproducibility requires persisting RunnerProfile.hr_zones alongside the series. |
| averageSpeed | m/s | cs_approx | Not present on all activities; convert to min/km for humans. |
| fastestSplit_1609 | s | cs_approx | Approximation only: not a controlled critical-speed test; best used as a lower-bound trend signal. |
| maxSpeed | m/s | cs_approx | Sprint artifacts; not used directly for CS. |
| elevationGain | m | elevation, decoupling | Absent on treadmill/intensity/indoor activities. Used as a gradient filter for decoupling (25 m/km). |
| elevationLoss | m | elevation | Same indoor gap as elevationGain. |
| minElevation | m | elevation |  |
| maxElevation | m | elevation |  |
| avgElevation | m | elevation |  |
| vO2MaxValue | mL/kg/min | vo2max | Only present on outdoor running (absent: treadmill, cross-training). Estimate (5-10% error), HRmax-dependent, underestimates highly-trained runners. Do not recompute. |
| aerobicTrainingEffect | 1-5 | load_reference | Proprietary; reference only, never a gate. |
| anaerobicTrainingEffect | 1-5 | load_reference | Proprietary; reference only. |
| averageRunningCadenceInStepsPerMinute | spm | cs_approx | Running only; cadence is not used as a headline metric in v2. |
| avgStrideLength | cm | cs_approx | Running only; informational. |
| startLatitude | deg | decoupling | Used for route clustering; horizontal geolocation (not fixed) required. |
| startLongitude | deg | decoupling |  |
| endLatitude | deg | decoupling |  |
| endLongitude | deg | decoupling |  |
| locationName | text | decoupling | Unreliable (frequently empty); route_key prefers start coordinates. |
| deviceId | id | decoupling | Proxy for sensor source. Cannot reliably distinguish chest strap vs optical from the list payload; flag recorded, honesty preferred. |
| manufacturer | text | decoupling |  |
| calories | kcal | cross_training | Estimate only; not a training-stress metric in v2. |
| hasIntensityIntervals | bool | trimp_banister, decoupling | Interval sessions are excluded from decoupling (not sustained effort); Banister TRIMP uses details HR series when available to handle them. |
| lapCount | count | volume | Informational / structure. |
| splitSummaries | list | cs_approx, decoupling | No per-split HR here — HR comes from activity_details. |

## 3. Activity details fields

| Garmin field | Units | Maps to metric(s) | Limitations |
| --- | --- | --- | --- |
| metrics[].heartRate | bpm[] | trimp_banister, decoupling | One request per activity (rate-limit aware). Downsampled by maxChartSize; gaps possible when optical. |
| metrics[].speed | m/s[] | decoupling, cs_approx | Speed vs distance series can misalign on GPS dropouts; resample to common timestamps. |
| metrics[].distance | m[] | cs_approx |  |
| metrics[].altitude | m[] | elevation | Baro vs GPS-derived altitude varies; not used for headline elevation (activity list elevationGain is authoritative). |
| metrics[].wkt | enum[] | decoupling | Use to mask non-effort segments when splitting halves. |
| lapsSummary | list | decoupling, trimp_banister | Fallback to lap-level HR if per-second series is empty. |

## 4. Performance endpoints

### Lactate threshold (`lt`)

| Garmin field | Units | Maps to metric(s) | Limitations |
| --- | --- | --- | --- |
| speed_and_heart_rate.heartRate | bpm | lt_hr | Anchored metric: LABEL trustworthy (7% error), pace is not (up to 20-26% overest.). Manual lab/race set via RunnerProfile.lthr_manual overrides. |
| speed_and_heart_rate.speed | m/s | lt_pace, cs_approx | FLAGGED: 2025 studies show Garmin LT pace can overestimate by 20-26%; use for trend, not absolute prescription. |
| speed_and_heart_rate.calendarDate | date | lt_hr, lt_pace |  |

### Race predictions (`race_predictions`)

| Garmin field | Units | Maps to metric(s) | Limitations |
| --- | --- | --- | --- |
| Run_5k.time | ms | race_5k | Riegel-derived; trusted more than marathon. Unit is milliseconds in Garmin payload (reconciled in Task 12). |
| Run_10k.time | ms | race_10k |  |
| Run_half_marathon.time | ms | race_half | Riegel calibration still OK at half distance. |
| Run_full_marathon.time | ms | race_full | LEAST TRUSTWORTHY: Riegel underestimates marathon by >=10 min for ~half of runners (Vickers 2016). Trend only. |

### Training status (`training_status`)

| Garmin field | Units | Maps to metric(s) | Limitations |
| --- | --- | --- | --- |
| load | arbitrary | load_reference | Proprietary, not independently validated. Reference display only. |
| trainingStatus | enum | load_reference | Proprietary, context only. |

## 5. Profile fields

### User settings (`user_settings`)

| Garmin field | Units | Maps to metric(s) | Limitations |
| --- | --- | --- | --- |
| userData.maxHRSetting | bpm | trimp_banister, vo2max | If unset, age-predicted fallback (220-age) used and flagged in metric flags. |
| userData.birthDate | date | trimp_banister | Used for fallback HRmax and Banister sex exponent. |
| userData.measurementSystem | enum | volume | Documented; values stored in SI regardless. |

### Heart-rate zones (`heart_rate_zones`)

| Garmin field | Units | Maps to metric(s) | Limitations |
| --- | --- | --- | --- |
| heartRateZones | list | trimp_edwards | ZONE_LIMIT: zone-second buckets are only reproducible if the same zone thresholds are stored. Persist in RunnerProfile.hr_zones. |

## 6. Cross-cutting limitations

1. No cycling power in the activity list — cross-training load is HR/time/calories only.
2. Elevation absent on indoor/treadmill/zero-distance-cycling activities.
3. VO2max is a Firstbeat estimate (5–10% error; HRmax-sensitive; underestimates ≥60 mL/kg/min); never recomputed.
4. Anchor on LT heart rate (≈7% error); LT pace can overestimate 20–26%.
5. Race predictors: near-distance (5K/10K/half) safest; marathon least trustworthy.
6. HR zone buckets are device-config-dependent — always store `hr_zones` with the series.
7. Optical vs chest-strap sensor is not reliably distinguishable from the list payload (deviceId is a proxy only).
8. Garmin Training Status / load / TE are proprietary — reference only, never gates.
9. Decoupling is only honest when aggregated over ≥6 sessions on similar flat routes; single-run values are noise (preprint: 57–83% session-residual variance).
