"""Lightweight JSON-RPC over stdio wrapper for garmin-connect-mcp."""
import json
import subprocess
import os
from pathlib import Path

MCP_CMD = ["npx", "-y", "@nicolasvegam/garmin-connect-mcp"]
ENV = {
    "GARMIN_EMAIL": "YOUR_EMAIL@example.com",
    "GARMIN_PASSWORD": "!op item get 'Garmin' --fields label=password --reveal",
}


class GarminClient:
    def __init__(self):
        self.cache_dir = Path("cache")
        self.cache_dir.mkdir(exist_ok=True)
        self.proc = None

    def call(self, method: str, params: dict = None) -> dict:
        req = {"jsonrpc": "2.0", "id": method, "method": method, "params": params or {}}
        payload = json.dumps(req) + "\n"
        self.proc = subprocess.Popen(
            MCP_CMD,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, **ENV},
        )
        self.proc.stdin.write(payload)
        self.proc.stdin.flush()
        self.proc.stdin.close()
        stdout = self.proc.stdout.read()
        self.proc.wait()
        try:
            return json.loads(stdout)
        except Exception:
            # Server may emit non-JSON startup; return raw wrapper for debugging
            return {"raw_stdout": stdout[:2000], "method": method}

    def fetch_activities(self, start_date: str = None, end_date: str = None):
        result = self.call("get_activities", {"start": start_date, "end": end_date})
        ts_path = self.cache_dir / f"garmin_raw_{start_date or 'all'}_{end_date or 'now'}.json"
        ts_path.write_text(json.dumps(result, indent=2, default=str))
        with open(self.cache_dir / "last_fetch_timestamp", "w") as f:
            from datetime import datetime, timezone
            f.write(datetime.now(timezone.utc).isoformat())
        return result
