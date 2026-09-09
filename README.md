# training-tracker

Personal Garmin running tracker.

## Quickstart

1. Run tests: `.venv/bin/python -m pytest -p no:cacheprovider -q`
2. Launch dashboard: `.venv/bin/streamlit run dashboard.py`
3. DB location: `data/training.sqlite` (gitignored; override with `TRAINING_DB` env var). Garmin auth uses `~/.garmin-mcp/v2_tokenstore.json` with 1Password/env fallback (`GARMIN_EMAIL`/`GARMIN_PASSWORD`).

## Data flow

Garmin API → `gateway.py` → `normalize.py` → `pipeline.py` → `store.py` (SQLite) → `session.py` → `dashboard.py`

## References

- Commands: [AGENTS.md](AGENTS.md)
- Metric definitions: [docs/v2/metrics-spec.md](docs/v2/metrics-spec.md)