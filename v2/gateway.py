"""Garmin REST gateway. Credentials from 1Password via `op` CLI, with env fallback.

Wraps garminconnect (Garmin client) for auth + connectapi. All token/credential
handling stays out of the repo: `op item get Garmin` or GARMIN_EMAIL/GARMIN_PASSWORD.
"""
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from garminconnect import Garmin

ACTIVITIES_PATH = "/activitylist-service/activities/search/activities"
DETAILS_PATH = "/activity-service/activity/{activity_id}/details"
TOKENSTORE = Path.home() / ".garmin-mcp" / "oauth2_token.json"


def load_op_creds(item: str = "Garmin") -> tuple[str | None, str | None]:
    """Read username/password from 1Password. Returns (None, None) if `op` is missing."""
    try:
        result = subprocess.run(
            ["op", "item", "get", item, "--fields", "username", "--fields", "password", "--reveal"],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        return None, None
    if result.returncode != 0:
        return None, None
    parts = result.stdout.strip().split(",")
    if len(parts) >= 2:
        return parts[0].rstrip(), parts[1].lstrip()
    return None, None


class GarminGateway:
    def __init__(self, cache_dir: Path = Path("cache/test_cache")):
        """Auth resolution order:
        1. Existing tokenstore at ~/.garmin-mcp/oauth2_token.json (avoids
           password/MFA on routine runs; provisioned by the v1 MCP login).
        2. Fresh login with credentials read from 1Password via `op item get
           Garmin` (fields username/password); GARMIN_EMAIL/GARMIN_PASSWORD
           env fallback for CI.
        A required-MFA failure on the fresh-login path is a one-time manual
        action: refresh the tokenstore once, then retry.
        """
        if TOKENSTORE.exists():
            self.auth_path = "tokenstore"
            self._garmin = Garmin(is_cn=False)
            self._garmin.login(tokenstore=str(TOKENSTORE))
        else:
            self.auth_path = "op_credentials"
            email, password = load_op_creds()
            if not email or not password:
                email = os.environ.get("GARMIN_EMAIL")
                password = os.environ.get("GARMIN_PASSWORD")
            if not email or not password:
                raise RuntimeError(
                    "Garmin auth unavailable: no tokenstore, no op creds, no env creds"
                )
            self._garmin = Garmin(email=email, password=password)
            self._garmin.login()
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

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
