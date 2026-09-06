# Owned Garmin HTTP layer for v2 gateway — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the garminconnect-driven auth+fetch in `v2/gateway.py` with a fully-owned `requests` layer (using the persistent v2 DI tokenstore) so every Garmin call has a strict, reliable per-request timeout. This fixes the intermittent multi-minute hang where garminconnect's own request layer tunnels through Cloudflare-TLS/curl_cffi strategies that can stall indefinitely.

**Architecture:** A new module `v2/garmin_http.py` owns (1) DI tokenstore load + DI refresh via `https://diauth.garmin.com/di-oauth2-service/oauth/token`, and (2) a `GET` helper for `connectapi.garmin.com` with a mandatory short `timeout`. `gateway.GarminGateway` keeps its exact public API (constructor signature, `auth_path`, and all `fetch_*` methods) but delegates to this module. When no valid tokenstore exists, fall back to a bounded garminconnect login as a documented last resort (credentials via `op`/env) — the routine path never touches garminconnect.

**Tech Stack:** Python 3.11, `requests`, stdlib `base64/json/time/threading`, existing `v2/` modules (`normalize.py`, `store.py`, `session.py`). Streamlit dashboard unchanged.

**Spec:** Root-cause investigation documented in session (superpowers conversation, 2026-08-31). Live-verified: raw `requests` against `connectapi.garmin.com` with `Bearer <v2 di_token>` returns 200 in ~0.1s for activities/lactate/power; DI refresh returns 200 in ~0.16s with fresh tokens. garminconnect, by contrast, intermittently hangs >60–120s on the identical endpoint. v1 precedent (`v1/garmin_client.py::fetch_activities_live`) used the same plain-requests approach successfully.

## Global Constraints

- Python >= 3.11; only stdlib + `requests` for the new HTTP path.
- `GarminGateway` public API must remain **API-stable** (no signature changes); `dashboard.py` and `tests/test_gateway.py` must not need edits to keep passing.
- Every outbound HTTP request MUST carry a mandatory `timeout` (connect + read) no greater than 15s. Never reuse `garminconnect` for routine fetches.
- Keep the v2 tokenstore schema `{"di_token": str, "di_refresh_token": str, "di_client_id": str}` at `TOKENSTORE_V2`.

---

## Task 1: `GarminTokenStore` — load/refresh DI token

**Files:**
- Create: `v2/garmin_http.py`
- Test: `v2/tests/test_garmin_http.py`

**Interfaces:**
- Consumes: `TOKENSTORE_V2` path `~/.garmin-mcp/v2_tokenstore.json` (defined in `gateway.py:30`).
- Produces:
  - `class GarminTokenStore` with:
    - `__init__(self, path: Path = TOKENSTORE_V2, timeout: float = 15.0)`
    - `load(self) -> None` — reads the JSON tokenstore; raises `GarminAuthError` if missing/invalid.
    - `access_token() -> str | None` — current `di_token`.
    - `has_valid_token() -> bool` — token exists and (no expiry claim) or not within 900s of `exp`.
    - `expires_soon() -> bool`
    - `refresh(self) -> bool` — POST DI refresh grant; on success persists updated `di_token`/`di_refresh_token` and returns `True`; on any error returns `False` (never raises out).
    - `_load_expiry() -> float | None` — decode `exp` from the `di_token` JWT payload.
  - Exception `GarminAuthError(Exception)`.
  - `DI_TOKEN_URL = "https://diauth.garmin.com/di-oauth2-service/oauth/token"`
  - `connectapi_garmin_headers(token: str) -> dict` — exact `_native_headers` set from garminconnect (see Task 2).

- [ ] **Step 1: Write failing tests** — create `v2/tests/test_garmin_http.py`:

```python
import base64
import json
import time
from pathlib import Path
from unittest import mock

import pytest

from garmin_http import DI_TOKEN_URL, GarminAuthError, GarminTokenStore


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `../.venv/bin/python -m pytest tests/test_garmin_http.py -v` from `v2/`. Expected: collection errors (`ModuleNotFoundError: garmin_http`).

- [ ] **Step 3: Implement `GarminTokenStore`** — create `v2/garmin_http.py`:

```python
"""Owned Garmin REST HTTP layer using the v2 DI tokenstore.

garminconnect's own request layer tunnels through Cloudflare-TLS/curl_cffi
strategies that can stall for minutes. This module owns the routine path
(auth via the persisted DI token + connectapi GETs) with strict timeouts.
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Optional

import requests

DI_TOKEN_URL = "https://diauth.garmin.com/di-oauth2-service/oauth/token"
CONNECTAPI_BASE = "https://connectapi.garmin.com"
_NATIVE_UA = "GCM-Android-5.23"
_X_GARMIN_UA = "Android-FIT/5.23.0.0"


class GarminAuthError(Exception):
    """Raised when the persisted Garmin credentials cannot be loaded."""


def _native_headers(extra: dict[str, str]) -> dict[str, str]:
    headers = {
        "User-Agent": _NATIVE_UA,
        "X-Garmin-User-Agent": _X_GARMIN_UA,
        "X-Garmin-Paired-App-Version": "10861",
        "X-Garmin-Client-Platform": "Android",
        "X-App-Ver": "10861",
        "X-Lang": "en",
        "X-GCExperience": "GC5",
        "Accept-Language": "en-US,en;q=0.9",
    }
    headers.update(extra)
    return headers


def connectapi_garmin_headers(token: str) -> dict[str, str]:
    return _native_headers({"Authorization": f"Bearer {token}", "Accept": "application/json"})


class GarminTokenStore:
    def __init__(self, path: Path = Path.home() / ".garmin-mcp" / "v2_tokenstore.json",
                 timeout: float = 15.0):
        self.path = Path(path)
        self.timeout = timeout
        self._data: dict = {}

    def load(self) -> None:
        try:
            raw = self.path.read_text()
        except OSError as e:
            raise GarminAuthError(f"tokenstore missing: {self.path}") from e
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise GarminAuthError(f"tokenstore is not valid JSON: {self.path}") from e
        if not data.get("di_token"):
            raise GarminAuthError("tokenstore has no di_token")
        self._data = data

    def access_token(self) -> Optional[str]:
        return self._data.get("di_token")

    def _load_expiry(self) -> Optional[float]:
        tok = self.access_token() or ""
        parts = tok.split(".")
        if len(parts) < 2:
            return None
        try:
            payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode()))
            exp = payload.get("exp")
            return float(exp) if isinstance(exp, (int, float)) else None
        except (IndexError, ValueError, json.JSONDecodeError):
            return None

    def expires_soon(self) -> bool:
        exp = self._load_expiry()
        if exp is None:
            return False
        return float(time.time()) > (exp - 900.0)

    def has_valid_token(self) -> bool:
        return bool(self.access_token())

    def refresh(self) -> bool:
        cid = self._data.get("di_client_id")
        rt = self._data.get("di_refresh_token")
        if not cid or not rt:
            return False
        basic = "Basic " + base64.b64encode(f"{cid}:".encode()).decode()
        headers = {
            "Authorization": basic,
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "Cache-Control": "no-cache",
        }
        data = {"grant_type": "refresh_token", "client_id": cid, "refresh_token": rt}
        try:
            r = requests.post(DI_TOKEN_URL, headers=headers, data=data, timeout=self.timeout)
        except requests.RequestException:
            return False
        if r.status_code != 200:
            return False
        try:
            j = r.json()
        except ValueError:
            return False
        if not j.get("access_token"):
            return False
        self._data["di_token"] = j["access_token"]
        if j.get("refresh_token"):
            self._data["di_refresh_token"] = j["refresh_token"]
        try:
            self._data["di_client_id"] = (
                self._re_extract_client_id(j["access_token"]) or self._data.get("di_client_id")
            )
        except Exception:
            pass
        self._persist()
        return True

    def _re_extract_client_id(self, token: str) -> Optional[str]:
        # Reuse JWT payload decoding for the client_id claim (optional nicety).
        parts = token.split(".")
        if len(parts) < 2:
            return None
        try:
            b64 = parts[1] + "=" * (-len(parts[1]) % 4)
            claims = json.loads(base64.urlsafe_b64decode(b64))
            cid = claims.get("client_id")
            return cid if isinstance(cid, str) and cid else None
        except Exception:
            return None

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `../.venv/bin/python -m pytest tests/test_garmin_http.py -v` from `v2/`. Expected: all PASS.

- [ ] **Step 5: Run the whole suite to confirm nothing broke**

Run: `../.venv/bin/python -m pytest tests/ -q` from `v2/`. Expected: 116 passed.

- [ ] **Step 6: Commit**

```bash
git add v2/garmin_http.py v2/tests/test_garmin_http.py
git commit -m "feat(v2): owned Garmin tokenstore load/refresh over requests"
```

---

## Task 2: `GarminHttp` — owned connectapi GET fetchers

**Files:**
- Modify: `v2/garmin_http.py`
- Test: `v2/tests/test_garmin_http.py`

**Interfaces:**
- Consumes: `GarminTokenStore`, `connectapi_garmin_headers`, `CONNECTAPI_BASE` from Task 1.
- Produces:
  - `class GarminHttp`:
    - `__init__(self, tokenstore: GarminTokenStore)`
    - `get_json(self, path: str, params: dict | None = None) -> dict | list` — auto-refreshes token when `expires_soon()`, `requests.get` with `timeout=tokenstore.timeout`, raises `GarminHttpError` on non-200.
    - `fetch_activities(self, start: str, end: str) -> list[dict]` — paginated `ACTIVITIES_PATH` loop (limit 100) identical to the current `gateway.fetch_activities` semantics.
    - `fetch_lactate_threshold(self) -> dict` — via `/biometric-service/biometric/latestLactateThreshold` + `/biometric-service/biometric/powerToWeight/latest/{today}?sport=Running`; returns garminconnect-shaped `{"speed_and_heart_rate": {...}, "power": {...}}`.
    - `fetch_race_predictions(self) -> dict` — via `/metrics-service/metrics/racepredictions/latest/{display_name}`.
  - `class GarminHttpError(Exception)`, `ACTIVITIES_PATH = "/activitylist-service/activities/search/activities"`.
  - `display_name` resolved via `connectapi` `GET /userprofile-service/socialProfile` at first use, cached on the instance.

- [ ] **Step 1: Write failing tests** — append to `v2/tests/test_garmin_http.py` (add these imports at the top of the file alongside the existing Task 1 imports):

```python
from garmin_http import (
    ACTIVITIES_PATH, CONNECTAPI_BASE,
    GarminHttp, GarminHttpError, GarminTokenStore,
)
```

Then append these tests (reuse `_jwt`, `_write_store` from Task 1):

```python
@pytest.fixture
def tokenstore(tmp_path) -> GarminTokenStore:
    p = _write_store(tmp_path / "v2.json")
    ts = GarminTokenStore(p, timeout=5.0)
    ts.load()
    return ts


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


def test_fetch_activities_paginates(tokenstore, monkeypatch):
    rows = [[_row(1), _row(2)], [_row(3)], []]
    def fake_get(url, headers=None, params=None, timeout=5.0):
        offset = int(params["offset"])
        r = mock.Mock(status_code=200)
        r.json.return_value = rows[min(offset // 100, len(rows) - 1)] if offset < 200 else []
        return r
    monkeypatch.setattr("garmin_http.requests.get", fake_get)
    gh = GarminHttp(tokenstore)
    acts = gh.fetch_activities("2026-01-01", "2026-08-31")
    assert [a["activityId"] for a in acts] == [1, 2, 3]


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


def _row(activity_id):
    return {"activityId": activity_id, "startTimeLocal": "2026-08-01 00:00:00",
            "distance": 1000.0, "duration": 600.0}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `../.venv/bin/python -m pytest tests/test_garmin_http.py -v` from `v2/`. Expected: failures (`GarminHttp`/`CONNECTAPI_BASE`/`ACTIVITIES_PATH` not defined).

- [ ] **Step 3: Implement `GarminHttp`** — append to `v2/garmin_http.py`:

```python
ACTIVITIES_PATH = "/activitylist-service/activities/search/activities"


class GarminHttpError(Exception):
    """Raised when a Garmin connectapi call fails (network or non-200)."""


class GarminHttp:
    def __init__(self, tokenstore: GarminTokenStore):
        self.tokenstore = tokenstore
        self._display_name: str | None = None

    def _token(self) -> str:
        if self.tokenstore.expires_soon():
            self.tokenstore.refresh()
        tok = self.tokenstore.access_token()
        if not tok:
            raise GarminAuthError("no access token available")
        return tok

    def get_json(self, path: str, params: dict | None = None) -> dict | list:
        url = CONNECTAPI_BASE + path
        headers = connectapi_garmin_headers(self._token())
        try:
            r = requests.get(url, headers=headers, params=params,
                             timeout=self.tokenstore.timeout)
        except requests.RequestException as e:
            raise GarminHttpError(f"request failed for {path}: {e}") from e
        if r.status_code != 200:
            raise GarminHttpError(f"API {r.status_code} for {path}: {r.text[:200]}")
        try:
            return r.json()
        except ValueError as e:
            raise GarminHttpError(f"non-JSON response for {path}") from e

    def _resolve_display_name(self) -> str:
        if self._display_name:
            return self._display_name
        prof = self.get_json("/userprofile-service/socialProfile")
        name = (prof.get("displayName") if isinstance(prof, dict) else None) or ""
        if not name:
            raise GarminHttpError("could not resolve Garmin display name")
        self._display_name = name
        return name

    def fetch_activities(self, start: str, end: str) -> list[dict]:
        out: list[dict] = []
        offset = 0
        limit = 100
        while True:
            page = self.get_json(ACTIVITIES_PATH, params={
                "startDate": start, "endDate": end, "limit": limit, "offset": offset,
            }) or []
            out.extend(page)
            if len(page) < limit:
                break
            offset += limit
        return out

    def fetch_lactate_threshold(self) -> dict:
        import datetime as _dt
        power = self.get_json("/biometric-service/biometric/powerToWeight/latest/"
                              f"{_dt.date.today()}?sport=Running")
        if isinstance(power, list) and power:
            power_dict = power[0]
        elif isinstance(power, dict):
            power_dict = power
        else:
            power_dict = {}
        sweat = self.get_json("/biometric-service/biometric/latestLactateThreshold")
        card = {"userProfilePK": None, "version": None, "calendarDate": None,
                "sequence": None, "speed": None, "heartRate": None,
                "heartRateCycling": None}
        if isinstance(sweat, list):
            for entry in sweat:
                if entry.get("speed") is not None:
                    card["speed"] = entry["speed"]
                hr = entry.get("heartRate") or entry.get("hearRate")
                if hr is not None:
                    card["heartRate"] = hr
                hrc = entry.get("heartRateCycling")
                if hrc is not None:
                    card["heartRateCycling"] = hrc
        return {"speed_and_heart_rate": card, "power": power_dict}

    def fetch_race_predictions(self) -> dict:
        name = self._resolve_display_name()
        return self.get_json(f"/metrics-service/metrics/racepredictions/latest/{name}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `../.venv/bin/python -m pytest tests/test_garmin_http.py -v` from `v2/`. Expected: all PASS (both Task 1 and Task 2 tests).

- [ ] **Step 5: Run the whole suite**

Run: `../.venv/bin/python -m pytest tests/ -q`. Expected: 116 passed.

- [ ] **Step 6: Commit**

```bash
git add v2/garmin_http.py v2/tests/test_garmin_http.py
git commit -m "feat(v2): owned connectapi GET fetchers with strict timeouts"
```

---

## Task 3: Rewire `GarminGateway` onto the owned HTTP layer

**Files:**
- Modify: `v2/gateway.py` (auth resolution `__init__` at lines 115-152, `fetch_*` methods at 154-207)
- Test: `v2/tests/test_gateway.py` (existing — must keep passing unchanged) and new tests appended to `v2/tests/test_gateway.py`

**Interfaces:**
- Consumes: `GarminTokenStore`, `GarminHttp`, `GarminHttpError` (Task 1 & 2); existing `choose_token_source`, `migrate_mcp_token`, `load_op_creds` from `gateway.py` as-is.
- Produces (unchanged public API, by design):
  - `GarminGateway.__init__(self, cache_dir: Path = Path("cache/test_cache"), tokenstore_v2: Path = TOKENSTORE_V2)`
  - attribute `self.auth_path: str` — `"tokenstore"` (owned HTTP path) or `"op_credentials"` (garminconnect fallback).
  - `fetch_activities(self, start, end) -> list[dict]` — delegates to `GarminHttp.fetch_activities`.
  - `fetch_lactate_threshold(self) -> dict` — delegates to `GarminHttp.fetch_lactate_threshold`.
  - `fetch_race_predictions(self) -> dict` — delegates to `GarminHttp.fetch_race_predictions`.
  - `fetch_activity_details(self, activity_id) -> dict` — owned `GET /activity-service/activity/{id}/details`.
  - `fetch_training_status(self, cdate) -> dict` — owned `GET /metrics-service/metrics/trainingstatus/aggregated/{cdate}`.
  - `_persist_tokens(self, path)` — kept as local write (no garminconnect).

**Behavior:** When a valid tokenstore exists (`choose_token_source` returns `("path", ...)` or `("migrated", json_string)`), build `GarminTokenStore` + `GarminHttp` and set `auth_path="tokenstore"`. Only when NO tokenstore is available (or the owned HTTP path cannot authenticate) fall back to the existing garminconnect login with `load_op_creds`, bounded by the dashboard's `run_with_timeout`. The owned HTTP path is the default and never touches garminconnect.

- [ ] **Step 1: Write failing tests** — append to `v2/tests/test_garmin_http.py` (imports from `gateway`):

```python
from gateway import GarminGateway
from garmin_http import GarminTokenStore, GarminHttp


def _fake_http(monkeypatch, fetch_results=None):
    fh = mock.Mock(spec=GarminHttp)
    if fetch_results is None:
        fetch_results = {"activities": [], "lactate": {"speed_and_heart_rate": {}, "power": {}},
                         "race": {"maybeMap": {}}}
    fh.fetch_activities.return_value = fetch_results["activities"]
    fh.fetch_lactate_threshold.return_value = fetch_results["lactate"]
    fh.fetch_race_predictions.return_value = fetch_results["race"]
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
    ts = GarminTokenStore(v2, timeout=5.0); ts.load()
    fh = _fake_http(monkeypatch)
    monkeypatch.setattr("gateway.GarminTokenStore", lambda p, timeout=None: ts)
    monkeypatch.setattr("gateway.GarminHttp", lambda ts: fh)
    gw = GarminGateway(cache_dir=tmp_path / "cache", tokenstore_v2=v2)
    gw.fetch_activities("a", "b")
    gw.fetch_lactate_threshold()
    gw.fetch_race_predictions()
    fh.fetch_activities.assert_called_once_with("a", "b")
    fh.fetch_lactate_threshold.assert_called_once()
    fh.fetch_race_predictions.assert_called_once()


def test_gateway_still_falls_back_to_op_login_without_tokenstore(monkeypatch, tmp_path):
    fake = _FakeGarmin(login_effects=[None])
    with mock.patch("gateway.Garmin", return_value=fake), \
         mock.patch("gateway.load_op_creds", return_value=("u", "p")), \
         mock.patch("gateway.choose_token_source", return_value=(None, None)):
        gw = GarminGateway(cache_dir=tmp_path / "cache", tokenstore_v2=tmp_path / "v2.json")
        assert gw.auth_path == "op_credentials"
```

Note: `_FakeGarmin` is defined in `v2/tests/test_gateway.py`. Reuse it by importing it in the appended test block, OR append these gateway tests directly to `v2/tests/test_gateway.py` (preferred — avoids import coupling). Put the three tests above in `v2/tests/test_gateway.py` after the existing tests.

- [ ] **Step 2: Run tests to verify they fail**

Run: `../.venv/bin/python -m pytest tests/test_gateway.py -v` from `v2/`. Expected: new tests fail (gateway doesn't yet expose `_http`, or still calls garminconnect illegally). Existing tests may still pass.

- [ ] **Step 3: Rewire `GarminGateway`** — modify `v2/gateway.py`:

Replace the constructor body (lines 126-152) with:

```python
        from garmin_http import GarminHttp, GarminTokenStore
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._http: GarminHttp | None = None
        email, password = load_op_creds()
        if not email or not password:
            email = os.environ.get("GARMIN_EMAIL")
            password = os.environ.get("GARMIN_PASSWORD")
        kind, value = choose_token_source()
        if kind is not None and value is not None:
            try:
                if kind == "migrated":
                    store_path = self._materialise_migrated_store(value, tokenstore_v2)
                else:
                    store_path = Path(value)
                store = GarminTokenStore(store_path, timeout=15.0)
                store.load()
                self._http = GarminHttp(store)
                self.auth_path = "tokenstore"
                return
            except Exception:
                pass
        # Fallback: garminconnect credential login (last resort, bounded by caller).
        self._login_garminconnect(email, password)
```

Add helper methods to the class (before `_persist_tokens`):

```python
    def _materialise_migrated_store(self, store_json: str, tokenstore_v2: Path) -> Path:
        path = Path(tokenstore_v2)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(store_json)
        return path

    def _login_garminconnect(self, email, password) -> None:
        from garminconnect import Garmin
        if not email or not password:
            raise RuntimeError(
                "Garmin auth unavailable: no tokenstore, no op creds, no env creds"
            )
        self._garmin = Garmin(email=email, password=password, is_cn=False)
        self._garmin.login()
        self.auth_path = "op_credentials"
        self._garmin.client.dumps = lambda: json.dumps({})  # no-op guard
```

Replace the fetch methods (lines 170-202 and 204-207) with delegations:

```python
    def fetch_activities(self, start: str, end: str) -> list[dict]:
        raw = self._require_http().fetch_activities(start, end)
        self._cache("garmin_raw.json", raw)
        return raw

    def fetch_activity_details(self, activity_id: int) -> dict:
        payload = self._require_http().get_json(
            DETAILS_PATH.format(activity_id=activity_id),
            params={"maxChartSize": 2000, "maxPolylineSize": 4000},
        ) or {}
        self._cache(f"activity_details_{activity_id}.json", payload)
        return payload

    def fetch_lactate_threshold(self) -> dict:
        payload = self._require_http().fetch_lactate_threshold()
        self._cache("lactate_threshold.json", payload)
        return payload

    def fetch_race_predictions(self) -> dict:
        payload = self._require_http().fetch_race_predictions()
        self._cache("race_predictions.json", payload)
        return payload

    def fetch_training_status(self, cdate: str) -> dict:
        payload = self._require_http().get_json(
            f"/metrics-service/metrics/trainingstatus/aggregated/{cdate}"
        )
        self._cache(f"training_status_{cdate}.json", payload)
        return payload

    def _require_http(self) -> "GarminHttp":
        if self._http is None:
            raise RuntimeError("Garmin HTTP layer not initialised (no tokenstore)")
        return self._http
```

Also simplify `_persist_tokens` to a local no-op write (it will only run in the garminconnect fallback path):

```python
    def _persist_tokens(self, path: Path) -> None:
        try:
            path = Path(path).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self._garmin.client.dumps() if False else {}))
        except Exception:
            pass
```

Note: after rewire, `fetch_activity_details` returns the full details JSON; `fetch_training_status` returns the aggregated JSON directly. Both keep the same shape as the connectapi JSON garminconnect returned, so no downstream assumptions change.

Update the module docstring's first paragraph (line 1-4) to state the owned-HTTP primary path and garminconnect-only-as-last-resort.

- [ ] **Step 4: Run tests to verify they pass**

Run: `../.venv/bin/python -m pytest tests/ -q` from `v2/`. Expected: existing 116 + new gateway tests all PASS.

- [ ] **Step 5: Live smoke test the owned path**

Run (from `v2/`, real network, tokenstore present):

```bash
../.venv/bin/python - <<'PY'
from pathlib import Path
from gateway import GarminGateway
gw = GarminGateway(cache_dir=Path("cache/smoke"))
print("auth_path:", gw.auth_path)
raw = gw.fetch_activities("2026-08-01", "2026-08-31")
print("activities:", len(raw))
print("lactate:", gw.fetch_lactate_threshold())
print("race:", gw.fetch_race_predictions())
PY
```

Expected: `auth_path: tokenstore`, `activities: N` (fast, < a few seconds, reliable).

- [ ] **Step 6: Commit**

```bash
git add v2/gateway.py v2/tests/test_gateway.py
git commit -m "feat(v2): run gateway fetch/auth through owned Garmin HTTP layer"
```

---

## Task 4: Dashboard live refresh + final verification

**Files:**
- Modify: none expected (dashboard already calls `GarminGateway` + `run_with_timeout`).
- If needed: `v2/dashboard.py` (only to tune messaging).

**Interfaces:**
- Consumes: `GarminGateway` rewire from Task 3; existing dashboard `refresh_garmin()` (dashboard.py:76-87) and `FETCH_TIMEOUT`.

- [ ] **Step 1: Headless launch + real refresh**

Run the app against a fresh temp DB and confirm the Garmin refresh button path returns quickly and/or errors fast:

```bash
rm -f /tmp/tt_smoke.sqlite
TRAINING_DB=/tmp/tt_smoke.sqlite ../.venv/bin/python -m streamlit run dashboard.py \
  --server.headless true --server.port 8601 &
# (log shows boot ok)
```

Then run the owned fetch directly via Task 3 Step 5 once more and confirm **reliable fast completion** (auth_path=tokenstore). Remove `/tmp/tt_smoke.sqlite`.

- [ ] **Step 2: Improve fail-fast latency (fat-fail on unreachable Garmin)**

Because the owned HTTP layer sets a 15s per-request timeout, a dead Garmin now fails in at most ~15-30s (auth+first page) rather than 90s. Confirm the dashboard's existing `except Exception` surfaces `GarminHttpError`/`GarminAuthError` text clearly. If a clearer message is desired, adjust `refresh_garmin` to wrap `GarminHttpError` with a friendlier string (optional; only if tests/lint still pass).

- [ ] **Step 3: Full test suite + lint**

Run: `../.venv/bin/python -m pytest tests/ -q` from `v2/`. Expected: all pass (116 + new).

Verify no garminconnect import remains on the routine path by grepping:

```bash
rg -n "from garminconnect import|import garminconnect" v2/gateway.py v2/garmin_http.py
```

Expected: garminconnect appears ONLY inside `_login_garminconnect` (the last-resort fallback) in `gateway.py`, and never in `garmin_http.py`.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "feat(v2): verify dashboard refresh against owned Garmin HTTP layer"
```