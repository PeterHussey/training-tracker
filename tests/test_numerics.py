"""Goldens ported from Runalyze's BisectionTest (Bisection.php)."""

from metrics import numerics


def test_identity_target_5_exact():
    assert numerics.find_value(lambda x: x, 5.0, 0, 10) == 5.0


def test_iteration_exhaust_returns_current_not_midpoint():
    # 4 iterations on [0,10] converging on 1: sequence 5,2.5,1.25,0.625 ->
    # 0.625 breaks to lower? verify sequence stops with returned Current == 0.9375
    assert numerics.find_value(lambda x: x, 1.0, 0, 10, max_iterations=4) == 0.9375


def test_full_iterations_converge_within_epsilon():
    assert abs(numerics.find_value(lambda x: x, 1.0, 0, 10) - 1.0) < 0.01


def test_cubic_target_7_converges_to_1():
    assert abs(numerics.find_value(lambda x: x**3 + 6, 7.0, -50, 20) - 1.0) <= 0.01
