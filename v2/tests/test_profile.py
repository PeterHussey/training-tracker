# tests/test_profile.py
from datetime import date

import pytest
from profile import RunnerProfile, default_profile


def test_age_predicted_hrmax_fallback():
    p = default_profile(age=40)
    assert p.hrmax == 180  # 220 - 40


def test_banister_exponent_by_sex():
    assert default_profile(age=40, sex="M").banister_exponent() == pytest.approx(1.92)
    assert default_profile(age=40, sex="F").banister_exponent() == pytest.approx(1.67)


def test_hrmax_boundaries():
    p = RunnerProfile(hrmax=200, hrrest=50, sex="M", birth_year=1986, lthr_manual=None, hr_zones={}, units="metric")
    assert p.age_predicted_hrmax() == 220 - (date.today().year - 1986)