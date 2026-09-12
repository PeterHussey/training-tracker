import pandas as pd
import pytest

from metrics.acwr import coupled_acwr


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


def test_training_gap_yields_nan_not_crash():
    """A 28-day zero-load gap makes the chronic window 0. The ratio must be
    NaN (missing), not inf."""
    idx = pd.date_range("2026-04-01", periods=90, freq="D")
    tr = pd.Series([100.0] * 30 + [0.0] * 30 + [100.0] * 30, index=idx)
    acwr = coupled_acwr(tr)
    assert acwr.dtype == float
    # mid-gap with residual chronic load: defined 0.0 (full detraining)
    assert acwr.loc["2026-05-15"] == 0.0
    # deep gap with an all-zero chronic window: NaN (missing), not inf
    assert pd.isna(acwr.loc["2026-05-29"])
