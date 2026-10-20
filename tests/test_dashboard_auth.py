"""The dashboard must survive missing Garmin credentials.

The startup detail fetch builds a GarminGateway, which raises when there is
no tokenstore, no 1Password creds and no env creds. The refresh *button*
already survives that; the startup path did not, so an expired token plus a
store with activities killed the whole app instead of degrading to the stored
data.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

FIXTURES = Path(__file__).parent / "fixtures"


class _NoAuthGateway:
    """Gateway double: construction fails the way it does without credentials."""

    def __init__(self, *args, **kwargs):
        raise RuntimeError("Garmin auth unavailable: no tokenstore, no op creds, no env creds")


@pytest.fixture
def seeded_app():
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "training.sqlite"
        raw = json.loads((FIXTURES / "activities_sample.json").read_text())
        from normalize import from_summary
        from store import MetricStore

        store = MetricStore(str(db_path))
        store.save_activities([from_summary(a) for a in raw])
        store.close()
        old_db = os.environ.get("TRAINING_DB")
        os.environ["TRAINING_DB"] = str(db_path)
        yield db_path
        # AppTest runs dashboard.py in-process; its @st.cache_resource store
        # outlives the run and would pin the fresh temp DB above.
        st.cache_resource.clear()
        if old_db is not None:
            os.environ["TRAINING_DB"] = old_db
        else:
            os.environ.pop("TRAINING_DB", None)


def test_dashboard_survives_missing_garmin_credentials(seeded_app):
    with patch("gateway.GarminGateway", _NoAuthGateway):
        at = AppTest.from_file("../dashboard.py", default_timeout=30)
        at.run()

    assert not at.exception
    warnings = [w.value for w in at.sidebar.warning]
    assert any("Garmin auth unavailable" in w for w in warnings)
    # The stored data still renders — the app degrades, it does not stop.
    assert len(at.tabs) >= 1
