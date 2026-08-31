# Garmin Field → v2 Metric Mapping

> Generated source of truth: `v2/garmin_fields.py`. Endpoints: activity list,
> activity details, lactate threshold, race predictions, training status,
> user settings / heart-rate zones.

**Confirmation status (frozen fixtures vs live):** live Garmin capture is not
possible in this environment (no credentials/2FA), so no row below was confirmed
against a live session. The activity-list rows are confirmed against the frozen
`v2/tests/fixtures/activities_sample.json` — a 20-activity subset of the real
cached Garmin activity-list payload (8 running, 4 treadmill_running,
4 indoor_cycling, 4 strength_training). The lactate-threshold and race-prediction
rows are confirmed against their frozen fixtures, which were written from the
documented canonical Garmin schemas (race `time` in ms). Training status has no
fixture: its rows reflect the garminconnect documented schema and are
unconfirmed against any payload.

## 1. Endpoints used

**Activity list** (`activitylist-service/activities/search/activities`) lists
all activities in a calendar window. It carries the per-activity summary fields
(volume, elevation, HR averages, TE, splits) that drive most v2 series before
any per-sample work is done.

**Activity details** (`activity-service/{id}/details`) returns the per-sample
series (heart rate, speed, distance, altitude, workout step) plus lap summaries.
The gateway wrapper is rate-limit aware and one-request-per-activity. As of the
current v2 measurement layer **no metric consumes these series** — TRIMP uses the
list `averageHR`, CS uses `fastestSplit_1609`, and decoupling is not yet wired
into the pipeline — so the details payload is reserved for deferred
sample-level metrics (see §3 status note).

**Performance endpoints** cover lactate threshold (`lt`), race predictions
(`race_predictions`), and training status/load (`training_status`). These are
`garminconnect` convenience endpoints and are far less frequent than per-run
requests.

**Profile endpoints** would provide user settings and HR-zone config
(`user_settings`, `heart_rate_zones`). These are not implemented as gateway
fetchers in v2 — the `RunnerProfile` dataclass (with `default_profile` /
`from_age`) supplies HRmax/HRrest/zones directly and no payload is parsed (see
§5 status note).

## 2. Activity list fields

| Garmin field | Units | Maps to metric(s) | Limitations |
| --- | --- | --- | --- |
| activityId | id | volume |  |
| activityUUID | uuid | volume |  |
| activityType.typeKey | enum | volume, cross_training | Nested under 'activityType'. Classification in normalize.py is typeKey-only (running / treadmill_running / else cross); absent or unknown typeKey is classified as cross. There is **no** sportTypeId fallback. |
| sportTypeId | id | — | Present in the payload (1/2/4 in the frozen sample) but **not read by normalize.py**; the registry's "backup for classification when typeKey is missing" claim is not implemented in v2. Classification relies solely on activityType.typeKey. Kept for reference only. |
| startTimeLocal | ISO datetime | volume | Drives local calendar day/week bucketing. Timezone id recorded separately in timeZoneId. |
| beginTimestamp | epoch ms | volume |  |
| distance | m | volume, elevation | 0.0 for indoor_cycling; absent for strength. Convert to km for human-facing series. |
| duration | s | volume, trimp_banister, trimp_edwards, acwr | Use duration not elapsedDuration for TRIMP denominators; elapsedDuration adds pause time. |
| elapsedDuration | s | volume | Can materially exceed duration on stop-and-go sessions; used for decoupling eligibility (>=90 min sustained). |
| movingDuration | s | volume | Not always present; fall back to duration. |
| averageHR | bpm | trimp_banister, cross_training | Optional-sensor dependent (chest strap preferred). Missing on manually-uploaded strength data -> activity excluded from HR-based load. |
| maxHR | bpm | trimp_banister, cross_training, hrmax | Reported even when elevation is missing; feeds `estimate_hrmax` (recurring max on >=2 distinct days) with one-off spikes discounted as sensor artifacts. |
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
| hasIntensityIntervals | bool | trimp_banister, decoupling | Not consumed in v2 (not normalized into the Activity model). The registry intent — exclude intervals from decoupling, use details HR series for Banister — is **not implemented**: decoupling eligibility does not check this flag and Banister TRIMP uses the list `averageHR`. |
| lapCount | count | volume | Informational / structure. |
| splitSummaries | list | cs_approx, decoupling | No per-split HR here — HR comes from activity_details. |

## 3. Activity details fields

> Status: the `fetch_activity_details` gateway wrapper exists, but no v2 metric
> currently consumes the per-sample series or `lapsSummary` — TRIMP uses the
> activity-list `averageHR`, CS uses `fastestSplit_1609`, and `metrics/decoupling.py`
> is not wired into the pipeline. The rows below describe the intended source for
> the deferred sample-level metrics, not a payload consumed today.

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

Confirmed payload shape (frozen `lactate_threshold.json`, written from the
documented canonical schema; no live capture — see header): top-level keys
`speed_and_heart_rate` and `power` (empty object in the fixture).
`speed_and_heart_rate` carries the three mapped keys above (`heartRate` bpm,
`speed` m/s, `calendarDate`) plus unmapped `sequence`, `userProfilePK`,
`version`, `heartRateCycling`. `parse_lt` reads only
`heartRate`/`speed`/`calendarDate`.

### Race predictions (`race_predictions`)

| Garmin field | Units | Maps to metric(s) | Limitations |
| --- | --- | --- | --- |
| Run_5k.time | ms | race_5k | Riegel-derived; trusted more than marathon. Unit is milliseconds in Garmin payload (reconciled in Task 12). |
| Run_10k.time | ms | race_10k |  |
| Run_half_marathon.time | ms | race_half | Riegel calibration still OK at half distance. |
| Run_full_marathon.time | ms | race_full | LEAST TRUSTWORTHY: Riegel underestimates marathon by >=10 min for ~half of runners (Vickers 2016). Trend only. |

Confirmed payload shape (frozen `race_predictions.json`, written from the
documented canonical schema; no live capture — see header): top-level
`asOfDate`/`asOfDateTime` plus one object per distance key (`Run_5k`,
`Run_10k`, `Run_half_marathon`, `Run_full_marathon`), each carrying `time`
(ms) and `pace` (s/km; informative, not mapped). No unit deviation from the
canonical ms schema was needed — `parse_predictions` converts `time` ms→s and
`asOfDate` becomes the series date.

### Training status (`training_status`)

| Garmin field | Units | Maps to metric(s) | Limitations |
| --- | --- | --- | --- |
| load | arbitrary | load_reference | Proprietary, not independently validated. Reference display only. |
| trainingStatus | enum | load_reference | Proprietary, context only. |

No payload was captured for this endpoint (no fixture, no live session in this
environment), so the two rows above reflect the garminconnect documented schema
only and are **unconfirmed** against a real or frozen payload. Neither field is
consumed by the v2 computation path.

## 5. Profile fields

> Status: `user_settings` / `heart_rate_zones` are NOT fetched in v2 — there is
> no gateway fetcher and no payload fixture. `RunnerProfile` (and its
> `default_profile` / `from_age` constructors) is configured directly, so the
> rows below describe the intended mapping of configured vs age-predicted
> profile values (and their `hrmax_source` flag), not a parsed payload.

### User settings (`user_settings`)

| Garmin field | Units | Maps to metric(s) | Limitations |
| --- | --- | --- | --- |
| userData.maxHRSetting | bpm | trimp_banister, vo2max | If unset, age-predicted fallback (220-age) used and flagged in metric flags; estimation from recurring observed max (`hrmax_source=observed`) applies when no explicit setting, with a manual `configured` value always winning. |
| userData.birthDate | date | trimp_banister | Used for fallback HRmax and Banister sex exponent. |
| userData.measurementSystem | enum | volume | Documented; values stored in SI regardless. |

### Heart-rate zones (`heart_rate_zones`)

| Garmin field | Units | Maps to metric(s) | Limitations |
| --- | --- | --- | --- |
| heartRateZones | list | trimp_edwards | ZONE_LIMIT: zone-second buckets are only reproducible if the same zone thresholds are stored. Persist in RunnerProfile.hr_zones. |

## 6. Cross-cutting limitations

1. No cycling power in the activity list — cross-training load is HR/time/calories only; `load.edwards_cross` can be 0 for sessions without zone seconds.
2. Elevation absent on indoor/treadmill/zero-distance-cycling activities.
3. VO2max is a Firstbeat estimate (5–10% error; HRmax-sensitive; underestimates ≥60 mL/kg/min); never recomputed.
4. Anchor on LT heart rate (≈7% error); LT pace can overestimate 20–26%.
5. Race predictors: near-distance (5K/10K/half) safest; marathon least trustworthy.
6. HR zone buckets are device-config-dependent — always store `hr_zones` with the series.
7. Optical vs chest-strap sensor is not reliably distinguishable from the list payload (deviceId is a proxy only).
8. Garmin Training Status / load / TE are proprietary — reference only, never gates.
9. Decoupling is only honest when aggregated over ≥6 sessions on similar flat routes; single-run values are noise (preprint: 57–83% session-residual variance).
10. Banister exponent is population-generic, not individualized (iTRIMP needs lab lactate testing to personalize).
11. Garmin TRIMP is a black box (proprietary beat-to-beat calculation) — we recompute our own TRIMP from the HR series for reproducible units.
