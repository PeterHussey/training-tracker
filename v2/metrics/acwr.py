"""Acute:Chronic workload ratio — a load-SWING monitor, never an injury gate.

Research brief 1.1: population ACWR bands are not validated for individual
running risk (Nakaoka found an inverse association). Use only individual-history
percentiles, and flag rapid change rather than absolute bands.
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
    ratio = acute_avg / chronic_avg.replace(0, pd.NA)
    ratio.name = "acwr"
    return ratio


def history_percentile(acwr: pd.Series, window: int = 180) -> pd.DataFrame:
    pct = acwr.rolling(window, min_periods=20).rank(pct=True)
    return pd.DataFrame({"acwr": acwr, "history_pct": pct})
