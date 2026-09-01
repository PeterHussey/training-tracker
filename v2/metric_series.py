"""Convert calculator output (pd.Series / pd.DataFrame) into metric_series rows."""

import json

import pandas as pd


def rows_from_series(metric: str, series, source: str, params=None, flags=None) -> list[dict]:
    params = params or {}
    flags = flags or {}
    if isinstance(series, pd.DataFrame):
        rows = []
        for col in series.columns:
            rows.extend(rows_from_series(f"{metric}.{col}", series[col], source, params, flags))
        return rows
    rows = []
    for i, value in series.items():
        if pd.isna(value):
            continue
        date = i.strftime("%Y-%m-%d") if hasattr(i, "strftime") else str(i)
        rows.append(
            {
                "metric": metric,
                "date": date,
                "value": float(value),
                "source": source,
                "params": json.dumps(params, sort_keys=True),
                "flags": json.dumps(flags, sort_keys=True),
            }
        )
    return rows
