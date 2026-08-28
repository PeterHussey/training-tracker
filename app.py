"""Streamlit training tracker UI."""
import streamlit as st, plotly.graph_objects as go, pandas as pd, numpy as np
from pathlib import Path
from metrics import synthetic_trimp, acwr, banister_ctl_atl_tsb, load_raw, daily_trimp_from_activities, parse_activities_from_raw
from garmin_client import GarminClient

st.set_page_config(page_title="Training Tracker", layout="wide")
st.title("Training Tracker — Garmin Connect Metrics")

# --- Instructions ---
with st.expander("How to run / refresh", expanded=True):
    st.markdown("""
```bash
. .venv/bin/activate
streamlit run app.py
```
Refresh uses `garmin_client.py` (persistent stdio JSON-RPC to `garmin-connect-mcp`).
Live auth: `GARMIN_EMAIL=YOUR_EMAIL@example.com` (from `~/.garmin-mcp/`).
Test window: **2/28/26** set below. Click Refresh → writes `cache/garmin_raw_*.json`.
Fitness Trend reads `get_vo2max` / `get_lactate_threshold` when available.
""")

# Compute date defaults from cache before sidebar
raw_tmp = load_raw()
acts_tmp = parse_activities_from_raw(raw_tmp) if isinstance(raw_tmp, (dict, list)) else []
if isinstance(acts_tmp, list) and acts_tmp:
    dlist = sorted([str(a.get("startTimeLocal",""))[:10] for a in acts_tmp if a.get("startTimeLocal")])
    if dlist:
        default_start = pd.to_datetime(dlist[0])
        default_end = pd.to_datetime(dlist[-1])
    else:
        default_start = pd.to_datetime("2026-02-28"); default_end = pd.to_datetime("2026-08-28")
else:
    default_start = pd.to_datetime("2026-02-28"); default_end = pd.to_datetime("2026-08-28")

# --- Sidebar controls ---
with st.sidebar:
    st.header("Controls (test: 2/28/26)")
    start = st.date_input("Start date", value=default_start)
    end = st.date_input("End date", value=default_end)
    refresh = st.button("Refresh Garmin data")
    gc = GarminClient()
    refresh_result = None
    if refresh:
        with st.spinner("Calling Garmin MCP (get_activities)..."):
            refresh_result = gc.fetch_activities(str(start), str(end))
        if isinstance(refresh_result, dict) and (refresh_result.get("status") == "no_mcp_server" or refresh_result.get("method") is None):
            from garmin_client import fetch_activities_live
            refresh_result = fetch_activities_live(str(start), str(end))
        st.success("Refresh complete")
    # Also try fitness-trend endpoints
    with st.expander("Live Garmin endpoints"):
        if st.button("Fetch VO2max"):
            st.json(gc.get_vo2max())
        if st.button("Fetch Lactate Threshold"):
            st.json(gc.get_lactate_threshold())

    ts_path = Path("cache/last_fetch_timestamp")
    refresh_key = ts_path.read_text()[:16] if ts_path.exists() else "none"
    if ts_path.exists():
        st.caption(f"Last fetch: {ts_path.read_text()[:19]}")
    else:
        st.caption("No fetch yet")

# --- Metrics cards (synthetic base + live fallback) ---
raw = load_raw()
vo2max_data = raw.get("get_vo2max_result") or raw.get("vo2max") if isinstance(raw, dict) else None
threshold_data = raw.get("get_lactate_threshold_result") or raw.get("lactate_threshold") if isinstance(raw, dict) else None

# Try live fetch by default for demo
vm = gc.get_vo2max()
lt = gc.get_lactate_threshold()

raw = load_raw()
# Derive date range from cached activities when available
acts = parse_activities_from_raw(raw) if isinstance(raw, (dict, list)) else []
if isinstance(acts, list) and acts:
    dates_list = sorted([a.get("startTimeLocal","")[:10] for a in acts if a.get("startTimeLocal") and isinstance(a.get("startTimeLocal"), str)])
    if dates_list:
        min_date = pd.to_datetime(dates_list[0])
        max_date = pd.to_datetime(dates_list[-1])
        default_start = min_date
        default_end = max_date
    else:
        default_start = pd.to_datetime("2026-02-28")
        default_end = pd.to_datetime("2026-08-28")
else:
    default_start = pd.to_datetime("2026-02-28")
    default_end = pd.to_datetime("2026-08-28")

series_real = daily_trimp_from_activities(raw)
if series_real and sum(series_real) > 0:
    series = series_real
    source_label = "Live Garmin (cached)"
    # Use actual dates from activities for x-axis
    acts_for_dates = parse_activities_from_raw(raw)
    date_strs = sorted({a.get("startTimeLocal","")[:10] for a in acts_for_dates if isinstance(a.get("startTimeLocal"), str) and a.get("startTimeLocal","")})
    if date_strs:
        dates = pd.to_datetime(date_strs)
    else:
        dates = pd.date_range(default_start, periods=len(series), freq="D")
else:
    series = synthetic_trimp(30)
    source_label = "Synthetic (no live Garmin data)"
    dates = pd.date_range(default_start, periods=30, freq="D")

ctl_f, atl_f, tsb_v, _, _, tsb_full = banister_ctl_atl_tsb(series)
acwr_v = acwr(sum(series[-7:]), sum(series[-28:] if len(series) >= 28 else series))

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("ACWR", f"{acwr_v:.2f}")
col2.metric("CTL", f"{ctl_f:.0f}")
col3.metric("ATL", f"{atl_f:.0f}")
col4.metric("TSB", f"{tsb_v:.0f}")
col5.metric("Test window", "2/28/26")

# --- Refresh result display ---
if refresh_result is not None:
    st.subheader("Refresh result (2/28/26)")
    st.json(refresh_result)

# --- Tabs ---
tab1, tab2, tab3, tab4 = st.tabs(["Training Load", "Fitness Trend", "Efficiency", "Volume"])

with tab1:
    st.subheader("TRIMP & ACWR — " + source_label)
    df_trimp = pd.DataFrame({"date": dates, "TRIMP": series})
    acwr_series = [acwr(sum(series[max(0,i-7):i]), sum(series[max(0,i-28):i])) for i in range(1, len(series)+1)]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df_trimp["date"], y=df_trimp["TRIMP"], mode="lines+markers", name="Daily TRIMP"))
    fig.add_trace(go.Scatter(x=df_trimp["date"], y=acwr_series, mode="lines", name="ACWR", yaxis="y2"))
    fig.update_layout(yaxis2=dict(overlaying="y", side="right", range=[0, 2]), title="TRIMP / ACWR — synthetic 30-day")
    st.plotly_chart(fig, use_container_width=True, key=f"load_{refresh_key}")
    st.caption("ACWR green 0.8–1.3; yellow outside; red >1.5. TSB positive = form.")

with tab2:
    st.subheader("Fitness Trend (VO2max / Threshold)")
    # Show live endpoint results when available; fall back to synthetic note
    c1, c2 = st.columns(2)
    with c1:
        st.write("**get_vo2max**")
        if isinstance(vm, dict) and "result" in vm:
            st.json(vm)
        elif isinstance(vm, dict) and "error" in vm:
            st.warning(f"VO2max endpoint returned: {vm.get('error','no response')}")
        else:
            st.info("Live `get_vo2max` integrated here. Returns Garmin VO2max estimate when auth active.")
            st.code(str(vm)[:500], language="json")
    with c2:
        st.write("**get_lactate_threshold**")
        if isinstance(lt, dict) and "result" in lt:
            st.json(lt)
        elif isinstance(lt, dict) and "error" in lt:
            st.warning(f"Threshold endpoint returned: {lt.get('error','no response')}")
        else:
            st.info("Live `get_lactate_threshold` integrated here. Returns HR & pace threshold.")
            st.code(str(lt)[:500], language="json")

with tab3:
    st.subheader("Efficiency")
    st.info("Aerobic decoupling + efficiency factor require `get_activity_details` time-series. Not yet triggered; refresh writes activities to cache for next load.")

with tab4:
    st.subheader("Volume")
    rolling_7 = pd.Series(series).rolling(7).sum()
    rolling_28 = pd.Series(series).rolling(28).sum()
    df_vol = pd.DataFrame({"date": dates, "7-day": rolling_7.values, "28-day": rolling_28.values})
    fig_vol = go.Figure()
    fig_vol.add_trace(go.Scatter(x=df_vol["date"], y=df_vol["7-day"], name="7-day rolling"))
    fig_vol.add_trace(go.Scatter(x=df_vol["date"], y=df_vol["28-day"], name="28-day rolling"))
    fig_vol.update_layout(title="Rolling mileage / TRIMP — synthetic 30-day")
    st.plotly_chart(fig_vol, use_container_width=True, key=f"vol_{refresh_key}")
