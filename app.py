"""Streamlit training tracker UI."""
import streamlit as st
import plotly.graph_objects as go
import pandas as pd
import numpy as np
from pathlib import Path
from metrics import synthetic_trimp, acwr, banister_ctl_atl_tsb, load_raw

st.set_page_config(page_title="Training Tracker", layout="wide")

st.title("Training Tracker — Garmin Connect Metrics")

with st.sidebar:
    st.header("Controls")
    start = st.date_input("Start date")
    end = st.date_input("End date")
    refresh = st.button("Refresh Garmin data")
    ts_path = Path("cache/last_fetch_timestamp")
    if ts_path.exists():
        st.caption(f"Last fetch: {ts_path.read_text()[:19]}")
    else:
        st.caption("No fetch yet (cached data only)")

# Synthetic data for demonstration / verification
series = synthetic_trimp(30)
dates = pd.date_range("2026-07-29", periods=30, freq="D")

# Metrics cards
ctl_f, atl_f, tsb_v, _, _, tsb_full = banister_ctl_atl_tsb(series)
acwr_v = acwr(sum(series[-7:]), sum(series[-28:] if len(series) >= 28 else series))

col1, col2, col3, col4 = st.columns(4)
col1.metric("ACWR", f"{acwr_v:.2f}", delta_color="inverse")
col2.metric("CTL", f"{ctl_f:.0f}")
col3.metric("ATL", f"{atl_f:.0f}")
col4.metric("TSB", f"{tsb_v:.0f}", delta_color="inverse")

tab1, tab2, tab3, tab4 = st.tabs(["Training Load", "Fitness Trend", "Efficiency", "Volume"])

with tab1:
    st.subheader("TRIMP & ACWR")
    df_trimp = pd.DataFrame({"date": dates, "TRIMP": series})
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df_trimp["date"], y=df_trimp["TRIMP"], mode="lines+markers", name="Daily TRIMP"))
    # ACWR overlay
    acwr_series = [acwr(sum(series[max(0,i-7):i]), sum(series[max(0,i-28):i])) for i in range(1, len(series)+1)]
    fig.add_trace(go.Scatter(x=df_trimp["date"], y=acwr_series, mode="lines", name="ACWR", yaxis="y2"))
    fig.update_layout(yaxis2=dict(overlaying="y", side="right", range=[0, 2]))
    st.plotly_chart(fig, use_container_width=True)
    st.write("Green ACWR 0.8–1.3; yellow outside; red >1.5. TSB positive = form.")

with tab2:
    st.subheader("Fitness Trend (Synthetic VO2max / Threshold)")
    # Placeholder for real Garmin data
    st.info("Integrate get_vo2max / get_lactate_threshold from garmin_client when live auth available.")

with tab3:
    st.subheader("Efficiency")
    st.info("Aerobic decoupling + efficiency factor require get_activity_details time-series.")

with tab4:
    st.subheader("Volume")
    rolling_7 = pd.Series(series).rolling(7).sum()
    rolling_28 = pd.Series(series).rolling(28).sum()
    df_vol = pd.DataFrame({"date": dates, "7-day": rolling_7.values, "28-day": rolling_28.values})
    st.plotly_chart(go.Figure().add_trace(go.Scatter(x=df_vol["date"], y=df_vol["7-day"], name="7-day rolling")).add_trace(go.Scatter(x=df_vol["date"], y=df_vol["28-day"], name="28-day rolling")), use_container_width=True)
