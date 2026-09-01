"""Owned Garmin REST HTTP layer using the v2 DI tokenstore.

garminconnect's own request layer tunnels through Cloudflare-TLS/curl_cffi
strategies that can stall for minutes. This module owns the routine path
(auth via the persisted DI token + connectapi GETs) with strict timeouts.
"""

from __future__ import annotations

import base64
import contextlib
import json
import threading
import time
from pathlib import Path

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


# Per-call hard deadline for a single connectapi GET. Healthy calls return in
# <1s; a stalled SSL body read can't be aborted by requests' socket timeout
# alone (see systematic-debugging trace: recv_into hangs indefinitely), so each
# request runs on a daemon thread bounded by this deadline and the thread is
# abandoned if it overstays — converting a silent stall into GarminHttpError.
GET_DEADLINE_SECONDS = 10.0


def _threaded_get(url: str, headers: dict, params: dict | None, budget: float) -> requests.Response:
    box: dict = {}

    def _do():
        try:
            box["resp"] = requests.get(
                url, headers=headers, params=params, timeout=min(budget, 5.0)
            )
        except BaseException as e:  # noqa: BLE001 — re-raised to caller
            box["error"] = e

    worker = threading.Thread(target=_do, daemon=True)
    worker.start()
    worker.join(budget)
    if worker.is_alive():
        raise GarminHttpError(f"GET {url} did not complete within {budget}s")
    if "error" in box:
        raise box["error"]
    return box["resp"]


_DEFAULT_TOKENSTORE = Path.home() / ".garmin-mcp" / "v2_tokenstore.json"


class GarminTokenStore:
    def __init__(
        self, path: Path = _DEFAULT_TOKENSTORE, timeout: float = 15.0
    ):
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

    def access_token(self) -> str | None:
        return self._data.get("di_token")

    def _load_expiry(self) -> float | None:
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
        return bool(self.access_token()) and not self.expires_soon()

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
        with contextlib.suppress(Exception):
            self._data["di_client_id"] = self._re_extract_client_id(
                j["access_token"]
            ) or self._data.get("di_client_id")
        self._persist()
        return True

    def _re_extract_client_id(self, token: str) -> str | None:
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


ACTIVITIES_PATH = "/activitylist-service/activities/search/activities"


class GarminHttpError(Exception):
    """Raised when a Garmin connectapi call fails (network or non-200)."""


class GarminHttp:
    def __init__(
        self, tokenstore: GarminTokenStore, max_retries: int = 3, retry_backoff: float = 1.0
    ):
        self.tokenstore = tokenstore
        self._display_name: str | None = None
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff

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
        budget = min(self.tokenstore.timeout, GET_DEADLINE_SECONDS)
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                r = _threaded_get(url, headers=headers, params=params, budget=budget)
                if r.status_code != 200:
                    raise GarminHttpError(f"API {r.status_code} for {path}: {r.text[:200]}")
                try:
                    return r.json()
                except ValueError as e:
                    raise GarminHttpError(f"non-JSON response for {path}") from e
            except (requests.RequestException, GarminHttpError) as e:
                last_error = e
                if attempt < self._max_retries and self._retry_backoff:
                    time.sleep(self._retry_backoff)
        raise last_error  # type: ignore[misc]

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
        seen: set = set()
        offset = 0
        limit = 100
        while True:
            page = (
                self.get_json(
                    ACTIVITIES_PATH,
                    params={
                        "startDate": start,
                        "endDate": end,
                        "limit": str(limit),
                        "offset": str(offset),
                    },
                )
                or []
            )
            # Deduplicate by activityId AND detect a non-advancing offset:
            # Garmin's connectapi can ignore `offset` and return the same full
            # page indefinitely, so `len(page) < limit` never fires and the
            # loop spins forever. When an entire page is already-seen activityIds
            # the offset isn't moving — bail to avoid an unbounded fetch.
            new_ids = 0
            for row in page:
                aid = row.get("activityId") if isinstance(row, dict) else None
                if aid is None or aid not in seen:
                    seen.add(aid)
                    out.append(row)
                    new_ids += 1
            if len(page) < limit or new_ids == 0:
                break
            offset += limit
        return out

    def fetch_lactate_threshold(self) -> dict:
        import datetime as _dt

        power = self.get_json(
            f"/biometric-service/biometric/powerToWeight/latest/{_dt.date.today()}?sport=Running"
        )
        if isinstance(power, list) and power:
            power_dict = power[0]
        elif isinstance(power, dict):
            power_dict = power
        else:
            power_dict = {}
        sweat = self.get_json("/biometric-service/biometric/latestLactateThreshold")
        card = {
            "userProfilePK": None,
            "version": None,
            "calendarDate": None,
            "sequence": None,
            "speed": None,
            "heartRate": None,
            "heartRateCycling": None,
        }
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

    def fetch_vo2max_trend(self, start_date: str, end_date: str) -> list[dict]:
        """Fetch daily VO2max trend for a date range.

        Uses /metrics-service/metrics/maxmet/daily/{start}/{end} which returns
        historical daily values (unlike 'latest' which always returns current).
        Returns a list of daily objects with 'generic' (running) VO2max data.
        """
        payload = self.get_json(
            f"/metrics-service/metrics/maxmet/daily/{start_date}/{end_date}"
        )
        return payload if isinstance(payload, list) else []
