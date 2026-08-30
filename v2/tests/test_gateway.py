import subprocess
from unittest import mock

from gateway import load_op_creds


def test_missing_op_binary_returns_none():
    with mock.patch(
        "gateway.subprocess.run",
        side_effect=FileNotFoundError,
    ):
        assert load_op_creds() == (None, None)


def test_op_timeout_returns_none():
    with mock.patch(
        "gateway.subprocess.run",
        side_effect=subprocess.TimeoutExpired("op item get Garmin", 15),
    ):
        assert load_op_creds() == (None, None)


def test_parse_reveal_output():
    r = mock.Mock()
    r.returncode = 0
    r.stdout = "user,pass"
    with mock.patch("gateway.subprocess.run", return_value=r):
        assert load_op_creds() == ("user", "pass")


def test_nonzero_returncode_returns_none():
    r = mock.Mock()
    r.returncode = 1
    r.stdout = "user,pass"
    with mock.patch("gateway.subprocess.run", return_value=r):
        assert load_op_creds() == (None, None)
