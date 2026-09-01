# Period Selector — Store-Bound Date Range — Design

## 1. Goal

The dashboard's **Period** date selector (sidebar) must reflect the actual date
range of data held in the persistent store — not a load-time snapshot, not the
current clock date. The selector's clickable bounds equal the store's earliest
and latest activity dates. A Garmin refresh that adds activities must grow that
range. A caption under the widget shows the maximum date range in the store. The
user can select any period within the range, and the charts filter to that
period (existing behavior, unchanged).

## 2. Background: the 8/30/2026 symptom

The selector appeared "hardcoded" to end at 2026-08-30. Root causes:

1. **Bounds come from a stale, shrinking session list.** `dashboard.py:701-702`
   computes `min_d` / `max_d` from `st.session_state["activities"]`. That list is
   populated from the store only on first load (`:657-661`). On a successful
   refresh it is **replaced** with the newly-fetched incremental slice
   (`dashboard.py:669`, `st.session_state["activities"] = acts`), so:
   - when refresh returns no new activities (`acts == []`), the list — and
     therefore the selector range — never changes;
   - when refresh does return activities, the list shrinks to just those rows,
     and the history range collapses with it.
   Either way the selector does not track the growing store.
2. **No reset path.** Nothing can snap the widget back to a freshly computed
   default after the store grows.

## 3. Design

All production changes are confined to `v2/dashboard.py`; tests in
`v2/tests/test_dashboard.py`. No changes to `store.py`, `session.py`, or any
chart-rendering code.

### 3.1 Refresh reloads the full store

Replace the "session list = fetched slice" assignment with a reload of the
merged history after the save succeeds:

```python
if acts:
    store.save_activities(acts)
    st.session_state["activities"] = store.load_activities()
```

The session activities list becomes the single source of truth for the
selector's range and always equals the store contents. The existing
`activities_sig` (dashboard.py:171) keys the view-compute cache, so a changed
list triggers a recompute while an unchanged one does not.

On a refresh that **fails** (exception in the existing `except` branch), no
save, no reload, and no widget reset happens — the session keeps its current
state.

### 3.2 Pure `period_bounds` helper

```python
def period_bounds(dates, window_days=DEFAULT_WINDOW_DAYS):
    """(min_d, since, end) for the Period selector.
    min_d/end are the store's earliest/latest activity dates; since is the
    trailing-window default, floored to min_d."""
    min_d = min(dates)
    end = max(dates)
    since = max(min_d, end - timedelta(days=window_days))
    return min_d, since, end
```

Mirrors the existing `compute_fetch_start` (dashboard.py:71) pure-helper style.
Independent of `date.today()` — the selector never offers dates with no data
beyond `end`.

### 3.3 Widget: explicit key + reset on successful refresh

```python
PERIOD_KEY = "period"

(min_d, since, end) = period_bounds([a.date for a in activities])
period = st.sidebar.date_input(
    "Period", value=(since, end), min_value=min_d, max_value=end, key=PERIOD_KEY
)
st.sidebar.caption(f"Store range {min_d} → {end} · {len(activities)} activities")
```

- **Explicit key** so the reset can be triggered programmatically.
- **Reset:** inside the successful-refresh branch (before the widget renders
  later in the same `main()` run), call
  `st.session_state.pop(PERIOD_KEY, None)`. The widget re-initializes from
  `value` on the next run, discarding any manual selection — "reset selection on
  every successful refresh" (user decision).
- When the user has not touched the widget and refresh returns nothing new, the
  pop is a no-op: the recomputed default is identical.
- A scalar `period` (user clicked a single date) keeps working via the existing
  `since = until = period` branch (dashboard.py:707-710).

### 3.4 Charts unchanged

Every chart and the activities table already consume `view.windowed(since,
until)` (dashboard.py:729) and `since`/`until` directly. No rendering code
changes.

## 4. Behavior spec (acceptance criteria)

Given a store with earliest date E and latest date L:

1. **On load** the widget shows bounds `[E, L]`; the default selection is
   `[max(E, L − 180d), L]`.
2. **Any period** within `[E, L]` is selectable; KPI cards, tabs, and the
   activities table filter to it.
3. **Successful refresh** that grows the store to L′: bounds widen to `[E, L′]`,
   the selection resets to `[max(E, L′ − 180d), L′]`, and the caption updates.
4. **Successful refresh** with no new activities: bounds/selection unchanged
   (reset is a no-op).
5. **Failed refresh:** nothing changes — bounds, selection, and caption stay as
   they were.
6. **Caption** always shows `Store range E → L · N activities`.

## 5. Edge cases

- **Empty store / first run:** the existing onboarding branch renders and stops
  before the selector is reached (dashboard.py:684-692). No change.
- **Single-day store** (`E == L`): bounds are a single point; behavior is
  pre-existing and unchanged by this spec.
- **Clock skew:** store dates are never compared to `date.today()` in the
  selector path, so future-dated or stale stores render their true range.

## 6. Testing

Unit tests in `v2/tests/test_dashboard.py` for `period_bounds`:

- returns `(min, max)` of the given dates for `min_d` and `end`;
- trailing window: `since == end − 180d` when the store spans more than the
  window;
- floored: `since == min_d` when the store spans less than the window;
- independent of the current date (no `date.today()` dependency).

Widget-key reset and the reload behavior are Streamlit-runtime UI concerns and
are not unit-tested (consistent with the existing dashboard spec §8). The
existing refresh-window tests must stay green; run `pytest`, `ruff check`,
`ruff format --check`, and `mypy` from `v2/`.

## 7. Files touched

- Modified: `v2/dashboard.py`
- Modified: `v2/tests/test_dashboard.py`