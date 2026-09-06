# Period Selector — Store-Bound Date Range — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the dashboard's Period date selector reflect the store's actual date range, grow on refresh, show a store-range caption, and reset to a fresh default after each successful refresh.

**Architecture:** All production changes live in the thin Streamlit view `v2/dashboard.py` plus unit tests in `v2/tests/test_dashboard.py`. Two pure pieces (`period_bounds`, and the session-activities reload) fix the stale/shrinking range; an explicit widget key plus a session-state pop implements the reset. No changes to `store.py`, `session.py`, or any chart-rendering code.

**Tech Stack:** Python 3.11, Streamlit, pytest, ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-09-01-period-selector-design.md`

## Global Constraints

- All commands run from `v2/` using the interpreter at `../.venv/bin/python`.
- Test command: `../.venv/bin/python -m pytest`
- Lint: `../.venv/bin/python -m ruff check .`
- Format check: `../.venv/bin/python -m ruff format --check .`
- Type check: `../.venv/bin/python -m mypy .`
- Ruff line-length 100, quote-style preserve, `E501` ignored. No code comments unless requested.
- TDD: write a failing test first, watch it fail, then implement.
- Do not run the dashboard (Streamlit) or touch the real DB during this plan; `period_bounds` is pure and tested in isolation.

---

### Task 1: `period_bounds` helper + unit tests

**Files:**
- Modify: `v2/dashboard.py` (add `period_bounds` near `compute_fetch_start`, ~line 84)
- Test: `v2/tests/test_dashboard.py` (add tests at end of file)

**Interfaces:**
- Produces: `period_bounds(dates: list[date], window_days: int = DEFAULT_WINDOW_DAYS) -> tuple[date, date, date]` returning `(min_d, since, end)` where `min_d = min(dates)`, `end = max(dates)`, `since = max(min_d, end - timedelta(days=window_days))`. `dates` is non-empty (the selector is only reached when activities exist).

- [ ] **Step 1: Write the failing tests**

Append to `v2/tests/test_dashboard.py`:

```python
from dashboard import DEFAULT_WINDOW_DAYS, period_bounds


def test_period_bounds_min_and_end_from_dates():
    dates = [date(2026, 1, 5), date(2026, 8, 30), date(2026, 3, 10)]
    min_d, since, end = period_bounds(dates)
    assert min_d == date(2026, 1, 5)
    assert end == date(2026, 8, 30)


def test_period_bounds_trailing_window_when_span_exceeds_window():
    dates = [date(2026, 1, 5), date(2026, 8, 30)]
    min_d, since, end = period_bounds(dates)
    assert min_d == date(2026, 1, 5)
    assert since == end - timedelta(days=DEFAULT_WINDOW_DAYS)
    assert end == date(2026, 8, 30)


def test_period_bounds_floored_to_min_when_span_under_window():
    dates = [date(2026, 8, 20), date(2026, 8, 30)]
    min_d, since, end = period_bounds(dates)
    assert min_d == date(2026, 8, 20)
    assert since == date(2026, 8, 20)
    assert end == date(2026, 8, 30)


def test_period_bounds_independent_of_today():
    # Pinning shows the helper never consults the current date.
    dates = [date(2026, 1, 5), date(2026, 8, 30)]
    min_d, since, end = period_bounds(dates)
    assert end == date(2026, 8, 30)
    assert min_d == date(2026, 1, 5)
```

Note: `date` and `timedelta` are already imported at the top of the test file.

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
../.venv/bin/python -m pytest tests/test_dashboard.py::test_period_bounds_min_and_end_from_dates -v
```
Expected: FAIL — `ImportError: cannot import name 'period_bounds'`.

- [ ] **Step 3: Write the minimal implementation**

In `v2/dashboard.py`, after `compute_fetch_start` (ends ~line 84):

```python
def period_bounds(
    dates: list[date], window_days: int = DEFAULT_WINDOW_DAYS
) -> tuple[date, date, date]:
    """(min_d, since, end) for the Period selector.

    min_d/end are the store's earliest/latest activity dates; since is the
    trailing-window default, floored to min_d. Independent of today's date.
    """
    min_d = min(dates)
    end = max(dates)
    since = max(min_d, end - timedelta(days=window_days))
    return min_d, since, end
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
../.venv/bin/python -m pytest tests/test_dashboard.py -v
```
Expected: PASS — all `period_bounds` tests (and existing tests still pass).

- [ ] **Step 5: Commit**

```bash
git add tests/test_dashboard.py dashboard.py
git commit -m "feat(v2): period_bounds helper for store-bound date range"
```

---

### Task 2: Reload full store after refresh + selector uses store bounds + caption + reset

**Files:**
- Modify: `v2/dashboard.py` lines 657-710 (refresh reload, widget, caption) and add `PERIOD_KEY` constant near line 33.

**Interfaces:**
- Consumes: `period_bounds` from Task 1.
- Produces: a session-state key `"period"` that the Streamlit widget uses and that Task 2's reset pops; a `PERIOD_KEY` module constant `"period"`.

- [ ] **Step 1: Add `PERIOD_KEY` constant**

Near the other module constants (line ~33, after `DEFAULT_WINDOW_DAYS`):

```python
PERIOD_KEY = "period"
```

- [ ] **Step 2: Change refresh to reload full store and reset the widget**

Replace lines 667-669 in `v2/dashboard.py`:

```python
            if acts:
                store.save_activities(acts)
                st.session_state["activities"] = store.load_activities()
                st.session_state.pop(PERIOD_KEY, None)
```

(The reset lives inside the `if acts:` block so a no-new-activities refresh — which leaves the selection untouched — does not pop an already-clean key; the `pop` is safe either way because it is a no-op when absent, but keeping it in the block matches the "reset only on growth" intent. Place the `pop` here; the widget is rendered later in the same `main()` run.)

- [ ] **Step 3: Derive bounds from the store and render caption + bounded widget**

Replace lines 701-706 in `v2/dashboard.py`:

```python
    min_d, default_since, max_d = period_bounds([a.date for a in activities])
    period = st.sidebar.date_input(
        "Period",
        value=(default_since, max_d),
        min_value=min_d,
        max_value=max_d,
        key=PERIOD_KEY,
    )
    st.sidebar.caption(f"Store range {min_d} → {max_d} · {len(activities)} activities")
```

Note: the old `default_since`/`max_d` local names are superseded here; `since`/`until` extraction below (lines 707-710) is unchanged and still references `period`.

- [ ] **Step 4: Verify the whole dashboard module and tests**

Run, from `v2/`:
```bash
../.venv/bin/python -m pytest
../.venv/bin/python -m ruff check .
../.venv/bin/python -m ruff format --check .
../.venv/bin/python -m mypy .
```
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add dashboard.py
git commit -m "feat(v2): bind Period selector to store range; caption + reset on refresh"
```

---

## Self-Review

**Spec coverage vs tasks:**
- §3.1 reload on refresh → Task 2 Step 2.
- §3.2 `period_bounds` → Task 1.
- §3.3 widget key + reset + caption → Task 2 Steps 1-3.
- §3.4 charts unchanged → no task needed (no rendering code touched).
- §6 testing (`period_bounds` cases) → Task 1.
- §7 files touched → Tasks 1-2.

**Placeholder scan:** every code step carries concrete implementation. No "TBD"/"add handling".

**Type consistency:** `period_bounds` returns `(date, date, date)` everywhere; session key constant `PERIOD_KEY` used in both the `pop` (Task 2 Step 2) and the widget key (Task 2 Step 3). `since`,`until` extraction branch (line 707-710) is untouched and still compiles against `period`.