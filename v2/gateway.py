"""Garmin REST gateway — owned HTTP layer primary, garminconnect fallback only.

Owned HTTP layer (`GarminHttp` via `GarminTokenStore`) is the routine auth path.
Only when no tokenstore exists (or load/auth fails) does the gateway fall back
to the `garminconnect` `Garmin` login (`op_credentials`). Credentials from 1Password
via `op` CLI (`load_op_creds`) or GARMIN_EMAIL/GARMIN_PASSWORD env vars remain
as the last-resort fallback.

Auth resolution order:
1. Native v2 tokenstore (`tokenstore_v2`) or migrated legacy MCP OAuth file.
2. Owned HTTP (`GarminTokenStore` + `GarminHttp`) — `auth_path="tokenstore"`.
3. `garminconnect` login — `auth_path="op_credentials"` (last resort).
"""

import base64
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from garmin_http import GarminHttp, GarminTokenStore

ACTIVITIES_PATH = "/activitylist-service/activities/search/activities"
DETAILS_PATH = "/activity-service/activity/{activity_id}/details"
TOKENSTORE = Path.home() / ".garmin-mcp" / "oauth2_token.json"
TOKENSTORE_V2 = Path.home() / ".garmin-mcp" / "v2_tokenstore.json"


def load_op_creds(item: str = "Garmin") -> tuple[str | None, str | None]:
    """Read username/password from 1Password. Returns (None, None) if `op` is missing."""
    try:
        result = subprocess.run(
            [
                "op",
                "item",
                "get",
                item,
                "--fields",
                "username,password",
                "--format",
                "json",
                "--reveal",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None, None
    if result.returncode != 0 or not result.stdout.strip():
        return None, None
    try:
        fields = json.loads(result.stdout)
        if isinstance(fields, list):
            by_label = {f.get("label"): f.get("value") for f in fields if isinstance(f, dict)}
            username, password = by_label.get("username"), by_label.get("password")
            if username and password:
                return username, password
    except json.JSONDecodeError:
        pass
    parts = [
        x.strip()
        for x in result.stdout.replace("\r", "").replace(",", "\n").split("\n")
        if x.strip()
    ]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return None, None


def _jwt_client_id(token: str) -> str | None:
    """Decode the `client_id` claim from a (DI) access-token JWT payload."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, json.JSONDecodeError):
        return None
    cid = claims.get("client_id")
    return cid if isinstance(cid, str) and cid else None


def migrate_mcp_token(path: Path = TOKENSTORE) -> str | None:
    """Build a garminconnect-native tokenstore string from the v1 MCP OAuth file.

    Returns the serialized tokenstore when the file carries a DI JWT (with a
    client_id claim) and a refresh token; None when it is not migratable.
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    access_token, refresh_token = data.get("access_token"), data.get("refresh_token")
    if not access_token or not refresh_token:
        return None
    client_id = _jwt_client_id(access_token)
    if not client_id:
        return None
    return json.dumps(
        {
            "di_token": access_token,
            "di_refresh_token": refresh_token,
            "di_client_id": client_id,
        }
    )


def choose_token_source(
    v2_path: Path = TOKENSTORE_V2, mcp_path: Path = TOKENSTORE
) -> tuple[str | None, str | None]:
    """Pick the best tokenstore available: native v2 > native MCP > migrated legacy."""
    if v2_path.exists():
        return "path", str(v2_path)
    if mcp_path.exists():
        try:
            data = json.loads(mcp_path.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
        if data.get("di_token"):
            return "path", str(mcp_path)
        migrated = migrate_mcp_token(mcp_path)
        if migrated is not None:
            return "migrated", migrated
    return None, None


class GarminGateway:
    def __init__(
        self, cache_dir: Path = Path("cache/test_cache"), tokenstore_v2: Path = TOKENSTORE_V2
    ):
        """Auth resolution order:
        1. Native tokenstore (v2 dump, else MCP file if already native).
        2. Migrated legacy MCP OAuth token (DI JWT + refresh_token).
        3. Fresh login via `op item get Garmin` (fields username/password) or
           GARMIN_EMAIL/GARMIN_PASSWORD env fallback.
        On success a native tokenstore is persisted to tokenstore_v2 so later
        runs restore over the token alone. MFA is not expected on this account.
        """
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
        self._login_garminconnect(email, password, tokenstore_v2=tokenstore_v2)

    def _materialise_migrated_store(self, store_json: str, tokenstore_v2: Path) -> Path:
        path = Path(tokenstore_v2)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(store_json)
        return path

    def _login_garminconnect(self, email, password, tokenstore_v2: Path = TOKENSTORE_V2) -> None:
        from garminconnect import Garmin  # lazy: keeps garminconnect off the routine path

        if not email or not password:
            raise RuntimeError("Garmin auth unavailable: no tokenstore, no op creds, no env creds")
        self._garmin = Garmin(email=email, password=password, is_cn=False)
        self._garmin.login()
        self.auth_path = "op_credentials"
        # Bootstrap an owned tokenstore from the garminconnect session so the
        # routine fetch path (GarminHttp) works without touching garminconnect.
        # garminconnect's `Client.dumps()` already emits the v2 native schema
        # {di_token, di_refresh_token, di_client_id}.
        try:
            tokenstore_v2.parent.mkdir(parents=True, exist_ok=True)
            tokenstore_v2.write_text(self._garmin.client.dumps())
            store = GarminTokenStore(tokenstore_v2, timeout=15.0)
            store.load()
            self._http = GarminHttp(store)
        except Exception:
            # garminconnect client is still usable directly as a last resort;
            # fetches that need the owned layer will raise a clear error.
            self._http = None

    def _cache(self, name: str, payload) -> None:
        path = self.cache_dir / name
        path.write_text(json.dumps(payload, indent=2, default=str))
        (self.cache_dir / "last_fetch_timestamp").write_text(datetime.now(UTC).isoformat())

    def fetch_activities(self, start: str, end: str) -> list[dict]:
        raw = self._require_http().fetch_activities(start, end)
        self._cache("garmin_raw.json", raw)
        return raw

    def fetch_activity_details(self, activity_id: int) -> dict:
        payload = (
            self._require_http().get_json(
                DETAILS_PATH.format(activity_id=activity_id),
                params={"maxChartSize": 2000, "maxPolylineSize": 4000},
            )
            or {}
        )
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

    def fetch_vo2max_trend(self, start: str, end: str) -> list[dict]:
        """Fetch daily VO2max trend for a date range.

        Returns a list of daily objects with 'generic' (running) VO2max data.
        Caches to vo2max_trend_{start}_{end}.json.
        """
        payload = self._require_http().fetch_vo2max_trend(start, end)
        self._cache(f"vo2max_trend_{start}_{end}.json", payload)
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
