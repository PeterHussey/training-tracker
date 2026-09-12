"""Acute:Chronic workload ratio — a load-SWING monitor, never an injury gate.

Research brief 1.1: population ACWR bands are not validated for individual
running risk (Nakaoka found an inverse association). Treat as a monitoring
signal for load swing, never as a deterministic risk gate.
"""

import pandas as pd


def coupled_acwr(daily: pd.Series, acute: int = 7, chronic: int = 28) -> pd.Series:
    """Coupled ACWR, average-normalized: (mean daily load over acute window) /
    (mean daily load over chronic window). Steady constant load => 1.0, which
    the raw sum7/sum28 ratio does NOT give (it asymptotes at 7/28). The input
    must be a dense daily calendar series; the pipeline densifies TRIMP with
    fill_value=0 before calling this.
    """
    s = daily.sort_index()
    acute_avg = s.rolling(acute, min_periods=acute).sum() / acute
    chronic_avg = s.rolling(chronic, min_periods=chronic).sum() / chronic
    # NaN (not pd.NA): keeps the float dtype so downstream rolling ops
    # keep working across training gaps.
    ratio = acute_avg / chronic_avg.replace(0, float("nan"))
    ratio.name = "acwr"
    return ratio
