"""Bisection root finder for inverting monotonic metric formulas.

Ported from Runalyze 4.3.x
(support/4.3.x @ 5669237a78c7d3512df969052fd0d97e5dca6820),
inc/core/Mathematics/Numerics/Bisection.php, AGPL.

Used to invert closed-form pace/prognosis formulas that have no analytic
inverse. ``Epsilon`` is a tolerance on the *function value*, not on the input.

No module-level imports from Runalyze or this repo: pure numerics.
"""

from collections.abc import Callable

MAX_ITERATIONS = 100
EPSILON = 0.01


def find_value(
    f: Callable[[float], float],
    target: float,
    lower: float,
    upper: float,
    max_iterations: int = MAX_ITERATIONS,
    epsilon: float = EPSILON,
) -> float:
    """Find ``x`` in ``[lower, upper]`` such that ``f(x)`` is within ``epsilon`` of ``target``.

    Mirrors Runalyze's ``Bisection``: the returned value is the last probe
    (``Current``), not a midpoint recomputed after the loop, so a run that
    exhausts ``max_iterations`` returns the final bisection probe as-is.
    """
    current = (lower + upper) / 2
    decreasing_factor = 1 if f(lower) <= f(upper) else -1

    for _ in range(max_iterations):
        value = f(current)

        if abs(value - target) < epsilon:
            break
        elif decreasing_factor * value > decreasing_factor * target:
            upper = current
        else:
            lower = current

        current = (lower + upper) / 2

    return current
