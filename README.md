# training-tracker

Personal Garmin running tracker.

## Quickstart

1. Install (creates `.venv` with app + dev dependencies): `uv sync --all-extras`
2. Run tests: `.venv/bin/python -m pytest -p no:cacheprovider -q`
3. Launch dashboard: `.venv/bin/streamlit run dashboard.py`
4. DB location: `data/training.sqlite` (gitignored; override with `TRAINING_DB` env var). Garmin auth uses `~/.garmin-mcp/v2_tokenstore.json` with 1Password/env fallback (`GARMIN_EMAIL`/`GARMIN_PASSWORD`).
5. Backups: every scheduled refresh snapshots the store to `data/backups/` before it writes (the newest 7 are kept). To restore: stop the dashboard, copy the wanted `training-<stamp>.sqlite` over `data/training.sqlite`, delete any leftover `training.sqlite-wal` / `training.sqlite-shm`, then restart the dashboard.

## Data flow

Garmin API → `gateway.py` → `normalize.py` → `pipeline.py` → `store.py` (SQLite) → `session.py` → `dashboard.py`

## References

- Commands: [AGENTS.md](AGENTS.md)
- Metric definitions: [docs/v2/metrics-spec.md](docs/v2/metrics-spec.md)