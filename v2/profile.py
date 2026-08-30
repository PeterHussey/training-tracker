"""Runner-level config: HRmax/HRrest/sex/birth/LTHR override/HR zones + Banister params."""
from dataclasses import dataclass, field
from datetime import date
from typing import ClassVar

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