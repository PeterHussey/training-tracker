import base64
import json
import time
from pathlib import Path
from unittest import mock

import pytest
import requests

from garmin_http import (
    ACTIVITIES_PATH,
    CONNECTAPI_BASE,
    GarminAuthError,
    GarminHttp,
    GarminHttpError,
    GarminTokenStore,
)


def _jwt(claims: dict) -> str:
    def b64(x) -> str:
        return base64.urlsafe_b64encode(json.dumps(x).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'none'})}.{b64(claims)}.sig"


def _write_store(path: Path, token_claims: dict | None = None, extra: dict | None = None) -> Path:
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
    return {
        "activityId": activity_id,
        "startTimeLocal": "2026-08-01 00:00:00",
        "distance": 1000.0,
        "duration": 600.0,
    }


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
    gh = GarminHttp(tokenstore, max_retries=0)
    t0 = time.time()
    with pytest.raises(GarminHttpError):
        gh.get_json(ACTIVITIES_PATH, params={"limit": "5", "offset": "0"})
    elapsed = time.time() - t0
    assert elapsed < 6, f"call did not abort promptly: {elapsed:.1f}s"


def test_get_json_retries_transient_failure_then_succeeds(tokenstore, monkeypatch):
    """Phase-4 robustness: a transient (stalled-then-recovers) page must be
    retried within the bounded retry budget instead of failing the whole fetch."""
    calls = {"n": 0}

    def flaky(url, headers=None, params=None, timeout=5.0):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.Timeout("simulated stalled SSL read")
        r = mock.Mock(status_code=200)
        r.json.return_value = [{"activityId": 7}]
        return r

    monkeypatch.setattr("garmin_http.requests.get", flaky)
    # patch threading time.sleep used for backoff so the test is instant
    monkeypatch.setattr("garmin_http.time.sleep", lambda s: None)
    gh = GarminHttp(tokenstore, max_retries=2, retry_backoff=0.0)
    out = gh.get_json(ACTIVITIES_PATH, params={"limit": "5", "offset": "0"})
    assert out == [{"activityId": 7}]
    assert calls["n"] == 2


def test_fetch_activities_terminates_on_duplicate_pages(tokenstore, monkeypatch):
    """Root-cause regression for the recurring timeout.

    Garmin's connectapi can return the SAME page regardless of `offset`
    (offset ignored), so each call yields 100 rows that are already-seen
    activityIds. The healthy break (`len(page) < limit`) never fires because
    every page is full (100). Without dedup-based termination the loop spins
    forever (this is what blew the 90s guard). The fetch must detect
    all-duplicate pages and stop, returning exactly the distinct rows.
    """
    page = [_row(i) for i in range(100)]  # activityIds 0..99
    call_count = {"n": 0}

    def fake_get(url, headers=None, params=None, timeout=5.0):
        call_count["n"] += 1
        if call_count["n"] >= 3:
            raise RuntimeError("LOOP SENTINEL: pagination not terminating")
        r = mock.Mock(status_code=200)
        r.json.return_value = list(page)  # identical 100 rows every page
        return r

    monkeypatch.setattr("garmin_http.requests.get", fake_get)
    gh = GarminHttp(tokenstore, max_retries=0)
    acts = gh.fetch_activities("a", "b")
    assert call_count["n"] == 2, f"expected dedup to stop after 2 pages, got {call_count['n']}"
    assert len(acts) == 100
    assert acts[0]["activityId"] == 0
    assert acts[-1]["activityId"] == 99


def test_fetch_vo2max_trend_hits_daily_endpoint(tokenstore, monkeypatch):
    """The trend endpoint must be /maxmet/daily/{start}/{end} (NOT /latest),

    which always returns the current value regardless of the date in the URL
    (python-garminconnect#74). The daily endpoint returns real historical
    variance.
    """
    calls = {}

    def fake_get(url, headers=None, params=None, timeout=5.0):
        calls["url"] = url
        r = mock.Mock(status_code=200)
        r.json.return_value = [
            {"generic": {"calendarDate": "2026-08-09", "vo2MaxPreciseValue": 46.5}}
        ]
        return r

    monkeypatch.setattr("garmin_http.requests.get", fake_get)
    gh = GarminHttp(tokenstore)
    out = gh.fetch_vo2max_trend("2026-08-01", "2026-08-27")
    assert "maxmet/daily/2026-08-01/2026-08-27" in calls["url"]
    assert "maxmet/latest" not in calls["url"]
    assert out[0]["generic"]["vo2MaxPreciseValue"] == 46.5


def test_fetch_race_predictions_trend_hits_daily_endpoint(tokenstore, monkeypatch):
    """Race history must come from /racepredictions/daily/{name} (NOT /latest).

    /latest returns a single current snapshot, which is why the store held
    only one race point. The daily endpoint returns one snapshot per day in
    range (same flat time5K/calendarDate schema as latest).
    """
    calls = {}

    def fake_get(url, headers=None, params=None, timeout=5.0):
        calls["url"] = url
        calls["params"] = params
        r = mock.Mock(status_code=200)
        r.json.return_value = [
            {"calendarDate": "2026-08-28", "time5K": 1405},
            {"calendarDate": "2026-09-03", "time5K": 1413},
        ]
        return r

    monkeypatch.setattr("garmin_http.requests.get", fake_get)
    gh = GarminHttp(tokenstore)
    gh._display_name = "test-user"
    out = gh.fetch_race_predictions_trend("2026-08-28", "2026-09-03")
    assert "racepredictions/daily/test-user" in calls["url"]
    assert "racepredictions/latest" not in calls["url"]
    assert calls["params"] == {
        "fromCalendarDate": "2026-08-28",
        "toCalendarDate": "2026-09-03",
    }
    assert len(out) == 2
    assert out[0]["time5K"] == 1405
