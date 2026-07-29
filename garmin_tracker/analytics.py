"""Derived metrics - pace, trend weight, readiness, load, consistency.

Pure functions: every calculation lives here, consumed by both the (future)
web app and the (future) LLM coach. Per the design spec: "the LLM must never
do arithmetic on raw data" - this module is where all of the arithmetic
happens, once.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from scipy.stats import theilslopes

from garmin_tracker import db


def _parse_date(d) -> date:
    if isinstance(d, date):
        return d
    return datetime.strptime(str(d), "%Y-%m-%d").date()


# ---- Generic pace math -------------------------------------------------

def pace_status(target: float, actual: float, elapsed_period: float,
                total_period: float, remaining_units: Optional[float] = None) -> dict:
    """target/actual over a period (e.g. sessions this month). Returns the
    expected-by-now value, delta, and the rate still required to hit target."""
    if total_period <= 0:
        return {"target": target, "actual": actual, "expected_by_now": None,
                "delta": None, "required_rate": None, "on_pace": None}

    expected_by_now = target * (elapsed_period / total_period)
    delta = actual - expected_by_now

    if remaining_units is None:
        remaining_units = max(total_period - elapsed_period, 0)
    remaining_target = target - actual
    if remaining_units > 0:
        required_rate = remaining_target / remaining_units
    else:
        required_rate = 0.0 if remaining_target <= 0 else float("inf")

    return {
        "target": target,
        "actual": actual,
        "expected_by_now": round(expected_by_now, 2),
        "delta": round(delta, 2),
        "required_rate": round(required_rate, 2) if required_rate not in (float("inf"),) else None,
        "on_pace": delta >= 0,
    }


def _count_activities(conn, start: date, end: date, bucket: str) -> int:
    rows = db.fetch_all_dicts(
        conn,
        "SELECT COUNT(*) as n FROM activities WHERE date >= ? AND date <= ? AND bucket = ?",
        (start.isoformat(), end.isoformat(), bucket),
    )
    return rows[0]["n"] if rows else 0


def _activity_dates(conn, start: date, end: date, bucket: str) -> list[date]:
    rows = db.fetch_all_dicts(
        conn,
        "SELECT date FROM activities WHERE date >= ? AND date <= ? AND bucket = ?",
        (start.isoformat(), end.isoformat(), bucket),
    )
    return sorted(_parse_date(r["date"]) for r in rows if r["date"])


def strength_pace(conn, goals: dict, today: Optional[date] = None) -> dict:
    today = today or date.today()
    month_start = today.replace(day=1)
    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    month_end = next_month - timedelta(days=1)

    total_period = (next_month - month_start).days
    elapsed_period = (today - month_start).days + 1
    remaining_units = (month_end - today).days

    actual = _count_activities(conn, month_start, today, "strength")
    target = goals["strength"]["monthly_sessions"]

    result = pace_status(target, actual, elapsed_period, total_period, remaining_units)
    result.update({"period": "month", "period_start": month_start.isoformat(),
                   "period_end": month_end.isoformat()})
    return result


def racquet_pace(conn, goals: dict, today: Optional[date] = None) -> dict:
    today = today or date.today()
    week_start = today - timedelta(days=today.weekday())  # Monday
    week_end = week_start + timedelta(days=6)

    total_period = 7
    elapsed_period = (today - week_start).days + 1
    remaining_units = (week_end - today).days

    actual = _count_activities(conn, week_start, today, "racquet")
    target = goals["racquet"]["weekly_sessions"]

    result = pace_status(target, actual, elapsed_period, total_period, remaining_units)
    result.update({"period": "week", "period_start": week_start.isoformat(),
                   "period_end": week_end.isoformat()})
    return result


def steps_pace(conn, goals: dict, today: Optional[date] = None) -> dict:
    """Daily step pace. NOTE: intraday (hour-of-day) pacing from the spec
    ("expected_by_now scales against the user's own learned hourly
    distribution") needs Garmin's intraday steps endpoint, which isn't
    synced yet (only a daily total is stored in daily_metrics). Falls back
    to a flat fraction-of-day-elapsed estimate until that's wired up."""
    today = today or date.today()
    row = db.fetch_all_dicts(conn, "SELECT steps FROM daily_metrics WHERE date = ?", (today.isoformat(),))
    actual = row[0]["steps"] if row and row[0]["steps"] is not None else 0
    target = goals["steps"]["daily_target"]

    now = datetime.now()
    fraction_of_day = (now.hour * 60 + now.minute) / (24 * 60)
    result = pace_status(target, actual, fraction_of_day, 1.0, remaining_units=(1.0 - fraction_of_day))
    result["method"] = "flat_fraction_of_day (intraday learned curve not yet available)"
    return result


# ---- Trend weight --------------------------------------------------------

def _ewma(values: list[float], span: int = 7) -> list[float]:
    alpha = 2 / (span + 1)
    out = []
    prev = None
    for v in values:
        prev = v if prev is None else alpha * v + (1 - alpha) * prev
        out.append(prev)
    return out


def next_checkpoint(checkpoints: list[dict], today: Optional[date] = None) -> Optional[dict]:
    today = today or date.today()
    upcoming = [c for c in checkpoints if _parse_date(c["date"]) >= today]
    if not upcoming:
        return None
    return min(upcoming, key=lambda c: _parse_date(c["date"]))


def trend_weight(conn, goals: dict, today: Optional[date] = None) -> dict:
    """7-day EWMA as the primary number (raw points kept only for faint-dot
    display). Rate and projection use Theil-Sen (median-of-slopes) rather
    than ordinary least squares - robust to the single-outlier weigh-ins a
    lone subject produces - over the trailing 21 days, with a 90% CI on the
    slope propagated into an earliest/latest projected-checkpoint-date band
    rather than a single false-precision date."""
    today = today or date.today()
    start = today - timedelta(days=180)  # plenty of history for a 7-day EWMA + 21-day slope
    rows = db.fetch_all_dicts(
        conn,
        "SELECT date, weight_lb FROM daily_metrics WHERE date >= ? AND date <= ? ORDER BY date",
        (start.isoformat(), today.isoformat()),
    )
    valid = [(r["date"], r["weight_lb"]) for r in rows if r["weight_lb"] is not None]

    if not valid:
        return {"trend_weight_lb": None, "rate_lb_per_week": None,
                "rate_lb_per_week_ci": None,
                "projected_checkpoint_date_range": None, "checkpoint": None,
                "required_lb_per_week": None, "raw_points": []}

    dates = [d for d, _ in valid]
    ewma_series = _ewma([w for _, w in valid])
    current_trend = round(ewma_series[-1], 2)

    # 21-day trailing Theil-Sen slope over the trend series
    cutoff = today - timedelta(days=21)
    trailing = [(d, e) for d, e in zip(dates, ewma_series) if _parse_date(d) >= cutoff]
    rate_per_week = None
    rate_ci = None
    if len(trailing) >= 4:
        xs = [(_parse_date(d) - _parse_date(trailing[0][0])).days for d, _ in trailing]
        ys = [e for _, e in trailing]
        slope, intercept, low_slope, high_slope = theilslopes(ys, xs, alpha=0.90)
        rate_per_week = round(slope * 7, 3)
        rate_ci = (round(low_slope * 7, 3), round(high_slope * 7, 3))

    checkpoint = next_checkpoint(goals["weight"]["checkpoints"], today)
    projected_range = None
    required_rate = None
    if checkpoint:
        target = checkpoint["target"]
        checkpoint_date = _parse_date(checkpoint["date"])
        weeks_to_checkpoint = max((checkpoint_date - today).days / 7.0, 0)
        if weeks_to_checkpoint > 0:
            required_rate = round((target - current_trend) / weeks_to_checkpoint, 3)

        if rate_ci is not None:
            # Faster of the two CI bounds -> earliest arrival; slower -> latest
            # (or "never" if that bound doesn't move toward the target at all).
            candidates = []
            for r in rate_ci:
                if r != 0 and ((target - current_trend) / r) > 0:
                    weeks_needed = (target - current_trend) / r
                    candidates.append(today + timedelta(weeks=weeks_needed))
            if candidates:
                projected_range = (min(candidates).isoformat(), max(candidates).isoformat())

    return {
        "trend_weight_lb": current_trend,
        "rate_lb_per_week": rate_per_week,
        "rate_lb_per_week_ci": rate_ci,
        "checkpoint": checkpoint,
        "projected_checkpoint_date_range": projected_range,
        "required_lb_per_week": required_rate,
        "raw_points": [{"date": d, "weight_lb": w} for d, w in valid[-30:]],
    }


# ---- Consistency / burst detection --------------------------------------

def front_load_index(conn, bucket: str = "strength", today: Optional[date] = None) -> Optional[float]:
    today = today or date.today()
    month_start = today.replace(day=1)
    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    month_end = next_month - timedelta(days=1)
    midpoint = month_start + (month_end - month_start) / 2

    sessions = _activity_dates(conn, month_start, today, bucket)
    if not sessions:
        return None
    first_half = [d for d in sessions if d <= midpoint]
    return round(len(first_half) / len(sessions), 2)


def burst_pattern(conn, bucket: str = "strength", weeks: int = 6,
                   today: Optional[date] = None) -> dict:
    today = today or date.today()
    start = today - timedelta(weeks=weeks)
    sessions = _activity_dates(conn, start, today, bucket)

    weekly_counts = []
    cursor = today - timedelta(days=today.weekday())  # this week's Monday
    for _ in range(weeks):
        week_start = cursor
        week_end = cursor + timedelta(days=6)
        count = sum(1 for d in sessions if week_start <= d <= week_end)
        weekly_counts.append(count)
        cursor -= timedelta(days=7)
    weekly_counts.reverse()  # oldest first

    burst_flag = any(
        weekly_counts[i] >= 3 and weekly_counts[i + 1] == 0
        for i in range(len(weekly_counts) - 1)
    )
    gap_since_last = (today - sessions[-1]).days if sessions else None

    return {"weekly_counts": weekly_counts, "burst_then_zero_flag": burst_flag,
            "days_since_last_session": gap_since_last}


# ---- Load / injury risk (homegrown HR-zone proxy) ------------------------

def _sum_load(conn, start: date, end: date) -> float:
    rows = db.fetch_all_dicts(
        conn,
        "SELECT training_load FROM activities WHERE date >= ? AND date <= ?",
        (start.isoformat(), end.isoformat()),
    )
    return round(sum(r["training_load"] for r in rows if r["training_load"] is not None), 1)


def acute_chronic_load_ratio(conn, today: Optional[date] = None) -> dict:
    today = today or date.today()
    acute = _sum_load(conn, today - timedelta(days=6), today)
    chronic_total = _sum_load(conn, today - timedelta(days=27), today)
    chronic_weekly_avg = round(chronic_total / 4, 1) if chronic_total else 0.0
    ratio = round(acute / chronic_weekly_avg, 2) if chronic_weekly_avg else None
    return {
        "acute_7d_load": acute,
        "chronic_28d_weekly_avg_load": chronic_weekly_avg,
        "ratio": ratio,
        "flag_high": ratio is not None and ratio > 1.5,
        "method": "homegrown HR-zone-minutes proxy (Garmin's own trainingLoad "
                  "is unavailable for this account)",
    }


def _sum_duration_minutes(conn, start: date, end: date, bucket: str) -> float:
    rows = db.fetch_all_dicts(
        conn,
        "SELECT duration_min FROM activities WHERE date >= ? AND date <= ? AND bucket = ?",
        (start.isoformat(), end.isoformat(), bucket),
    )
    return round(sum(r["duration_min"] or 0 for r in rows), 1)


def racquet_minutes_jump(conn, today: Optional[date] = None) -> dict:
    today = today or date.today()
    week_start = today - timedelta(days=today.weekday())
    prev_week_start = week_start - timedelta(days=7)
    prev_week_end = week_start - timedelta(days=1)

    this_week = _sum_duration_minutes(conn, week_start, today, "racquet")
    last_week = _sum_duration_minutes(conn, prev_week_start, prev_week_end, "racquet")
    pct_change = round((this_week - last_week) / last_week, 2) if last_week else None

    return {
        "this_week_minutes": this_week,
        "last_week_minutes": last_week,
        "pct_change": pct_change,
        "flag_jump": pct_change is not None and pct_change > 0.40,
    }


# ---- Readiness gate -------------------------------------------------------

def readiness(body_battery_wake: Optional[int], hrv_status: Optional[str],
              sleep_score: Optional[int], resting_hr: Optional[int],
              baseline_resting_hr_30d: Optional[float]) -> dict:
    """Simple, transparent rule-based classification - green/amber/red -
    deliberately not a black-box model, so the reasoning is always visible."""
    red = amber = 0
    reasons = []

    if hrv_status in ("LOW", "UNBALANCED"):
        red += 1
        reasons.append(f"hrv_status={hrv_status}")

    if sleep_score is not None:
        if sleep_score < 60:
            red += 1
            reasons.append(f"sleep_score={sleep_score} (<60)")
        elif sleep_score < 75:
            amber += 1
            reasons.append(f"sleep_score={sleep_score} (<75)")

    if body_battery_wake is not None:
        if body_battery_wake < 30:
            red += 1
            reasons.append(f"body_battery_wake={body_battery_wake} (<30)")
        elif body_battery_wake < 50:
            amber += 1
            reasons.append(f"body_battery_wake={body_battery_wake} (<50)")

    if resting_hr is not None and baseline_resting_hr_30d is not None:
        diff = resting_hr - baseline_resting_hr_30d
        if diff >= 5:
            red += 1
            reasons.append(f"resting_hr {diff:+.1f} vs 30d baseline")
        elif diff >= 3:
            amber += 1
            reasons.append(f"resting_hr {diff:+.1f} vs 30d baseline")

    if red >= 1:
        state = "red"
    elif amber >= 1:
        state = "amber"
    else:
        state = "green"

    return {"state": state, "reasons": reasons}


def readiness_today(conn, today: Optional[date] = None) -> dict:
    today = today or date.today()
    row = db.fetch_all_dicts(
        conn, "SELECT * FROM daily_metrics WHERE date = ?", (today.isoformat(),)
    )
    row = row[0] if row else {}

    baseline_start = today - timedelta(days=30)
    baseline_rows = db.fetch_all_dicts(
        conn,
        "SELECT resting_hr FROM daily_metrics WHERE date >= ? AND date < ? AND resting_hr IS NOT NULL",
        (baseline_start.isoformat(), today.isoformat()),
    )
    baseline = (
        sum(r["resting_hr"] for r in baseline_rows) / len(baseline_rows)
        if baseline_rows else None
    )

    result = readiness(
        row.get("body_battery_wake"),  # real wake-time reading now (nearest intraday
                                        # bodyBatteryValuesArray sample to sleep-end
                                        # timestamp) - verified within ~10 min of actual
                                        # wake against real data, not a day-min/max proxy.
        row.get("hrv_status"),
        row.get("sleep_score"),
        row.get("resting_hr"),
        baseline,
    )
    result["baseline_resting_hr_30d"] = round(baseline, 1) if baseline else None
    return result


# ---- Today screen helpers -------------------------------------------------

def _trailing_avg(conn, column: str, end_exclusive: date, days: int = 7) -> Optional[float]:
    start = end_exclusive - timedelta(days=days)
    rows = db.fetch_all_dicts(
        conn,
        f"SELECT {column} as v FROM daily_metrics WHERE date >= ? AND date < ? AND {column} IS NOT NULL",
        (start.isoformat(), end_exclusive.isoformat()),
    )
    if not rows:
        return None
    return round(sum(r["v"] for r in rows) / len(rows), 1)


def yesterday_summary(conn, today: Optional[date] = None) -> dict:
    """Steps, activity, calories, sleep - each vs. its trailing 7-day average,
    per spec §7.1.4. ("Activity" has no average - it's just what happened,
    if anything.)"""
    today = today or date.today()
    yesterday = today - timedelta(days=1)
    rows = db.fetch_all_dicts(conn, "SELECT * FROM daily_metrics WHERE date = ?", (yesterday.isoformat(),))
    row = rows[0] if rows else {}

    sleep_hours = round(row["sleep_minutes"] / 60.0, 1) if row.get("sleep_minutes") is not None else None
    sleep_avg_minutes = _trailing_avg(conn, "sleep_minutes", yesterday)
    sleep_avg_hours = round(sleep_avg_minutes / 60.0, 1) if sleep_avg_minutes is not None else None

    activity_rows = db.fetch_all_dicts(
        conn,
        "SELECT activity_type, name, duration_min FROM activities WHERE date = ? ORDER BY start_time",
        (yesterday.isoformat(),),
    )

    return {
        "date": yesterday.isoformat(),
        "steps": row.get("steps"),
        "steps_avg_7d": _trailing_avg(conn, "steps", yesterday),
        "activities": [
            {"type": a["activity_type"], "name": a["name"], "duration_min": a["duration_min"]}
            for a in activity_rows
        ],
        "active_calories": row.get("active_calories"),
        "active_calories_avg_7d": _trailing_avg(conn, "active_calories", yesterday),
        "sleep_hours": sleep_hours,
        "sleep_hours_avg_7d": sleep_avg_hours,
    }


# ---- Full snapshot (the "debug JSON dump" for verification) --------------

def build_snapshot(conn, goals: dict, today: Optional[date] = None) -> dict:
    today = today or date.today()
    return {
        "date": today.isoformat(),
        "steps_pace": steps_pace(conn, goals, today),
        "strength_pace": strength_pace(conn, goals, today),
        "racquet_pace": racquet_pace(conn, goals, today),
        "trend_weight": trend_weight(conn, goals, today),
        "front_load_index": front_load_index(conn, today=today),
        "burst_pattern": burst_pattern(conn, today=today),
        "acute_chronic_load_ratio": acute_chronic_load_ratio(conn, today),
        "racquet_minutes_jump": racquet_minutes_jump(conn, today),
        "readiness": readiness_today(conn, today),
        "yesterday": yesterday_summary(conn, today),
    }
