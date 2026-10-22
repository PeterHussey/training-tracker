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


def test_dashboard_reports_an_unopenable_store_instead_of_a_traceback(tmp_path):
    """A store path that cannot be created used to escape as a bare traceback
    — the user saw a stack trace and nothing telling them what to do."""
    blocker = tmp_path / "data"
    blocker.write_text("not a directory")
    old_db = os.environ.get("TRAINING_DB")
    os.environ["TRAINING_DB"] = str(blocker / "training.sqlite")
    try:
        at = AppTest.from_file("../dashboard.py", default_timeout=30)
        at.run()
    finally:
        st.cache_resource.clear()
        if old_db is not None:
            os.environ["TRAINING_DB"] = old_db
        else:
            os.environ.pop("TRAINING_DB", None)

    assert not at.exception
    errors = [e.value for e in at.error]
    assert any("Cannot open the training store" in e for e in errors)
