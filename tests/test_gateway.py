import base64
import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

import gateway
from garmin_http import GarminHttp, GarminHttpError, GarminTokenStore
from gateway import (
    GarminGateway,
    choose_token_source,
    load_op_creds,
    migrate_mcp_token,
)
from normalize import parse_rpe_feel, parse_weather


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
    monkeypatch.setattr("gateway.choose_token_source", lambda *args, **kwargs: ("path", str(v2)))
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
    monkeypatch.setattr("gateway.choose_token_source", lambda *args, **kwargs: ("path", str(v2)))
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


def test_gateway_includes_original_error_when_tokenstore_fails(tmp_path):
    """When the tokenstore exists but cannot be loaded (e.g. PermissionError),
    the RuntimeError must include the original error so the user sees the real
    cause instead of the generic 'no tokenstore, no op creds, no env creds'."""
    v2 = tmp_path / "v2_tokenstore.json"
    v2.write_text("{}")  # exists but empty

    class _FailingTS:
        def __init__(self, p, timeout=None):
            pass

        def load(self):
            raise PermissionError("[Errno 1] Operation not permitted")

    with (
        mock.patch("garminconnect.Garmin", side_effect=Exception("should not reach garminconnect")),
        mock.patch("gateway.load_op_creds", return_value=(None, None)),
        mock.patch("gateway.choose_token_source", return_value=("path", str(v2))),
        mock.patch("gateway.GarminTokenStore", _FailingTS),
        pytest.raises(RuntimeError, match="PermissionError"),
    ):
        GarminGateway(cache_dir=tmp_path / "cache", tokenstore_v2=v2)


# --- Task 1: activity summary/weather fetch + RPE/Feel/weather parses ---


def _gateway_with_http(monkeypatch, tmp_path, fh):
    """GarminGateway wired to a fake owned-HTTP layer (no network)."""
    v2 = tmp_path / "v2_tokenstore.json"
    v2.write_text(json.dumps({"di_token": "t", "di_refresh_token": "r", "di_client_id": "c"}))
    ts = GarminTokenStore(v2, timeout=5.0)
    ts.load()
    monkeypatch.setattr("gateway.GarminTokenStore", lambda p, timeout=None: ts)
    monkeypatch.setattr("gateway.GarminHttp", lambda _ts: fh)
    monkeypatch.setattr("gateway.choose_token_source", lambda *a, **k: ("path", str(v2)))
    with mock.patch("gateway.load_op_creds", return_value=(None, None)):
        return GarminGateway(cache_dir=tmp_path / "cache", tokenstore_v2=v2)


def test_gateway_fetch_activity_summary_caches_payload(monkeypatch, tmp_path):
    fh = mock.Mock(spec=GarminHttp)
    payload = {"summaryDTO": {"directWorkoutRpe": 50, "directWorkoutFeel": 25}}
    fh.fetch_activity_summary.return_value = payload
    gw = _gateway_with_http(monkeypatch, tmp_path, fh)
    out = gw.fetch_activity_summary(7)
    assert out == payload
    fh.fetch_activity_summary.assert_called_once_with(7)
    cached = json.loads((tmp_path / "cache" / "activity_summary_7.json").read_text())
    assert cached == payload


def test_gateway_fetch_activity_weather_caches_payload(monkeypatch, tmp_path):
    fh = mock.Mock(spec=GarminHttp)
    payload = {"temp": 66, "apparentTemp": 66, "relativeHumidity": 88}
    fh.fetch_activity_weather.return_value = payload
    gw = _gateway_with_http(monkeypatch, tmp_path, fh)
    out = gw.fetch_activity_weather(9)
    assert out == payload
    fh.fetch_activity_weather.assert_called_once_with(9)
    cached = json.loads((tmp_path / "cache" / "activity_weather_9.json").read_text())
    assert cached == payload


def test_gateway_fetch_weather_empty_payload_cached_as_empty_dict(monkeypatch, tmp_path):
    """Indoor runs yield {} (404 or empty) — cached so refresh skips refetch."""
    fh = mock.Mock(spec=GarminHttp)
    fh.fetch_activity_weather.return_value = {}
    gw = _gateway_with_http(monkeypatch, tmp_path, fh)
    assert gw.fetch_activity_weather(11) == {}
    cached = json.loads((tmp_path / "cache" / "activity_weather_11.json").read_text())
    assert cached == {}


def test_gateway_fetch_wrappers_default_none_to_empty(monkeypatch, tmp_path):
    fh = mock.Mock(spec=GarminHttp)
    fh.fetch_activity_summary.return_value = None
    fh.fetch_activity_weather.return_value = None
    gw = _gateway_with_http(monkeypatch, tmp_path, fh)
    assert gw.fetch_activity_summary(3) == {}
    assert gw.fetch_activity_weather(3) == {}
    assert json.loads((tmp_path / "cache" / "activity_summary_3.json").read_text()) == {}
    assert json.loads((tmp_path / "cache" / "activity_weather_3.json").read_text()) == {}


def test_gateway_fetch_activity_splits_caches_payload(monkeypatch, tmp_path):
    fh = mock.Mock(spec=GarminHttp)
    payload = {"activityId": 12, "lapDTOs": [{"lapIndex": 1, "distance": 1000.0}]}
    fh.fetch_activity_splits.return_value = payload
    gw = _gateway_with_http(monkeypatch, tmp_path, fh)
    out = gw.fetch_activity_splits(12)
    assert out == payload
    fh.fetch_activity_splits.assert_called_once_with(12)
    cached = json.loads((tmp_path / "cache" / "activity_splits_12.json").read_text())
    assert cached == payload


def test_gateway_fetch_splits_none_payload_cached_as_empty_dict(monkeypatch, tmp_path):
    """A None/empty http result must still be cached as {} (no refetch loop)."""
    fh = mock.Mock(spec=GarminHttp)
    fh.fetch_activity_splits.return_value = None
    gw = _gateway_with_http(monkeypatch, tmp_path, fh)
    assert gw.fetch_activity_splits(3) == {}
    assert json.loads((tmp_path / "cache" / "activity_splits_3.json").read_text()) == {}


def test_garmin_http_summary_and_weather_use_activity_service_paths():
    http = GarminHttp(mock.Mock())
    http.get_json = mock.Mock(return_value={"ok": True})
    assert http.fetch_activity_summary(42) == {"ok": True}
    http.get_json.assert_called_once_with("/activity-service/activity/42")
    http.get_json.reset_mock()
    assert http.fetch_activity_weather(42) == {"ok": True}
    http.get_json.assert_called_once_with("/activity-service/activity/42/weather")


def test_garmin_http_weather_404_returns_empty_other_errors_raise(monkeypatch):
    """End-to-end through get_json: a real 404 response must become {} (and
    the raise site must attach status_code so the wrapper can key on it) —
    any other status still raises with its code preserved."""

    def _respond(status):
        def fake_get(url, headers=None, params=None, timeout=5.0):
            r = mock.Mock(status_code=status)
            r.text = "not found" if status == 404 else "boom"
            return r

        return fake_get

    ts = mock.Mock()
    ts.timeout = 5.0
    ts.expires_soon.return_value = False
    ts.access_token.return_value = "tok"
    http = GarminHttp(ts, max_retries=0)

    monkeypatch.setattr("garmin_http.requests.get", _respond(404))
    assert http.fetch_activity_weather(1) == {}

    monkeypatch.setattr("garmin_http.requests.get", _respond(500))
    with pytest.raises(GarminHttpError) as exc:
        http.fetch_activity_weather(1)
    assert exc.value.status_code == 500


def test_garmin_http_fetch_coerces_non_dict_payload_to_empty():
    http = GarminHttp(mock.Mock())
    http.get_json = mock.Mock(return_value=[{"activityId": 1}])
    assert http.fetch_activity_summary(1) == {}
    assert http.fetch_activity_weather(1) == {}


def test_parse_rpe_feel_maps_rpe_and_feel():
    parsed = parse_rpe_feel({"summaryDTO": {"directWorkoutRpe": 50, "directWorkoutFeel": 25}})
    assert parsed == {"rpe_10": 5.0, "feel": "Weak"}


def test_parse_rpe_feel_five_labels_and_zero_values_present():
    labels = {0: "Very Weak", 25: "Weak", 50: "Normal", 75: "Strong", 100: "Very Strong"}
    for raw, label in labels.items():
        assert parse_rpe_feel({"summaryDTO": {"directWorkoutFeel": raw}})["feel"] == label
    # RPE 0 is a present value, not a missing one.
    assert parse_rpe_feel({"summaryDTO": {"directWorkoutRpe": 0}})["rpe_10"] == 0.0


def test_parse_rpe_feel_out_of_range_feel_gets_question_suffix():
    assert parse_rpe_feel({"summaryDTO": {"directWorkoutFeel": 60}})["feel"] == "60?"
    assert parse_rpe_feel({"summaryDTO": {"directWorkoutFeel": -5}})["feel"] == "-5?"


def test_parse_rpe_feel_none_safe_on_empty_or_malformed_payload():
    for payload in (None, {}, {"summaryDTO": None}, {"summaryDTO": []}, {"summaryDTO": {}}):
        assert parse_rpe_feel(payload) == {"rpe_10": None, "feel": None}
    malformed = {"summaryDTO": {"directWorkoutRpe": "bad", "directWorkoutFeel": [1, 2]}}
    assert parse_rpe_feel(malformed) == {"rpe_10": None, "feel": "[1, 2]?"}


def test_parse_weather_maps_verified_payload():
    parsed = parse_weather(
        {
            "temp": 66,
            "apparentTemp": 66,
            "dewPoint": 63,
            "relativeHumidity": 88,
            "windDirection": 200,
            "windDirectionCompassPoint": "ssw",
            "windSpeed": 3,
            "windGust": None,
            "weatherStationDTO": {"name": "some-station"},
            "weatherTypeDTO": {"desc": "Fair"},
        }
    )
    assert parsed == {
        "wx_temp": 66,
        "wx_feels": 66,
        "wx_humidity": 88,
        "wx_wind": 3,
        "wx_wind_dir": "ssw",
        "wx_desc": "Fair",
        "wx_unit": "F",
    }


def test_parse_weather_none_safe_on_empty_or_malformed_payload():
    all_none = {
        "wx_temp": None,
        "wx_feels": None,
        "wx_humidity": None,
        "wx_wind": None,
        "wx_wind_dir": None,
        "wx_desc": None,
        "wx_unit": "F",
    }
    for payload in (
        None,
        {},
        [],
        {"weatherTypeDTO": None},
        {"weatherTypeDTO": "oops"},
        {"weatherTypeDTO": {"desc": 5}},
        {"temp": "warm", "relativeHumidity": "humid", "apparentTemp": None},
    ):
        assert parse_weather(payload) == all_none


def test_parse_weather_wind_dir_falls_back_to_degrees():
    assert parse_weather({"windDirection": 200})["wx_wind_dir"] == "200"


def test_parse_helpers_emit_canonical_task2_keys():
    """Guard for the ledger's Ruling 1: downstream (Task 2) consumes exactly
    these keys — no dew_point, no wx_temp_f, no renames."""
    assert set(parse_rpe_feel({})) == {"rpe_10", "feel"}
    assert set(parse_weather({})) == {
        "wx_temp",
        "wx_feels",
        "wx_humidity",
        "wx_wind",
        "wx_wind_dir",
        "wx_desc",
        "wx_unit",
    }
