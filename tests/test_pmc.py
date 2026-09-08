import pandas as pd
import pytest

from metrics.pmc import ctl_atl_tsb


def test_tsb_is_ctl_minus_atl_and_ctl_smoother():
    idx = pd.date_range("2026-04-01", periods=14, freq="D")
    tr = pd.Series([100.0, 50.0] + [50.0] * 12, index=idx)
    out = ctl_atl_tsb(tr)
    assert list(out.columns) == ["ctl", "atl", "tsb"]
    assert out["tsb"].iloc[-1] == pytest.approx(out["ctl"].iloc[-1] - out["atl"].iloc[-1])
    # ctl (tau 42) is more sluggish than atl (tau 7), so right after the step
    # down to 50 it still sits higher than atl
    assert out["ctl"].iloc[1] > out["atl"].iloc[1]


def test_reindexes_sparse_days_with_zero():
    idx = pd.to_datetime(["2026-04-01", "2026-04-03"])
    tr = pd.Series([120.0, 90.0], index=idx)
    out = ctl_atl_tsb(tr)
    assert len(out) == 3  # Apr 1, 2, 3
    assert out.index[1] == pd.Timestamp("2026-04-02")


def test_empty_input_returns_empty():
    out = ctl_atl_tsb(pd.Series([], dtype=float))
    assert out.empty
