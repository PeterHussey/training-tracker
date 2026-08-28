"""Persistent stdio JSON-RPC wrapper for garmin-connect-mcp."""
import json, subprocess, os, time
from pathlib import Path

ENV = {"GARMIN_EMAIL": "YOUR_EMAIL@example.com", "GARMIN_PASSWORD": "!op item get 'Garmin' --fields label=password --reveal"}

class GarminClient:
    def __init__(self):
        self.proc = None
        self.cache_dir = Path("cache")
        self.cache_dir.mkdir(exist_ok=True)

    def _start(self):
        if self.proc is None or self.proc.poll() is not None:
            self.proc = subprocess.Popen(
                ["npx", "-y", "@nicolasvegam/garmin-connect-mcp"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env={**os.environ, **ENV},
            )
            # Consume server startup banner (non-JSON lines before first response)
            time.sleep(0.5)

    def call(self, method: str, params: dict = None) -> dict:
        self._start()
        req = {"jsonrpc":"2.0","id":method,"method":method,"params":params or {}}
        line = json.dumps(req) + "\n"
        try:
            self.proc.stdin.write(line); self.proc.stdin.flush()
            # Read line-by-line until we get a JSON response line
            for _ in range(30):
                out = self.proc.stdout.readline()
                if out:
                    out = out.strip()
                    if out.startswith("{") or out.startswith("["):
                        try:
                            return json.loads(out)
                        except json.JSONDecodeError:
                            continue
            return {"raw_stdout":"timeout/no-json","method":method}
        except Exception as e:
            return {"error":str(e),"method":method}

    def fetch_activities(self, start_date="2026-02-28", end_date="2026-02-28"):
        res = self.call("get_activities", {"start":start_date,"end":end_date})
        ts_path = self.cache_dir / f"garmin_raw_{start_date or 'all'}_{end_date or 'now'}.json"
        ts_path.write_text(json.dumps(res, indent=2, default=str))
        Path("cache/last_fetch_timestamp").write_text(__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat())
        return res

    def get_vo2max(self) -> dict:
        return self.call("get_vo2max")

    def get_lactate_threshold(self) -> dict:
        return self.call("get_lactate_threshold")
