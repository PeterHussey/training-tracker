from backfill_sports import plan_updates


def _summary(aid, type_key):
    return {"activityId": aid, "activityType": {"typeKey": type_key}}


def test_plan_updates_flags_reclassified_rows_only():
    stored = {1: "cross", 2: "cross", 3: "running", 4: "cross"}
    raw = [
        _summary(1, "strength_training"),  # cross -> strength
        _summary(2, "indoor_cycling"),  # still cross: no change
        _summary(3, "running"),  # unchanged
        _summary(4, ""),  # missing typeKey: skipped
        _summary(999, "strength_training"),  # unknown id: skipped
    ]
    assert plan_updates(stored, raw) == [(1, "cross", "strength")]


def test_plan_updates_empty_fetch_no_plans():
    assert plan_updates({1: "cross"}, []) == []
