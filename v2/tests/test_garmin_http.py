import base64
import json
import time
from pathlib import Path
from unittest import mock

import pytest

from garmin_http import (
    ACTIVITIES_PATH, CONNECTAPI_BASE,
    GarminHttp, GarminHttpError, GarminTokenStore, GarminAuthError,
    DI_TOKEN_URL,
)


def _jwt(claims: dict) -> str:
    def b64(x) -> str:
        return base64.urlsafe_b64encode(json.dumps(x).encode()).rstrip(b"=").decode()
    return f"{b64({'alg':'none'})}.{b64(claims)}.sig"


def _write_store(path: Path, token_claims: dict | None = None,
                 extra: dict | None = None) -> Path:
    claims = token_claims if token_claims is not None else {"exp": int(time.time()) + 7200}
    data = {"di_token": _jwt(claims), "di_refresh_token": "rt", "di_client_id": "cid"}
    if extra:
        data.update(extra)
    path.write_text(json.dumps(data))
    return path


def test_load_parses_tokenstore(tmp_path):
    p = _write_store(tmp_path / "v2.json")
    ts = GarminTokenStore(p, timeout=5.0)
    ts.load()
    assert ts.access_token() == _jwt({"exp": int(time.time()) + 7200})


def test_load_missing_file_raises(tmp_path):
    ts = GarminTokenStore(tmp_path / "nope.json", timeout=5.0)
    with pytest.raises(GarminAuthError):
        ts.load()


def test_load_invalid_json_raises(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    ts = GarminTokenStore(p, timeout=5.0)
    with pytest.raises(GarminAuthError):
        ts.load()


def test_expires_soon_false_when_far_from_expiry(tmp_path):
    p = _write_store(tmp_path / "v2.json", token_claims={"exp": int(time.time()) + 7200})
    ts = GarminTokenStore(p, timeout=5.0)
    ts.load()
    assert ts.expires_soon() is False
    assert ts.has_valid_token() is True


def test_expires_soon_true_within_threshold(tmp_path):
    p = _write_store(tmp_path / "v2.json", token_claims={"exp": int(time.time()) + 300})
    ts = GarminTokenStore(p, timeout=5.0)
    ts.load()
    assert ts.expires_soon() is True
    assert ts.has_valid_token() is False


def test_refresh_posts_grant_and_persists(tmp_path):
    p = _write_store(tmp_path / "v2.json")
    resp = mock.Mock()
    resp.status_code = 200
    resp.json.return_value = {"access_token": "newtok", "refresh_token": "newrt"}
    with mock.patch("garmin_http.requests.post", return_value=resp) as post:
        ts = GarminTokenStore(p, timeout=5.0)
        ts.load()
        assert ts.refresh() is True
    _, kwargs = post.call_args
    assert kwargs["timeout"] == 5.0
    assert kwargs["data"]["grant_type"] == "refresh_token"
    saved = json.loads(p.read_text())
    assert saved["di_token"] == "newtok"
    assert saved["di_refresh_token"] == "newrt"


def test_refresh_non_200_returns_false(tmp_path):
    p = _write_store(tmp_path / "v2.json")
    resp = mock.Mock()
    resp.status_code = 401
    with mock.patch("garmin_http.requests.post", return_value=resp):
        ts = GarminTokenStore(p, timeout=5.0)
        ts.load()
        assert ts.refresh() is False


# --- Task 2: GarminHttp tests ---


@pytest.fixture
def tokenstore(tmp_path) -> GarminTokenStore:
    p = _write_store(tmp_path / "v2.json")
    ts = GarminTokenStore(p, timeout=5.0)
    ts.load()
    return ts


def _row(activity_id):
    return {"activityId": activity_id, "startTimeLocal": "2026-08-01 00:00:00",
            "distance": 1000.0, "duration": 600.0}


def test_get_json_calls_connectapi_with_bearer(tokenstore, monkeypatch):
    calls = {}
    def fake_get(url, headers=None, params=None, timeout=5.0):
        calls["url"] = url
        calls["hdr"] = headers
        calls["timeout"] = timeout
        r = mock.Mock(status_code=200)
        r.json.return_value = [{"activityId": 1}]
        return r
    monkeypatch.setattr("garmin_http.requests.get", fake_get)
    gh = GarminHttp(tokenstore)
    out = gh.get_json(ACTIVITIES_PATH, params={"limit": 1})
    assert out == [{"activityId": 1}]
    assert calls["url"] == CONNECTAPI_BASE + ACTIVITIES_PATH
    assert calls["hdr"]["Authorization"].startswith("Bearer ")
    assert calls["timeout"] == 5.0


def test_get_json_raises_on_error_status(tokenstore, monkeypatch):
    def fake_get(url, headers=None, params=None, timeout=5.0):
        r = mock.Mock(status_code=500)
        r.text = "boom"
        return r
    monkeypatch.setattr("garmin_http.requests.get", fake_get)
    gh = GarminHttp(tokenstore)
    with pytest.raises(GarminHttpError):
        gh.get_json("/x")


def test_fetch_activities_uses_limit_100_and_offset(tokenstore, monkeypatch):
    seen = []
    def fake_get(url, headers=None, params=None, timeout=5.0):
        seen.append((params["limit"], params.get("offset", "0")))
        r = mock.Mock(status_code=200)
        r.json.return_value = []
        return r
    monkeypatch.setattr("garmin_http.requests.get", fake_get)
    GarminHttp(tokenstore).fetch_activities("a", "b")
    assert seen == [("100", "0")]


def test_fetch_activities_paginates(tokenstore, monkeypatch):
    def fake_get(url, headers=None, params=None, timeout=5.0):
        offset = int(params["offset"])
        page = [_row(i) for i in range(offset, offset + 100)] if offset < 200 else [_row(200)]
        r = mock.Mock(status_code=200)
        r.json.return_value = page
        return r
    monkeypatch.setattr("garmin_http.requests.get", fake_get)
    gh = GarminHttp(tokenstore)
    acts = gh.fetch_activities("2026-01-01", "2026-08-31")
    assert len(acts) == 201
    assert acts[0]["activityId"] == 0
    assert acts[-1]["activityId"] == 200


def test_get_json_aborts_stall_within_call_budget(tokenstore, monkeypatch):
    """Root-cause regression: a half-stalled SSL socket defeats requests'
    `timeout=`, so a stuck connectapi call must be abandoned by force (daemon
    thread + hard deadline) and surfaced as GarminHttpError — not hang forever."""
    def stall_forever(url, headers=None, params=None, timeout=5.0):
        time.sleep(8)  # simulates a half-open socket that never delivers the body
    monkeypatch.setattr("garmin_http.requests.get", stall_forever)
    gh = GarminHttp(tokenstore)
    t0 = time.time()
    with pytest.raises(GarminHttpError):
        gh.get_json(ACTIVITIES_PATH, params={"limit": "5", "offset": "0"})
    elapsed = time.time() - t0
    assert elapsed < 6, f"call did not abort promptly: {elapsed:.1f}s"
