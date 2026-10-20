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
