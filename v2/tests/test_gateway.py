import base64
import json
import subprocess
from pathlib import Path
from unittest import mock

from gateway import (
    GarminGateway,
    choose_token_source,
    load_op_creds,
    migrate_mcp_token,
)


def _jwt(payload: dict) -> str:
    def b64(x) -> str:
        return base64.urlsafe_b64encode(json.dumps(x).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'none'})}.{b64(payload)}.fakesig"


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


def test_parse_json_format_output():
    r = mock.Mock()
    r.returncode = 0
    r.stdout = json.dumps([
        {"label": "username", "value": "runner@example.com"},
        {"label": "password", "value": "s3cret"},
    ])
    with mock.patch("gateway.subprocess.run", return_value=r):
        assert load_op_creds() == ("runner@example.com", "s3cret")


def test_parse_newline_output():
    r = mock.Mock()
    r.returncode = 0
    r.stdout = "runner@example.com\ns3cret\n"
    with mock.patch("gateway.subprocess.run", return_value=r):
        assert load_op_creds() == ("runner@example.com", "s3cret")


def test_migrate_mcp_token_builds_native_store(tmp_path):
    p = tmp_path / "oauth2_token.json"
    p.write_text(json.dumps({
        "access_token": _jwt({"client_id": "di-client-abc", "scope": "garmin"}),
        "refresh_token": "refresh-123",
        "token_type": "Bearer",
    }))
    stored = json.loads(migrate_mcp_token(p))
    assert stored["di_refresh_token"] == "refresh-123"
    assert stored["di_client_id"] == "di-client-abc"
    assert "di_token" in stored


def test_migrate_mcp_token_none_when_not_migratable(tmp_path):
    missing_refresh = tmp_path / "no_refresh.json"
    missing_refresh.write_text(json.dumps({"access_token": _jwt({"client_id": "c"})}))
    assert migrate_mcp_token(missing_refresh) is None
    native = tmp_path / "native.json"
    native.write_text(json.dumps({"di_token": "t", "di_refresh_token": "r", "di_client_id": "c"}))
    assert migrate_mcp_token(native) is None
    absent = tmp_path / "absent.json"
    assert migrate_mcp_token(absent) is None


def test_migrate_mcp_token_rejects_non_jwt_access_token(tmp_path):
    p = tmp_path / "oauth2_token.json"
    p.write_text(json.dumps({"access_token": "not-a-jwt", "refresh_token": "r"}))
    assert migrate_mcp_token(p) is None


def test_choose_token_source_priority(tmp_path):
    v2 = tmp_path / "v2_tokenstore.json"
    mcp_native = tmp_path / "mcp_native.json"
    mcp_legacy = tmp_path / "mcp_legacy.json"
    legacy_other = tmp_path / "legacy_no_jwt.json"
    mcp_native.write_text(json.dumps({"di_token": "t", "di_refresh_token": "r", "di_client_id": "c"}))
    mcp_legacy.write_text(json.dumps({"access_token": _jwt({"client_id": "c"}), "refresh_token": "r"}))
    legacy_other.write_text(json.dumps({"access_token": "no-jwt", "refresh_token": "r"}))
    v2.write_text(json.dumps({"di_token": "t2", "di_refresh_token": "r2", "di_client_id": "c2"}))
    assert choose_token_source(v2, mcp_native) == ("path", str(v2))
    v2.unlink()
    assert choose_token_source(v2, mcp_native) == ("path", str(mcp_native))
    kind, value = choose_token_source(v2, mcp_legacy)
    assert kind == "migrated"
    assert json.loads(value)["di_client_id"] == "c"
    assert choose_token_source(v2, legacy_other) == (None, None)


class _FakeGarmin:
    def __init__(self, login_effects=None):
        self.login = mock.Mock(side_effect=login_effects)
        self.calls = []

    def __call__(self, *a, **k):
        return self

    @property
    def client(self):
        return self

    def dumps(self):
        return json.dumps({"di_token": "t"})


def test_gateway_prefers_tokenstore_path(tmp_path):
    v2 = tmp_path / "v2_tokenstore.json"
    v2.write_text(json.dumps({"di_token": "t", "di_refresh_token": "r", "di_client_id": "c"}))
    fake = _FakeGarmin()
    with mock.patch("gateway.Garmin", return_value=fake) as gcls, \
         mock.patch("gateway.load_op_creds", return_value=(None, None)), \
         mock.patch("gateway.choose_token_source", return_value=("path", str(v2))):
        gw = GarminGateway(cache_dir=tmp_path / "cache", tokenstore_v2=v2)
        gcls.assert_called_once()
        fake.login.assert_called_once_with(tokenstore=str(v2))
        assert gw.auth_path == "tokenstore"


def test_gateway_falls_back_to_op_credentials(tmp_path):
    v2 = tmp_path / "v2_tokenstore.json"
    fake = _FakeGarmin(login_effects=[RuntimeError("token expired"), None])
    with mock.patch("gateway.Garmin", return_value=fake), \
         mock.patch("gateway.load_op_creds", return_value=("u", "p")), \
         mock.patch("gateway.choose_token_source", return_value=("migrated", "{}")):
        gw = GarminGateway(cache_dir=tmp_path / "cache", tokenstore_v2=v2)
        assert fake.login.call_count == 2
        assert fake.login.call_args_list[1].args == ()
        assert gw.auth_path == "op_credentials"


def test_gateway_op_login_without_tokenstore(tmp_path):
    v2 = tmp_path / "v2_tokenstore.json"
    fake = _FakeGarmin(login_effects=[None])
    with mock.patch("gateway.Garmin", return_value=fake), \
         mock.patch("gateway.load_op_creds", return_value=("u", "p")), \
         mock.patch("gateway.choose_token_source", return_value=(None, None)):
        gw = GarminGateway(cache_dir=tmp_path / "cache", tokenstore_v2=v2)
        fake.login.assert_called_once_with()
        assert gw.auth_path == "op_credentials"


def test_gateway_raises_without_any_credentials(tmp_path):
    fake = _FakeGarmin()
    with mock.patch("gateway.Garmin", return_value=fake), \
         mock.patch("gateway.load_op_creds", return_value=(None, None)), \
         mock.patch("gateway.choose_token_source", return_value=(None, None)):
        try:
            GarminGateway(cache_dir=tmp_path / "cache", tokenstore_v2=tmp_path / "v2.json")
        except RuntimeError as e:
            assert "Garmin auth unavailable" in str(e)
        else:
            raise AssertionError("expected RuntimeError")
