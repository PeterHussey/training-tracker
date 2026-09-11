"""Period picker reset regression tests — coherent module."""

import json
import os
import tempfile
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

# reset_period_after_refresh imported inside tests to allow env setup

FIXTURES = Path(__file__).parent / "fixtures"

# ------------------------------------------------------------------
# 4 pure helper tests for reset_period_after_refresh
# ------------------------------------------------------------------


def test_reset_extends_until_preserving_since():
    from dashboard import reset_period_after_refresh

    assert reset_period_after_refresh(
        (date(2026, 1, 1), date(2026, 8, 27)), date(2026, 3, 15), date(2026, 9, 3)
    ) == (date(2026, 1, 1), date(2026, 9, 3))


def test_reset_uses_default_when_no_prior_value():
    from dashboard import reset_period_after_refresh

    assert reset_period_after_refresh(None, date(2026, 3, 15), date(2026, 9, 3)) == (
        date(2026, 3, 15),
        date(2026, 9, 3),
    )


def test_reset_single_date_prior_value():
    from dashboard import reset_period_after_refresh

    assert reset_period_after_refresh(date(2026, 8, 27), date(2026, 3, 15), date(2026, 9, 3)) == (
        date(2026, 8, 27),
        date(2026, 9, 3),
    )


def test_reset_normalizes_datetime_values():
    from dashboard import reset_period_after_refresh

    assert reset_period_after_refresh(
        (datetime(2026, 1, 1), datetime(2026, 8, 27)), date(2026, 3, 15), date(2026, 9, 3)
    ) == (date(2026, 1, 1), date(2026, 9, 3))


# ------------------------------------------------------------------
# 2 AppTest regression tests
# ------------------------------------------------------------------

FAKE_ACTIVITY_999999 = {
    "activityId": 999999,
    "activityName": "Fake Refresh Activity",
    "startTimeLocal": "2026-09-03 10:00:00",
    "startTimeGMT": "2026-09-03 14:00:00",
    "activityType": {"typeKey": "running"},
    "distance": 5000.0,
    "duration": 1800.0,
    "elapsedDuration": 1800.0,
    "movingDuration": 1800.0,
    "averageSpeed": 2.78,
    "averageHR": 150.0,
    "maxHR": 165.0,
    "elevationGain": 50.0,
    "vO2MaxValue": 46.0,
    "hasIntensityIntervals": False,
}


class _FakeGateway:
    """Gateway double: refresh returns one new activity; detail/trend fetches are empty."""

    def __init__(self, *args, **kwargs):
        self.cache_dir = Path("cache/test_cache")
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def fetch_activities(self, start, end):
        return [FAKE_ACTIVITY_999999]

    def fetch_activity_details(self, activity_id):
        return {}

    def fetch_lactate_threshold(self):
        return {}

    def fetch_race_predictions_trend(self, start, end):
        return []

    def fetch_vo2max_trend(self, start, end):
        return []


class _FakeGatewayNoop(_FakeGateway):
    """Gateway double: refresh returns no activities."""

    def fetch_activities(self, start, end):
        return []


def _seed_store_with_sample(db_path: Path) -> None:
    raw = json.loads((FIXTURES / "activities_sample.json").read_text())
    from normalize import from_summary
    from store import MetricStore

    store = MetricStore(str(db_path))
    store.save_activities([from_summary(a) for a in raw])
    store.close()


@pytest.fixture
def seeded_app():
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "training.sqlite"
        _seed_store_with_sample(db_path)
        old_db = os.environ.get("TRAINING_DB")
        os.environ["TRAINING_DB"] = str(db_path)
        yield db_path
        # AppTest runs dashboard.py in-process; its @st.cache_resource store
        # outlives the run and would pin the fresh temp DB above. Clear so the
        # next test starts clean and the deleted tempdir is never written to.
        st.cache_resource.clear()
        if old_db is not None:
            os.environ["TRAINING_DB"] = old_db
        else:
            os.environ.pop("TRAINING_DB", None)


def _find_period(at):
    for w in at.date_input:
        if w.key and w.key.startswith("period_"):
            return w
    return None


def test_period_widget_key_resets_after_refresh_that_adds_activities(seeded_app):
    with (
        patch("gateway.GarminGateway", _FakeGateway),
        patch("gateway.choose_token_source", return_value=("fake", "fake")),
    ):
        at = AppTest.from_file("../dashboard.py", default_timeout=10)
        at.run()

        di = _find_period(at)
        assert di is not None
        assert di.key == "period_0"
        assert di.value == (date(2026, 6, 17), date(2026, 8, 27))

        at.button[0].click().run()

        di_after = _find_period(at)
        assert di_after is not None
        assert di_after.key == "period_1"
        assert di_after.value == (date(2026, 6, 17), date(2026, 9, 3))

        from store import MetricStore

        store_after = MetricStore(str(seeded_app))
        assert 999999 in {a.activity_id for a in store_after.load_activities()}
        store_after.close()

        at.run()
        di_stick = _find_period(at)
        assert di_stick.key == "period_1"
        assert di_stick.value == (date(2026, 6, 17), date(2026, 9, 3))


def test_period_widget_survives_noop_refresh(seeded_app):
    with (
        patch("gateway.GarminGateway", _FakeGatewayNoop),
        patch("gateway.choose_token_source", return_value=("fake", "fake")),
    ):
        at = AppTest.from_file("../dashboard.py", default_timeout=10)
        at.run()

        di_before = _find_period(at)
        assert di_before is not None
        before_key, before_value = di_before.key, di_before.value

        at.button[0].click().run()

        di_after = _find_period(at)
        assert di_after is not None
        assert di_after.key == before_key
        assert di_after.value == before_value
