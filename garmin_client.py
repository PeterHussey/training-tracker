"""Persistent stdio JSON-RPC wrapper for garmin-connect-mcp."""
import json, subprocess, os, time
from pathlib import Path

def _load_op_creds():
    import subprocess
    result = subprocess.run(
        ["op", "item", "get", "Garmin", "--fields", "username", "--fields", "password", "--reveal"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return None, None
    parts = result.stdout.strip().split(",")
    if len(parts) >= 2:
        return parts[0], parts[1]
    return None, None

ENV = {}
_email, _password = _load_op_creds()
if _email and _password:
    ENV = {"GARMIN_EMAIL": _email, "GARMIN_PASSWORD": _password}
MCP_CMD = ["/opt/homebrew/bin/bun", "x", "-y", "@nicolasvegam/garmin-connect-mcp"]

class GarminClient:
    def __init__(self):
        self.proc = None
        self.cache_dir = Path("cache")
        self.cache_dir.mkdir(exist_ok=True)

    def _start(self):
        if self.proc is None or self.proc.poll() is not None:
            self.proc = subprocess.Popen(
                MCP_CMD,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env={**os.environ, **ENV},
            )
            time.sleep(0.5)

    def call(self, method: str, params: dict = None) -> dict:
        try:
            self._start()
        except FileNotFoundError as e:
            return {"status":"no_mcp_server","message":"npx not in PATH (Node/npm missing). Refresh will not work until node/npm installed and `npx` available. Live auth tokens present at ~/.garmin-mcp/.","error":str(e),"method":method,"suggested_fix":"Install node/npm; verify `which npx`."}
        req = {"jsonrpc":"2.0","id":method,"method":method,"params":params or {}}
        line = json.dumps(req) + "\n"
        try:
            self.proc.stdin.write(line); self.proc.stdin.flush()
            for _ in range(30):
                out = self.proc.stdout.readline()
                if out:
                    out = out.strip()
                    if out.startswith("{") or out.startswith("["):
                        try:
                            return json.loads(out)
                        except json.JSONDecodeError:
                            continue
            return {"raw_stdout":"timeout/no-json","method":method,"note":"Server may have started but produced no JSON line; check stderr."}
        except Exception as e:
            return {"error":str(e),"method":method,"note":"Subprocess failed — likely npx missing or MCP server crashed."}

    def fetch_activities(self, start_date="2026-02-28", end_date="2026-02-28"):
        res = self.call("get_activities", {"start":start_date,"end":end_date})
        ts_path = self.cache_dir / "garmin_raw.json"
        ts_path.write_text(json.dumps(res, indent=2, default=str))
        Path("cache/last_fetch_timestamp").write_text(__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat())
        return res

    def get_vo2max(self) -> dict:
        return self.call("get_vo2max")

    def get_lactate_threshold(self) -> dict:
        return self.call("get_lactate_threshold")

# Real live fallback using cached OAuth token + Garmin REST API directly
import requests

def fetch_activities_live(start_date="2026-02-28", end_date="2026-02-28") -> dict:
    from urllib.parse import quote
    token_path = Path.home() / ".garmin-mcp" / "oauth2_token.json"
    if not token_path.exists():
        return {"error":"No OAuth token at ~/.garmin-mcp/oauth2_token.json"}
    try:
        token_data = json.loads(token_path.read_text())
        token = token_data.get("access_token")
        if not token:
            return {"error":"No access_token in OAuth file"}
    except Exception as e:
        return {"error":f"Failed to read token: {e}"}
    headers = {"Authorization": f"Bearer {token}", "User-Agent": "python-garminconnect"}
    url = f"https://connectapi.garmin.com/activitylist-service/activities/search/activities?startDate={quote(start_date)}&endDate={quote(end_date)}"
    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        ts_path = Path("cache") / "garmin_raw.json"
        Path("cache").mkdir(exist_ok=True)
        ts_path.write_text(json.dumps(data, indent=2))
        with open("cache/last_fetch_timestamp", "w") as f:
            f.write(__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat())
        return {"status":"live","method":"curl/get","data":data,"cached_to":str(ts_path)}
    except Exception as e:
        return {"status":"live_failed","error":str(e),"suggestion":"Token may be expired; try refreshing auth via Garmin MCP server."}
