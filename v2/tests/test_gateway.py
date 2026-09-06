import base64
import json
import subprocess
from pathlib import Path
from unittest import mock

import gateway
from garmin_http import GarminHttp, GarminTokenStore
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
    r.stdout = json.dumps(
        [
            {"label": "username", "value": "runner@example.com"},
            {"label": "password", "value": "s3cret"},
        ]
    )
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
    p.write_text(
        json.dumps(
            {
                "access_token": _jwt({"client_id": "di-client-abc", "scope": "garmin"}),
                "refresh_token": "refresh-123",
                "token_type": "Bearer",
            }
        )
    )
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
    mcp_native.write_text(
        json.dumps({"di_token": "t", "di_refresh_token": "r", "di_client_id": "c"})
    )
    mcp_legacy.write_text(
        json.dumps({"access_token": _jwt({"client_id": "c"}), "refresh_token": "r"})
    )
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


def test_gateway_prefers_tokenstore_path(monkeypatch, tmp_path):
    v2 = tmp_path / "v2_tokenstore.json"
    v2.write_text(json.dumps({"di_token": "t", "di_refresh_token": "r", "di_client_id": "c"}))
    ts = GarminTokenStore(v2, timeout=5.0)
    ts.load()
    fh = mock.Mock(spec=GarminHttp)
    monkeypatch.setattr("gateway.GarminTokenStore", lambda p, timeout=None: ts)
    monkeypatch.setattr("gateway.GarminHttp", lambda ts: fh)
    with (
        mock.patch("gateway.load_op_creds", return_value=(None, None)),
        mock.patch("gateway.choose_token_source", return_value=("path", str(v2))),
    ):
        gw = GarminGateway(cache_dir=tmp_path / "cache", tokenstore_v2=v2)
        assert gw.auth_path == "tokenstore"
        assert gw._http is fh


def test_gateway_falls_back_to_op_credentials(monkeypatch, tmp_path):
    v2 = tmp_path / "v2_tokenstore.json"
    v2.write_text(json.dumps({"di_token": "t", "di_refresh_token": "r", "di_client_id": "c"}))
    fake = _FakeGarmin(login_effects=[None])
    with (
        mock.patch("garminconnect.Garmin", return_value=fake),
        mock.patch("gateway.load_op_creds", return_value=("u", "p")),
        mock.patch("gateway.choose_token_source", return_value=("migrated", "{}")),
        mock.patch("gateway.GarminTokenStore") as mock_ts,
    ):
        mock_ts.side_effect = RuntimeError("tokenstore failed")
        gw = GarminGateway(cache_dir=tmp_path / "cache", tokenstore_v2=v2)
        assert gw.auth_path == "op_credentials"


def test_gateway_op_login_without_tokenstore(monkeypatch, tmp_path):
    v2 = tmp_path / "v2_tokenstore.json"
    fake = _FakeGarmin(login_effects=[None])
    with (
        mock.patch("garminconnect.Garmin", return_value=fake),
        mock.patch("gateway.load_op_creds", return_value=("u", "p")),
        mock.patch("gateway.choose_token_source", return_value=(None, None)),
    ):
        gw = GarminGateway(cache_dir=tmp_path / "cache", tokenstore_v2=v2)
        fake.login.assert_called_once_with()
        assert gw.auth_path == "op_credentials"


def test_gateway_raises_without_any_credentials(tmp_path):
    fake = _FakeGarmin()
    with (
        mock.patch("garminconnect.Garmin", return_value=fake),
        mock.patch("gateway.load_op_creds", return_value=(None, None)),
        mock.patch("gateway.choose_token_source", return_value=(None, None)),
    ):
        try:
            GarminGateway(cache_dir=tmp_path / "cache", tokenstore_v2=tmp_path / "v2.json")
        except RuntimeError as e:
            assert "Garmin auth unavailable" in str(e)
        else:
            raise AssertionError("expected RuntimeError")


def _fake_http(monkeypatch, fetch_results=None):
    fh = mock.Mock(spec=GarminHttp)
    if fetch_results is None:
        fetch_results = {
            "activities": [],
            "lactate": {"speed_and_heart_rate": {}, "power": {}},
            "race": {"maybeMap": {}},
            "race_trend": [{"calendarDate": "2026-08-29", "time5K": 1405}],
            "vo2max": [],
        }
    fh.fetch_activities.return_value = fetch_results["activities"]
    fh.fetch_lactate_threshold.return_value = fetch_results["lactate"]
    fh.fetch_race_predictions.return_value = fetch_results["race"]
    fh.fetch_race_predictions_trend.return_value = fetch_results["race_trend"]
    fh.fetch_vo2max_trend.return_value = fetch_results["vo2max"]
    return fh


def test_gateway_uses_owned_http_path(monkeypatch, tmp_path):
    v2 = tmp_path / "v2_tokenstore.json"
    v2.write_text(json.dumps({"di_token": "t", "di_refresh_token": "r", "di_client_id": "c"}))
    ts = GarminTokenStore(v2, timeout=5.0)
    ts.load()
    fh = _fake_http(monkeypatch)
    monkeypatch.setattr("gateway.GarminTokenStore", lambda p, timeout=None: ts)
    monkeypatch.setattr("gateway.GarminHttp", lambda ts: fh)
    gw = GarminGateway(cache_dir=tmp_path / "cache", tokenstore_v2=v2)
    assert gw.auth_path == "tokenstore"
    assert gw._http is fh


def test_gateway_fetch_delegates(monkeypatch, tmp_path):
    v2 = tmp_path / "v2_tokenstore.json"
    v2.write_text(json.dumps({"di_token": "t", "di_refresh_token": "r", "di_client_id": "c"}))
    ts = GarminTokenStore(v2, timeout=5.0)
    ts.load()
    fh = _fake_http(monkeypatch)
    monkeypatch.setattr("gateway.GarminTokenStore", lambda p, timeout=None: ts)
    monkeypatch.setattr("gateway.GarminHttp", lambda ts: fh)
    gw = GarminGateway(cache_dir=tmp_path / "cache", tokenstore_v2=v2)
    gw.fetch_activities("a", "b")
    gw.fetch_lactate_threshold()
    gw.fetch_race_predictions()
    gw.fetch_race_predictions_trend("a", "b")
    gw.fetch_vo2max_trend("a", "b")
    fh.fetch_activities.assert_called_once_with("a", "b")
    fh.fetch_lactate_threshold.assert_called_once()
    fh.fetch_race_predictions.assert_called_once()
    fh.fetch_race_predictions_trend.assert_called_once_with("a", "b")
    fh.fetch_vo2max_trend.assert_called_once_with("a", "b")


def test_gateway_still_falls_back_to_op_login_without_tokenstore(monkeypatch, tmp_path):
    fake = _FakeGarmin(login_effects=[None])
    with (
        mock.patch("garminconnect.Garmin", return_value=fake),
        mock.patch("gateway.load_op_creds", return_value=("u", "p")),
        mock.patch("gateway.choose_token_source", return_value=(None, None)),
    ):
        gw = GarminGateway(cache_dir=tmp_path / "cache", tokenstore_v2=tmp_path / "v2.json")
        assert gw.auth_path == "op_credentials"


def test_fallback_materialises_tokenstore_and_builds_owned_http(monkeypatch, tmp_path):
    """Last-resort op_credentials path must produce a working owned GarminHttp.

    Regression guard for the final review finding: previously `_login_garminconnect`
    set only `self._garmin` and left `self._http = None`, so every `fetch_*`
    raised RuntimeError. After login the gateway must materialise the v2 tokenstore
    from `garminconnect`'s `client.dumps()` (v2 schema) and build a `GarminHttp`.
    """
    v2_path = tmp_path / "v2_tokenstore.json"
    tokenstore_json = json.dumps(
        {
            "di_token": "di-t",
            "di_refresh_token": "di-r",
            "di_client_id": "di-c",
        }
    )
    fake = mock.Mock()
    fake.login = mock.Mock(return_value=None)

    class _FakeClient:
        def dumps(self):
            return tokenstore_json

    fake.client = _FakeClient()

    http = mock.Mock(spec=GarminHttp)
    monkeypatch.setattr("gateway.GarminHttp", lambda ts: http)
    monkeypatch.setattr("gateway.GarminTokenStore", lambda p, timeout=None: _RaisingTS(p))
    with (
        mock.patch("garminconnect.Garmin", return_value=fake),
        mock.patch("gateway.load_op_creds", return_value=("u", "p")),
        mock.patch("gateway.choose_token_source", return_value=(None, None)),
    ):
        gw = GarminGateway(cache_dir=tmp_path / "cache", tokenstore_v2=v2_path)
        assert gw.auth_path == "op_credentials"
        assert gw._http is http, "fallback must build the owned HTTP layer"
        assert json.loads(v2_path.read_text())["di_client_id"] == "di-c"
        gw.fetch_activities("a", "b")
        http.fetch_activities.assert_called_once_with("a", "b")


class _RaisingTS:
    """GarminTokenStore double whose load() always succeeds (no network)."""

    def __init__(self, p, timeout=None):
        self.path = p

    def load(self):
        pass


def test_no_top_level_garminconnect_import():
    """Requirement: the routine tokenstore path must not pay garminconnect's
    import-time cost. `from garminconnect import Garmin` must be lazy (inside
    `_login_garminconnect`), not a top-level statement."""
    import ast

    src = Path(gateway.__file__).read_text()
    tree = ast.parse(src)
    top_level_imports = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for n in node.names:
                top_level_imports.append(n.name)
        elif isinstance(node, ast.ImportFrom):
            top_level_imports.append(node.module)
    assert not any(
        (n or "") == "garminconnect" or (n or "").startswith("garminconnect")
        for n in top_level_imports
    ), "gateway must not import garminconnect at module top level"
    lazy = any(
        isinstance(n, ast.ImportFrom) and n.module == "garminconnect"
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef,))
        for n in fn.body
        if isinstance(n, ast.ImportFrom)
    )
    assert lazy or "garminconnect" not in src, "expected garminconnect import to be lazy"
