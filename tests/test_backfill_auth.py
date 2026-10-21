"""Every backfill CLI reports missing Garmin credentials instead of crashing.

The four backfill scripts construct `GarminGateway` unguarded, so without a
tokenstore, 1Password or env credentials they died with a full traceback —
the last reason criterion 8 ("missing Garmin credentials surface a clear
error, not a stack trace") could not be green.
"""

import pytest

import backfill_cadence
import backfill_event_types
import backfill_names
import backfill_sports
from store import MetricStore

MODULES = [
    (backfill_names, "backfill_names"),
    (backfill_sports, "backfill"),
    (backfill_event_types, "backfill_event_types"),
    (backfill_cadence, "backfill_cadence"),
]


class _NoAuthGateway:
    def __init__(self, *args, **kwargs):
        raise RuntimeError("Garmin auth unavailable: no tokenstore, no op creds, no env creds")


def _seeded_db(tmp_path):
    db = tmp_path / "t.sqlite"
    store = MetricStore(str(db))
    store.conn.execute(
        "INSERT INTO activities (activity_id, activity_date, sport) "
        "VALUES (1, '2024-01-01', 'running')"
    )
    store.conn.commit()
    store.close()
    return db


@pytest.mark.parametrize("module,prefix", MODULES)
def test_missing_credentials_report_one_line_not_a_traceback(
    module, prefix, tmp_path, monkeypatch, capsys
):
    db = _seeded_db(tmp_path)
    monkeypatch.setattr(module, "GarminGateway", _NoAuthGateway)

    rc = module.main(["--db", str(db)])

    err = capsys.readouterr().err
    assert rc == 1
    assert "Traceback" not in err
    assert prefix in err
    assert "Garmin auth unavailable" in err
