# training-tracker

Personal Garmin running tracker.

## Quickstart

1. Install (creates `.venv` with app + dev dependencies): `uv sync --all-extras`
2. Run tests: `.venv/bin/python -m pytest -p no:cacheprovider -q`
3. Launch dashboard: `.venv/bin/streamlit run dashboard.py`
4. DB location: `data/training.sqlite` (gitignored; override with `TRAINING_DB` env var). Garmin auth uses `~/.garmin-mcp/v2_tokenstore.json` with 1Password/env fallback (`GARMIN_EMAIL`/`GARMIN_PASSWORD`).
5. Backups: every scheduled refresh snapshots the store to `data/backups/` before it writes (the newest 7 are kept). To restore: stop the dashboard, copy the wanted `training-<stamp>.sqlite` over `data/training.sqlite`, delete any leftover `training.sqlite-wal` / `training.sqlite-shm`, then restart the dashboard.

## Service

The dashboard runs as a launchd agent on 127.0.0.1:8501, refreshing itself from Garmin daily at 05:00 with a health check at 06:30.

1. Install the three agents: `deploy/launchd/install.sh`
2. Check they are loaded: `launchctl list | grep trainingtracker`
3. Dashboard health: `curl -sf http://127.0.0.1:8501/_stcore/health`
4. Refresh on demand: `launchctl kickstart -k gui/$(id -u)/com.trainingtracker.refresh`
5. Health check by hand: `.venv/bin/python health_check.py` (exits 0 when every check passes; the verdict also shows in the dashboard sidebar)

## Data flow

Garmin API → `gateway.py` → `normalize.py` → `pipeline.py` → `store.py` (SQLite) → `session.py` → `dashboard.py`

## References

- Commands: [AGENTS.md](AGENTS.md)
- Metric definitions: [docs/v2/metrics-spec.md](docs/v2/metrics-spec.md)