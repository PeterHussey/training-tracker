# tests/test_field_registry.py
import json
from pathlib import Path
from garmin_fields import FIELD_REGISTRY, METRICS, fields_for_metric, GarminField

FIXTURE = Path(__file__).parent / "fixtures" / "activities_sample.json"

ACTIVITY_LIST_FIELDS = {f.name for f in FIELD_REGISTRY if f.endpoint == "activity_list"}

# ctl/atl/tsb are chronic/acute derived metrics computed by accumulating the
# daily series (volume/trimp); they consume no raw Garmin field directly, so
# they are intentionally absent from every field's metrics tuple.
DERIVED_METRICS = {"ctl", "atl", "tsb"}

# Fields that carry no limitation text by design (a limitation string would be
# filler). Kept in sync with the registry so the units/limitations test stays
# a genuine guard against accidentally-truncated documentation.
LIMITLESS_FIELDS = {
    "activityId", "activityUUID", "beginTimestamp", "minElevation",
    "maxElevation", "avgElevation", "startLongitude", "endLatitude",
    "endLongitude", "manufacturer", "metrics[].distance",
    "speed_and_heart_rate.calendarDate", "Run_10k.time",
}


def test_every_metric_is_mapped():
    mapped = {m for f in FIELD_REGISTRY for m in f.metrics}
    assert mapped == METRICS - DERIVED_METRICS
    assert DERIVED_METRICS <= METRICS


def _resolve(obj, dotted):
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return True


def test_every_activity_list_field_is_present_or_documented_absent():
    data = json.loads(FIXTURE.read_text())
    running = [a for a in data if a.get("activityType", {}).get("typeKey") == "running"]
    missing = []
    for f in FIELD_REGISTRY:
        if f.endpoint != "activity_list":
            continue
        if not any(_resolve(a, f.name) for a in running):
            missing.append(f.name)
    assert missing == [], f"fields absent from every running activity: {missing}"


def test_fields_have_units_and_limitations():
    for f in FIELD_REGISTRY:
        assert f.units, f"{f.name} missing units"
        if f.name in LIMITLESS_FIELDS:
            continue
        assert f.limitations, f"{f.name} missing limitations"


def test_field_registry_is_frozen_and_importable():
    assert isinstance(FIELD_REGISTRY, tuple)
    assert isinstance(METRICS, frozenset)
    assert fields_for_metric("volume")
    assert all(f.metrics for f in FIELD_REGISTRY)
