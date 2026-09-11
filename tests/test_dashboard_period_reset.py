"""Period picker reset regression tests.

Root cause (streamlit #5442, reproduced in 1.62.0): the dashboard refreshed the
Activities Period ``date_input`` by ``st.session_state.pop(PERIOD_KEY, None)``
inside the Refresh button handler. A script-body pop updates the Python-side
value for only one run; the browser keeps displaying the pre-refresh range and
re-adopts it on the next rerun. Net effect reported: charts/KPIs look right for
one run ("other tabs seem to update to the right range"), then the Activities
tab's exact-date Period filter reverts and freshly fetched activities vanish
from its range.

Fix: after a refresh adds activities, give the Period picker a NEW widget key
(forcing the browser to adopt the reset range), preserving the user's start
date and extending the end out to the new latest activity date.
"""

from datetime import date
from pathlib import Path

from dashboard import reset_period_after_refresh

FIXTURES = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parent.parent


# --- pure helper -------------------------------------------------------------


def test_reset_extends_until_preserving_since():
    assert reset_period_after_refresh(
        (date(2026, 1, 1), date(2026, 8, 27)), date(2026, 3, 15), date(2026, 9, 3)
    ) == (date(2026, 1, 1), date(2026, 9, 3))


def test_reset_uses_default_when_no_prior_value():
    assert reset_period_after_refresh(None, date(2026, 3, 15), date(2026, 9, 3)) == (
        date(2026, 3, 15),
        date(2026, 9, 3),
    )


def test_reset_single_date_prior_value():
    assert reset_period_after_refresh(date(2026, 8, 27), date(2026, 3, 15), date(2026, 9, 3)) == (
        date(2026, 8, 27),
        date(2026, 9, 3),
    )


def test_reset_normalizes_datetime_values():
    from datetime import datetime

    assert reset_period_after_refresh(
        (datetime(2026, 1, 1), datetime(2026, 8, 27)), date(2026, 3, 15), date(2026, 9, 3)
    ) == (date(2026, 1, 1), date(2026, 9, 3))
