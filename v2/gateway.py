"""Garmin REST gateway. Credentials from 1Password via `op` CLI, with env fallback.

Wraps garminconnect (Garmin client) for auth + connectapi. All token/credential
handling stays out of the repo: `op item get Garmin` or GARMIN_EMAIL/GARMIN_PASSWORD.

Auth resolution:
1. A garminconnect-native tokenstore (preferred path: ~/.garmin-mcp/v2_tokenstore.json)
   so routine runs avoid password/MFA.
2. Migration of the v1 MCP OAuth file (~/.garmin-mcp/oauth2_token.json), whose
   `access_token` is a Garmin DI JWT carrying a `client_id` claim, into the native
   tokenstore shape; the session is refreshed with its `refresh_token`.
3. Fresh login with credentials from 1Password (`op item get Garmin`) or the
   GARMIN_EMAIL/GARMIN_PASSWORD env vars.

Successful logins persist a native tokenstore to ~/.garmin-mcp/v2_tokenstore.json
(never overwriting the MCP server's oauth2_token.json).
"""
import base64
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from garminconnect import Garmin

ACTIVITIES_PATH = "/activitylist-service/activities/search/activities"
DETAILS_PATH = "/activity-service/activity/{activity_id}/details"
TOKENSTORE = Path.home() / ".garmin-mcp" / "oauth2_token.json"
TOKENSTORE_V2 = Path.home() / ".garmin-mcp" / "v2_tokenstore.json"


def load_op_creds(item: str = "Garmin") -> tuple[str | None, str | None]:
    """Read username/password from 1Password. Returns (None, None) if `op` is missing."""
    try:
        result = subprocess.run(
            ["op", "item", "get", item, "--fields", "username,password",
             "--format", "json", "--reveal"],
            capture_output=True, text=True, timeout=15,
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
    parts = [x.strip() for x in result.stdout.replace("\r", "").replace(",", "\n").split("\n")
             if x.strip()]
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
    return json.dumps({
        "di_token": access_token,
        "di_refresh_token": refresh_token,
        "di_client_id": client_id,
    })


def choose_token_source(v2_path: Path = TOKENSTORE_V2, mcp_path: Path = TOKENSTORE) -> tuple[str | None, str | None]:
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
    def __init__(self, cache_dir: Path = Path("cache/test_cache"),
                 tokenstore_v2: Path = TOKENSTORE_V2):
        """Auth resolution order:
        1. Native tokenstore (v2 dump, else MCP file if already native).
        2. Migrated legacy MCP OAuth token (DI JWT + refresh_token).
        3. Fresh login via `op item get Garmin` (fields username/password) or
           GARMIN_EMAIL/GARMIN_PASSWORD env fallback.
        On success a native tokenstore is persisted to tokenstore_v2 so later
        runs restore over the token alone. MFA is not expected on this account.
        """
        email, password = load_op_creds()
        if not email or not password:
            email = os.environ.get("GARMIN_EMAIL")
            password = os.environ.get("GARMIN_PASSWORD")
        self._garmin = Garmin(email=email, password=password, is_cn=False)
        kind, value = choose_token_source()
        if kind in ("path", "migrated"):
            try:
                self._garmin.login(tokenstore=value)
                self.auth_path = "tokenstore"
            except Exception:
                if not (email and password):
                    raise RuntimeError(
                        "Garmin auth unavailable: tokenstore failed and no creds"
                    )
                self.auth_path = "op_credentials"
                self._garmin.login()
        elif email and password:
            self.auth_path = "op_credentials"
            self._garmin.login()
        else:
            raise RuntimeError(
                "Garmin auth unavailable: no tokenstore, no op creds, no env creds"
            )
        self._persist_tokens(tokenstore_v2)
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _persist_tokens(self, path: Path) -> None:
        try:
            data = self._garmin.client.dumps()
            path = Path(path).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(data)
        except Exception:
            pass

    def _cache(self, name: str, payload) -> None:
        path = self.cache_dir / name
        path.write_text(json.dumps(payload, indent=2, default=str))
        (self.cache_dir / "last_fetch_timestamp").write_text(
            datetime.now(timezone.utc).isoformat()
        )

    def fetch_activities(self, start: str, end: str) -> list[dict]:
        out: list[dict] = []
        offset = 0
        limit = 100
        while True:
            page = self._garmin.connectapi(
                ACTIVITIES_PATH,
                params={"startDate": start, "endDate": end, "limit": limit, "offset": offset},
            ) or []
            out.extend(page)
            if len(page) < limit:
                break
            offset += limit
        self._cache("garmin_raw.json", out)
        return out

    def fetch_activity_details(self, activity_id: int) -> dict:
        payload = self._garmin.connectapi(
            DETAILS_PATH.format(activity_id=activity_id),
            params={"maxChartSize": 2000, "maxPolylineSize": 4000},
        ) or {}
        self._cache(f"activity_details_{activity_id}.json", payload)
        return payload

    def fetch_lactate_threshold(self) -> dict:
        payload = self._garmin.get_lactate_threshold(latest=True)
        self._cache("lactate_threshold.json", payload)
        return payload

    def fetch_race_predictions(self) -> dict:
        payload = self._garmin.get_race_predictions()
        self._cache("race_predictions.json", payload)
        return payload

    def fetch_training_status(self, cdate: str) -> dict:
        payload = self._garmin.get_training_status(cdate)
        self._cache(f"training_status_{cdate}.json", payload)
        return payload