import pandas as pd
import pytest

from metrics.acwr import coupled_acwr, history_percentile


def test_constant_load_yields_ratio_one():
    idx = pd.date_range("2026-04-01", periods=35, freq="D")
    tr = pd.Series([100.0] * 35, index=idx)
    acwr = coupled_acwr(tr)
    last = acwr.dropna().iloc[-1]
    assert last == pytest.approx(1.0)


def test_acute_chronic_windows():
    idx = pd.date_range("2026-04-01", periods=35, freq="D")
    base = [100.0] * 28 + [200.0] * 7
    tr = pd.Series(base, index=idx)
    acwr = coupled_acwr(tr)
    # 2026-05-01 is day 31: acute window = indices 24..30 (4x100 + 3x200 = 1000 -> /7)
    # chronic window = indices 3..30 (25x100 + 3x200 = 3100 -> /28)
    # ratio = (1000/7)/(3100/28) = 4000/3100 ~= 1.29 > 1.0
    day31 = acwr[acwr.index == pd.Timestamp("2026-05-01")].iloc[0]
    assert day31 == pytest.approx(4000.0 / 3100.0)


def test_history_percentile_bounds():
    idx = pd.date_range("2026-04-01", periods=60, freq="D")
    tr = pd.Series(100.0, index=idx)
    acwr = coupled_acwr(tr)
    hp = history_percentile(acwr, window=30)
    # all equal -> pandas rank(pct=True) uses pct = avg_rank / nobs; for an
    # all-equal window of n values, avg_rank = (n+1)/2, so pct = (n+1)/(2n) = 31/60.
    assert hp["history_pct"].iloc[-1] == pytest.approx((30 + 1) / (2 * 30))
