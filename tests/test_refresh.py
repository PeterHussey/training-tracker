"""refresh.py — the launchd job's entry point must report failures, not tracebacks.

The scheduled refresh is the only thing that keeps the store current. When
Garmin auth is unavailable it must fail with a readable line in the job's
stderr log, not a stack trace nobody reads.
"""

import refresh


class _NoAuthGateway:
    """Gateway double: construction fails the way it does without credentials."""

    def __init__(self, *args, **kwargs):
        raise RuntimeError("Garmin auth unavailable: no tokenstore, no op creds, no env creds")


def test_missing_credentials_report_one_line_not_a_traceback(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRAINING_DB", str(tmp_path / "t.sqlite"))
    monkeypatch.setattr(refresh, "GarminGateway", _NoAuthGateway)

    rc = refresh.main()

    err = capsys.readouterr().err
    assert rc == 1
    assert "Traceback" not in err
    assert "Garmin auth unavailable" in err


class _StubGateway:
    """Gateway double that returns empty payloads — no network, no credentials."""

    def __init__(self, *args, **kwargs):
        pass

    def fetch_activities(self, start, end):
        return []

    def fetch_lactate_threshold(self):
        return {}

    def fetch_race_predictions_trend(self, start, end):
        return []

    def fetch_vo2max_trend(self, start, end):
        return []


def test_refresh_snapshots_the_store_before_it_writes(tmp_path, monkeypatch, capsys):
    """The store is the only artefact that cannot be rebuilt from Garmin."""
    monkeypatch.setenv("TRAINING_DB", str(tmp_path / "t.sqlite"))
    monkeypatch.setattr(refresh, "GarminGateway", _StubGateway)

    rc = refresh.main()

    snapshots = sorted((tmp_path / "backups").glob("training-*.sqlite"))
    assert rc == 0
    assert len(snapshots) == 1
    assert "backup written to" in capsys.readouterr().out


def test_a_refresh_writes_a_start_line_before_it_fetches(tmp_path, monkeypatch, capsys):
    """The start line is what tells the health check a refresh is running from
    a wedged one; the completion line is what ends that state."""
    monkeypatch.setenv("TRAINING_DB", str(tmp_path / "t.sqlite"))
    monkeypatch.setattr(refresh, "GarminGateway", _StubGateway)

    assert refresh.main() == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("[refresh] start ")
    assert lines[-1].startswith("refresh: ")


def test_a_failed_snapshot_does_not_stop_the_refresh(tmp_path, monkeypatch, capsys):
    """A backup that cannot be written must not cost us the refresh."""
    monkeypatch.setenv("TRAINING_DB", str(tmp_path / "t.sqlite"))
    monkeypatch.setattr(refresh, "GarminGateway", _StubGateway)

    def _boom(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(refresh.MetricStore, "snapshot", _boom)

    rc = refresh.main()

    err = capsys.readouterr().err
    assert rc == 0
    assert "backup failed" in err
    assert "Traceback" not in err
