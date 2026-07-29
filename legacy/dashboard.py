"""Streamlit dashboard for locally-synced Garmin Connect data.

Run with: streamlit run dashboard.py
"""
from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from garmin_tracker import config, db

# Fixed categorical order (never cycled) - see dataviz skill palette.md
CATEGORICAL_COLORS = [
    "#2a78d6",  # blue
    "#008300",  # green
    "#e87ba4",  # magenta
    "#eda100",  # yellow
    "#1baf7a",  # aqua
    "#eb6834",  # orange
    "#4a3aa7",  # violet
    "#e34948",  # red
]
SEQUENTIAL_BLUE = ["#cde2fb", "#86b6ef", "#3987e5", "#256abf", "#104281"]

st.set_page_config(page_title="Garmin Health Tracker", layout="wide")

if config.DASHBOARD_PASSWORD:
    if not st.session_state.get("authed"):
        pw = st.text_input("Password", type="password")
        if pw == config.DASHBOARD_PASSWORD:
            st.session_state.authed = True
            st.rerun()
        if pw:
            st.error("Incorrect password")
        st.stop()


@st.cache_data(ttl=300)
def load_table(table: str) -> pd.DataFrame:
    conn = db.get_connection()
    try:
        rows = db.fetch_all_dicts(conn, f"SELECT * FROM {table}")
        df = pd.DataFrame(rows)
    except Exception:
        df = pd.DataFrame()
    conn.close()
    return df


def parse_dates(df: pd.DataFrame, col: str) -> pd.DataFrame:
    if col in df.columns:
        df[col] = pd.to_datetime(df[col], errors="coerce")
    return df


daily_stats = parse_dates(load_table("daily_stats"), "date")
sleep = parse_dates(load_table("sleep"), "date")
activities = parse_dates(load_table("activities"), "start_time")
training_status = parse_dates(load_table("training_status"), "date")

st.title("Garmin Health Tracker")

if daily_stats.empty and activities.empty:
    st.warning(
        "No data found yet. Run `python -m garmin_tracker.sync --full` to pull "
        "your history, then refresh this page."
    )
    st.stop()

# ---- Date range filter -------------------------------------------------
min_date = date.today() - timedelta(days=365)
if not daily_stats.empty:
    min_date = daily_stats["date"].min().date()

range_choice = st.radio(
    "Time range", ["Last 30 days", "Last 90 days", "Last 12 months", "All time"],
    horizontal=True, index=2,
)
range_days = {"Last 30 days": 30, "Last 90 days": 90, "Last 12 months": 365}.get(range_choice)
window_start = date.today() - timedelta(days=range_days) if range_days else min_date
window_start_ts = pd.Timestamp(window_start)

ds = daily_stats[daily_stats["date"] >= window_start_ts] if not daily_stats.empty else daily_stats
sl = sleep[sleep["date"] >= window_start_ts] if not sleep.empty else sleep
act = activities[activities["start_time"] >= window_start_ts] if not activities.empty else activities

# ---- Summary cards -------------------------------------------------------
st.subheader("Summary")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Workouts", int(len(act)) if not act.empty else 0)
c2.metric("Avg sleep score", f"{sl['sleep_score'].mean():.0f}" if not sl.empty and sl["sleep_score"].notna().any() else "—")
c3.metric("Avg stress", f"{ds['stress_avg'].mean():.0f}" if not ds.empty and ds["stress_avg"].notna().any() else "—")
c4.metric("Avg resting HR", f"{ds['resting_hr'].mean():.0f}" if not ds.empty and ds["resting_hr"].notna().any() else "—")

st.divider()

# ---- Workout frequency by activity type ----------------------------------
st.subheader("Workout frequency by activity type")
if not act.empty:
    act["week"] = act["start_time"].dt.to_period("W").apply(lambda p: p.start_time)
    freq = act.groupby(["week", "activity_type"]).size().reset_index(name="count")
    types = sorted(freq["activity_type"].dropna().unique())
    color_map = {t: CATEGORICAL_COLORS[i % len(CATEGORICAL_COLORS)] for i, t in enumerate(types)}
    fig = px.bar(
        freq, x="week", y="count", color="activity_type",
        color_discrete_map=color_map,
        labels={"week": "Week", "count": "Workouts", "activity_type": "Type"},
    )
    fig.update_layout(barmode="stack", legend_title_text="Activity type")
    st.plotly_chart(fig, width='stretch')
else:
    st.info("No activities in this range.")

# ---- Trend lines: resting HR & sleep score -------------------------------
st.subheader("Resting HR & sleep score trends")
trend_col1, trend_col2 = st.columns(2)

with trend_col1:
    if not ds.empty and ds["resting_hr"].notna().any():
        fig = px.line(ds.sort_values("date"), x="date", y="resting_hr",
                       labels={"date": "Date", "resting_hr": "Resting HR (bpm)"})
        fig.update_traces(line_color=CATEGORICAL_COLORS[0], line_width=2)
        st.plotly_chart(fig, width='stretch')
    else:
        st.info("No resting HR data in this range.")

with trend_col2:
    if not sl.empty and sl["sleep_score"].notna().any():
        fig = px.line(sl.sort_values("date"), x="date", y="sleep_score",
                       labels={"date": "Date", "sleep_score": "Sleep score"})
        fig.update_traces(line_color=CATEGORICAL_COLORS[4], line_width=2)
        st.plotly_chart(fig, width='stretch')
    else:
        st.info("No sleep score data in this range.")

st.subheader("Stress trend")
if not ds.empty and ds["stress_avg"].notna().any():
    fig = px.line(ds.sort_values("date"), x="date", y="stress_avg",
                   labels={"date": "Date", "stress_avg": "Avg stress"})
    fig.update_traces(line_color=CATEGORICAL_COLORS[5], line_width=2)
    st.plotly_chart(fig, width='stretch')
else:
    st.info("No stress data in this range.")

st.caption(
    "Note: Garmin's public API does not expose true HRV for most consumer "
    "accounts, so HRV is not shown here — resting HR, sleep score, and stress "
    "are used as the closest available recovery signals."
)

st.divider()

# ---- Calendar heatmap of workout days ------------------------------------
st.subheader("Workout calendar")
if not act.empty:
    daily_counts = act.groupby(act["start_time"].dt.date).size()
    idx = pd.date_range(window_start, date.today())
    daily_counts = daily_counts.reindex(idx.date, fill_value=0)

    df_cal = pd.DataFrame({"date": idx})
    df_cal["count"] = daily_counts.values
    df_cal["week"] = df_cal["date"].dt.isocalendar().week.astype(int)
    df_cal["year"] = df_cal["date"].dt.isocalendar().year.astype(int)
    df_cal["yw"] = df_cal["year"].astype(str) + "-W" + df_cal["week"].astype(str).str.zfill(2)
    df_cal["weekday"] = df_cal["date"].dt.day_name().str[:3]

    weekday_order = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    pivot = df_cal.pivot_table(index="weekday", columns="yw", values="count", aggfunc="sum")
    pivot = pivot.reindex(weekday_order)

    fig = go.Figure(
        data=go.Heatmap(
            z=pivot.values,
            x=pivot.columns,
            y=pivot.index,
            colorscale=[[i / (len(SEQUENTIAL_BLUE) - 1), c] for i, c in enumerate(SEQUENTIAL_BLUE)],
            hovertemplate="Week %{x}<br>%{y}: %{z} workout(s)<extra></extra>",
            showscale=True,
            colorbar=dict(title="Workouts"),
        )
    )
    fig.update_layout(xaxis_visible=False, height=260, margin=dict(t=10, b=10))
    st.plotly_chart(fig, width='stretch')
else:
    st.info("No activities in this range.")

st.divider()

with st.expander("Raw activities table"):
    if not act.empty:
        st.dataframe(act.sort_values("start_time", ascending=False), width='stretch')
    else:
        st.write("No activities to show.")
