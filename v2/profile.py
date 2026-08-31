"""Runner-level config: HRmax/HRrest/sex/birth/LTHR override/HR zones + Banister params."""
from dataclasses import dataclass, field, replace
from datetime import date
from typing import ClassVar

from metrics.hrmax import estimate_hrmax

BANISTER_B = {"M": 1.92, "F": 1.67}
BANISTER_INTERCEPT = 0.64


@dataclass
class RunnerProfile:
    hrmax: int
    hrrest: int
    sex: str
    birth_year: int
    lthr_manual: int | None = None
    hr_zones: dict[int, tuple[int, int]] = field(default_factory=dict)
    units: str = "metric"
    hrmax_source: str = "configured"  # "configured" | "age_predicted"

    EDWARDS_WEIGHTS: ClassVar[dict[int, float]] = {1: 1.0, 2: 2.0, 3: 3.0, 4: 4.0, 5: 5.0}

    @classmethod
    def from_age(cls, age: int, hrrest: int, sex: str, birth_year: int, **kw):
        return cls(hrmax=220 - age, hrrest=hrrest, sex=sex, birth_year=birth_year,
                   hrmax_source="age_predicted", **kw)

    def age_predicted_hrmax(self) -> int:
        return 220 - (date.today().year - self.birth_year)

    def banister_exponent(self) -> float:
        return BANISTER_B.get(self.sex.upper(), 1.92)

    def exp_intercept_factor(self) -> float:
        return BANISTER_INTERCEPT


def default_profile(age: int = 40, hrrest: int = 60, sex: str = "M",
                    birth_year: int | None = None, lthr_manual: int | None = None) -> RunnerProfile:
    by = birth_year or (date.today().year - age)
    p = RunnerProfile.from_age(age=age, hrrest=hrrest, sex=sex, birth_year=by, lthr_manual=lthr_manual)
    p.hr_zones = {1: (0, int(0.60 * p.hrmax)), 2: (int(0.60 * p.hrmax) + 1, int(0.70 * p.hrmax)),
                  3: (int(0.70 * p.hrmax) + 1, int(0.80 * p.hrmax)), 4: (int(0.80 * p.hrmax) + 1, int(0.90 * p.hrmax)),
                  5: (int(0.90 * p.hrmax) + 1, p.hrmax)}
    return p


def with_estimated_hrmax(profile: RunnerProfile, activities, min_recurrence: int = 2,
                         floor: int = 120) -> RunnerProfile:
    """Return a copy of `profile` with HRmax observed from past workouts.

    A manually `configured` HRmax is always respected (estimation never
    overrides an explicit value). When no recurring observed HR is available,
    returns the original profile (age-predicted) unchanged.
    """
    if profile.hrmax_source == "configured":
        return profile
    estimate = estimate_hrmax(activities, min_recurrence=min_recurrence, floor=floor)
    if estimate is None or estimate == profile.hrmax:
        return profile
    return replace(profile, hrmax=estimate, hrmax_source="observed")