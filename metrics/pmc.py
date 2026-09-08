"""Performance-Management chart: CTL (fitness, tau 42d), ATL (fatigue, tau 7d), TSB.

Research brief 1.3: TSB is a freshness/load-trajectory visualizer, not a
performance claim. Band thresholds are heuristics.
"""

import pandas as pd


def ctl_atl_tsb(daily_trimp: pd.Series, tau_ctl: int = 42, tau_atl: int = 7) -> pd.DataFrame:
    if daily_trimp.empty:
        return pd.DataFrame(columns=["ctl", "atl", "tsb"])
    full_idx = pd.date_range(daily_trimp.index.min(), daily_trimp.index.max(), freq="D")
    tr = daily_trimp.reindex(full_idx, fill_value=0.0)
    ctl = tr.ewm(alpha=1 / tau_ctl, adjust=False).mean()
    atl = tr.ewm(alpha=1 / tau_atl, adjust=False).mean()
    out = pd.DataFrame({"ctl": ctl, "atl": atl})
    out["tsb"] = out["ctl"] - out["atl"]
    return out
