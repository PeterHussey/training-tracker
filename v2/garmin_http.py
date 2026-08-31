"""Owned Garmin REST HTTP layer using the v2 DI tokenstore.

garminconnect's own request layer tunnels through Cloudflare-TLS/curl_cffi
strategies that can stall for minutes. This module owns the routine path
(auth via the persisted DI token + connectapi GETs) with strict timeouts.
"""
from __future__ import annotations

import base64
import json
import time
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
        try:
            self._data["di_client_id"] = (
                self._re_extract_client_id(j["access_token"]) or self._data.get("di_client_id")
            )
        except Exception:
            pass
        self._persist()
        return True

    def _re_extract_client_id(self, token: str) -> Optional[str]:
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
