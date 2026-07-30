"""Derived metrics - pace, trend weight, readiness, load, consistency.

Pure functions: every calculation lives here, consumed by both the (future)
web app and the (future) LLM coach. Per the design spec: "the LLM must never
do arithmetic on raw data" - this module is where all of the arithmetic
happens, once.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

from scipy.stats import theilslopes

from garmin_tracker import config, db


def _parse_date(d) -> date:
    if isinstance(d, date):
        return d
    return datetime.strptime(str(d), "%Y-%m-%d").date()


# ---- Generic pace math -------------------------------------------------

def pace_status(target: float, actual: Optional[float], elapsed_period: float,
                total_period: float, remaining_units: Optional[float] = None) -> dict:
    """target/actual over a period (e.g. sessions this month). Returns the
    expected-by-now value, delta, and the rate still required to hit target.

    `actual=None` means genuinely not-yet-synced, not zero - it must never
    be substituted with 0 by a caller. expected_by_now can still be computed
    (it only depends on target/elapsed/total), but delta/on_pace/required_rate
    all stay None rather than doing arithmetic against a fabricated number."""
    if total_period <= 0:
        return {"target": target, "actual": actual, "expected_by_now": None,
                "delta": None, "required_rate": None, "on_pace": None}

    expected_by_now = target * (elapsed_period / total_period)

    if actual is None:
        return {
            "target": target, "actual": None,
            "expected_by_now": round(expected_by_now, 2),
            "delta": None, "required_rate": None, "on_pace": None,
        }

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


def _round_to_int_fields(result: dict, *fields: str) -> dict:
    """Session counts are discrete - "expected 11.23 sessions" isn't a real
    quantity a human would say. Applied by the specific caller (not inside
    pace_status itself, which is shared by non-session quantities too)."""
    for f in fields:
        if result.get(f) is not None:
            result[f] = round(result[f])
    return result


def _round_to_nearest_fields(result: dict, nearest: int, *fields: str) -> dict:
    for f in fields:
        if result.get(f) is not None:
            result[f] = round(result[f] / nearest) * nearest
    return result


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
    today = today or config.local_today()
    month_start = today.replace(day=1)
    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    month_end = next_month - timedelta(days=1)

    total_period = (next_month - month_start).days
    elapsed_period = (today - month_start).days + 1
    remaining_units = (month_end - today).days

    actual = _count_activities(conn, month_start, today, "strength")
    target = goals["strength"]["monthly_sessions"]

    result = pace_status(target, actual, elapsed_period, total_period, remaining_units)
    _round_to_int_fields(result, "expected_by_now", "delta", "required_rate")
    result.update({"period": "month", "period_start": month_start.isoformat(),
                   "period_end": month_end.isoformat()})
    return result


def racquet_pace(conn, goals: dict, today: Optional[date] = None) -> dict:
    today = today or config.local_today()
    week_start = today - timedelta(days=today.weekday())  # Monday
    week_end = week_start + timedelta(days=6)

    total_period = 7
    elapsed_period = (today - week_start).days + 1
    remaining_units = (week_end - today).days

    actual = _count_activities(conn, week_start, today, "racquet")
    target = goals["racquet"]["weekly_sessions"]

    result = pace_status(target, actual, elapsed_period, total_period, remaining_units)
    _round_to_int_fields(result, "expected_by_now", "delta", "required_rate")
    result.update({"period": "week", "period_start": week_start.isoformat(),
                   "period_end": week_end.isoformat()})
    return result


def hourly_step_curve(conn, today: date, days: int = 90) -> list[float]:
    """Trailing-N-day cumulative fraction of a day's steps typically
    accumulated by the end of each local hour (0-23) - nobody accumulates
    steps linearly from midnight, so this replaces the old flat-fraction-of-
    clock-time estimate. Each day is normalized against its OWN intraday
    total (not daily_metrics.steps) - Garmin's intraday endpoint and its
    daily-summary endpoint don't always agree on the day's total, confirmed
    against real data, so self-normalizing avoids that cross-endpoint
    mismatch entirely; only the accumulation *shape* is used here. Returns
    [] if there isn't at least a few days of intraday history yet."""
    start = today - timedelta(days=days)
    end = today - timedelta(days=1)
    rows = db.fetch_all_dicts(
        conn, "SELECT date, hour, steps FROM intraday_steps WHERE date >= ? AND date <= ?",
        (start.isoformat(), end.isoformat()),
    )
    by_date: dict[str, dict[int, int]] = {}
    for r in rows:
        by_date.setdefault(r["date"], {})[r["hour"]] = r["steps"]

    cumulative_fractions: dict[int, list[float]] = {h: [] for h in range(24)}
    for hourly in by_date.values():
        day_total = sum(hourly.values())
        if day_total <= 0:
            continue
        cumulative = 0
        for h in range(24):
            cumulative += hourly.get(h, 0)
            cumulative_fractions[h].append(cumulative / day_total)

    if not any(cumulative_fractions.values()):
        return []

    curve = []
    prev = 0.0
    for h in range(24):
        vals = cumulative_fractions[h]
        frac = (sum(vals) / len(vals)) if vals else prev
        curve.append(frac)
        prev = frac
    for i in range(1, 24):  # guard against float noise making it non-monotonic
        curve[i] = max(curve[i], curve[i - 1])
    return curve


def steps_pace(conn, goals: dict, today: Optional[date] = None) -> dict:
    """Daily step pace. expected_by_now scales against the user's own
    learned hourly accumulation curve (see hourly_step_curve) rather than a
    flat fraction of clock time elapsed - falls back to the flat estimate
    only until enough intraday history has accumulated."""
    today = today or config.local_today()
    row = db.fetch_all_dicts(conn, "SELECT steps FROM daily_metrics WHERE date = ?", (today.isoformat(),))
    # None (not 0) when today hasn't synced yet, or synced without a steps
    # reading - a real absence of data, not a real zero step count.
    actual = row[0]["steps"] if row else None
    target = goals["steps"]["daily_target"]

    now = datetime.now(config.LOCAL_TZ)
    flat_fraction = (now.hour * 60 + now.minute) / (24 * 60)

    curve = hourly_step_curve(conn, today)
    if curve:
        h0 = now.hour
        within_hour = now.minute / 60
        prev_cum = curve[h0 - 1] if h0 > 0 else 0.0
        fraction = prev_cum + (curve[h0] - prev_cum) * within_hour
        method = "hourly_learned_curve (trailing 90d intraday)"
    else:
        fraction = flat_fraction
        method = "flat_fraction_of_day (insufficient intraday history yet)"

    result = pace_status(target, actual, fraction, 1.0, remaining_units=(1.0 - fraction))
    result["method"] = method
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
    today = today or config.local_today()
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
    today = today or config.local_today()
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
        rate_per_week = round(slope * 7, 1)
        rate_ci = (round(low_slope * 7, 1), round(high_slope * 7, 1))

    checkpoint = next_checkpoint(goals["weight"]["checkpoints"], today)
    projected_range = None
    required_rate = None
    if checkpoint:
        target = checkpoint["target"]
        checkpoint_date = _parse_date(checkpoint["date"])
        weeks_to_checkpoint = max((checkpoint_date - today).days / 7.0, 0)
        if weeks_to_checkpoint > 0:
            required_rate = round((target - current_trend) / weeks_to_checkpoint, 1)

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
    today = today or config.local_today()
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
    today = today or config.local_today()
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
    today = today or config.local_today()
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
    today = today or config.local_today()
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


def weekly_review_window(today: Optional[date] = None) -> dict:
    """The last 7 COMPLETE days - today doesn't count as complete yet. This
    is always a fixed rolling window regardless of what day-of-week the
    weekly review actually runs on, and gets stamped into the review's own
    header so an off-schedule run is self-documenting rather than silently
    mislabeled as "this week"."""
    today = today or config.local_today()
    end = today - timedelta(days=1)
    start = end - timedelta(days=6)
    return {"start": start.isoformat(), "end": end.isoformat()}


def rolling_racquet_minutes_jump(conn, today: Optional[date] = None) -> dict:
    """Same signal as racquet_minutes_jump, but always two full, equal-length
    7-day windows (last 7 complete days vs. the 7 before that) rather than
    calendar Monday-Sunday weeks. racquet_minutes_jump's "this week so far"
    is fine for a daily running check-in, but comparing a partial current
    week against a complete prior week is exactly what produced a false
    "racquet minutes down 76%" when the weekly review ran on a Wednesday -
    the weekly review must use this version, never the calendar-week one."""
    window = weekly_review_window(today)
    end = _parse_date(window["end"])
    start = _parse_date(window["start"])
    prior_end = start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=6)

    this_period = _sum_duration_minutes(conn, start, end, "racquet")
    prior_period = _sum_duration_minutes(conn, prior_start, prior_end, "racquet")
    pct_change = round((this_period - prior_period) / prior_period, 2) if prior_period else None

    return {
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "this_period_minutes": this_period,
        "prior_period_start": prior_start.isoformat(),
        "prior_period_end": prior_end.isoformat(),
        "prior_period_minutes": prior_period,
        "pct_change": pct_change,
        "flag_jump": pct_change is not None and pct_change > 0.40,
    }


# ---- Sync freshness --------------------------------------------------------

SYNC_SOURCES = ("daily_metrics", "activities", "training_status")
STALE_AFTER_HOURS = 6  # sync runs twice daily (~04:30/11:00 Central); 6h covers
                       # that cadence plus slack for one missed run


def sync_status(conn, now: Optional[datetime] = None) -> dict:
    """Per-source last-sync time and staleness, computed once here so the
    dashboard, the email, and the pre-send gate all agree on what "fresh"
    means - none of them may re-derive this from sync_log independently."""
    now = now or datetime.now(timezone.utc)
    sources = {}
    any_stale = False
    for source in SYNC_SOURCES:
        rows = db.fetch_all_dicts(
            conn,
            "SELECT timestamp, status FROM sync_log WHERE source = ? ORDER BY timestamp DESC LIMIT 1",
            (source,),
        )
        if not rows:
            sources[source] = {"last_sync_at": None, "status": "never", "stale": True}
            any_stale = True
            continue
        last_time = datetime.fromisoformat(rows[0]["timestamp"])
        stale = rows[0]["status"] != "ok" or (now - last_time) > timedelta(hours=STALE_AFTER_HOURS)
        sources[source] = {
            "last_sync_at": rows[0]["timestamp"],
            "status": rows[0]["status"],
            "stale": stale,
        }
        any_stale = any_stale or stale

    return {"sources": sources, "any_stale": any_stale}


# ---- Readiness gate -------------------------------------------------------

def readiness(body_battery_wake: Optional[int], hrv_status: Optional[str],
              sleep_score: Optional[int], resting_hr: Optional[int],
              baseline_resting_hr_30d: Optional[float]) -> dict:
    """Simple, transparent rule-based classification - green/amber/red/unknown
    - deliberately not a black-box model, so the reasoning is always visible.

    Body Battery and sleep score are required inputs: missing either one
    means there isn't enough signal to call it green (or red/amber), so the
    state is `unknown` rather than guessing. Green specifically requires
    present, in-range inputs - it is never the default."""
    if body_battery_wake is None or sleep_score is None:
        missing = [
            name for name, v in (("body_battery_wake", body_battery_wake), ("sleep_score", sleep_score))
            if v is None
        ]
        return {
            "state": "unknown",
            "reasons": [f"missing: {', '.join(missing)}"],
            "body_battery_wake": body_battery_wake,
            "sleep_score": sleep_score,
            "resting_hr": resting_hr,
            "hrv_status": hrv_status,
        }

    red = amber = 0
    reasons = []

    if hrv_status in ("LOW", "UNBALANCED"):
        red += 1
        reasons.append(f"hrv_status={hrv_status}")

    if sleep_score < 60:
        red += 1
        reasons.append(f"sleep_score={sleep_score} (<60)")
    elif sleep_score < 75:
        amber += 1
        reasons.append(f"sleep_score={sleep_score} (<75)")

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

    return {
        "state": state,
        "reasons": reasons,
        # Raw inputs the decision was made from, carried through so callers
        # (dashboard template, email) read them from this one dict instead
        # of re-querying daily_metrics themselves.
        "body_battery_wake": body_battery_wake,
        "sleep_score": sleep_score,
        "resting_hr": resting_hr,
        "hrv_status": hrv_status,
    }


def readiness_today(conn, today: Optional[date] = None) -> dict:
    today = today or config.local_today()
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
    today = today or config.local_today()
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


# ---- Activity & Calories page (Phase 3) ----------------------------------

BUCKETS = ["strength", "racquet", "cardio", "other"]
_WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def weekly_calories_by_bucket(conn, weeks: int = 8, today: Optional[date] = None) -> dict:
    """Per-week active-calorie totals broken out by activity bucket, trailing
    N weeks - the main calorie chart per spec §4 ("where is my output
    actually coming from")."""
    today = today or config.local_today()
    this_week_start = today - timedelta(days=today.weekday())
    start = this_week_start - timedelta(weeks=weeks - 1)

    rows = db.fetch_all_dicts(
        conn,
        "SELECT date, bucket, calories FROM activities WHERE date >= ? AND date <= ? AND calories IS NOT NULL",
        (start.isoformat(), today.isoformat()),
    )

    week_labels = []
    data = {b: [] for b in BUCKETS}
    cursor = start
    for _ in range(weeks):
        week_start = cursor
        week_end = week_start + timedelta(days=6)
        week_labels.append(week_start.isoformat())
        for b in BUCKETS:
            total = sum(
                r["calories"] for r in rows
                if r["bucket"] == b and week_start.isoformat() <= r["date"] <= week_end.isoformat()
            )
            data[b].append(round(total))
        cursor += timedelta(weeks=1)

    return {"week_labels": week_labels, "buckets": BUCKETS, "data": data}


def weekday_step_cycle(conn, weeks: int = 12, today: Optional[date] = None) -> dict:
    """Steps grouped by weekday, one panel per weekday showing that
    weekday's series across the trailing N weeks with its own Theil-Sen
    trend - surfaces things a normal time series structurally hides (e.g. a
    specific weekday eroding over months), per spec §7.3."""
    today = today or config.local_today()
    start = today - timedelta(weeks=weeks)
    rows = db.fetch_all_dicts(
        conn,
        "SELECT date, steps FROM daily_metrics WHERE date >= ? AND date <= ? AND steps IS NOT NULL",
        (start.isoformat(), today.isoformat()),
    )

    panels = {i: [] for i in range(7)}
    for r in rows:
        d = _parse_date(r["date"])
        panels[d.weekday()].append((d, r["steps"]))

    result = {}
    for i, name in enumerate(_WEEKDAY_NAMES):
        series = sorted(panels[i])
        trend_per_week = None
        if len(series) >= 4:
            xs = [(d - series[0][0]).days for d, _ in series]
            ys = [v for _, v in series]
            slope, *_rest = theilslopes(ys, xs, alpha=0.90)
            trend_per_week = round(slope * 7, 1)
        result[name] = {
            "points": [{"date": d.isoformat(), "steps": v} for d, v in series],
            "trend_per_week": trend_per_week,
        }
    return result


def calendar_heatmap_data(conn, month: Optional[date] = None) -> dict:
    """One entry per day in the month with its dominant activity bucket (or
    None), for the month calendar heatmap. Strength/racquet outrank cardio
    outranks other when a day has more than one activity."""
    month = month or config.local_today().replace(day=1)
    month_start = month.replace(day=1)
    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    month_end = next_month - timedelta(days=1)

    rows = db.fetch_all_dicts(
        conn,
        "SELECT date, bucket FROM activities WHERE date >= ? AND date <= ?",
        (month_start.isoformat(), month_end.isoformat()),
    )
    priority = {"strength": 3, "racquet": 3, "cardio": 2, "other": 1}
    by_date: dict[str, str] = {}
    for r in rows:
        cur = by_date.get(r["date"])
        if cur is None or priority.get(r["bucket"], 0) > priority.get(cur, 0):
            by_date[r["date"]] = r["bucket"]

    days = []
    d = month_start
    while d <= month_end:
        days.append({"date": d.isoformat(), "bucket": by_date.get(d.isoformat())})
        d += timedelta(days=1)

    return {"month": month_start.isoformat(), "days": days}


def avg_calories_per_session(conn, days: int = 90, today: Optional[date] = None) -> list[dict]:
    """Average calories by activity type, trailing N days - "what's a
    typical tennis session vs. a typical lift worth", per spec §4."""
    today = today or config.local_today()
    start = today - timedelta(days=days)
    rows = db.fetch_all_dicts(
        conn,
        "SELECT activity_type, calories FROM activities WHERE date >= ? AND date <= ? AND calories IS NOT NULL",
        (start.isoformat(), today.isoformat()),
    )
    sums: dict[str, list[float]] = {}
    for r in rows:
        sums.setdefault(r["activity_type"], []).append(r["calories"])

    return sorted(
        [{"activity_type": t, "avg_calories": round(sum(v) / len(v)), "n": len(v)} for t, v in sums.items()],
        key=lambda x: -x["avg_calories"],
    )


# ---- Full snapshot (the single source of truth for the dashboard, the
# daily email, and the weekly review - none of those may compute their own
# aggregate query; they all read this dict) --------------------------------

def build_snapshot(conn, goals: dict, today: Optional[date] = None) -> dict:
    """The only function that computes report-facing metrics. `snapshot_date`
    is the one date every consumer must render and can assert against -
    never re-derived, never inferred from "the latest row in the table"."""
    today = today or config.local_today()
    snapshot = {
        "snapshot_date": today.isoformat(),
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
        "sync_status": sync_status(conn),
    }

    # Canary for the date-boundary bug class (steps queried for "today" and
    # "yesterday" silently landing on the same underlying row): a real
    # human being able to walk exactly as many steps two days running is
    # astronomically unlikely, so equal-and-nonzero means a query resolved
    # the wrong date, not a coincidence. None ("not yet synced") is a
    # distinct state from 0 and never trips this - only two genuinely equal,
    # nonzero, non-missing readings do.
    today_steps = snapshot["steps_pace"]["actual"]
    yesterday_steps = snapshot["yesterday"]["steps"]
    collision = (
        today_steps is not None and yesterday_steps is not None
        and today_steps != 0 and today_steps == yesterday_steps
    )
    assert not collision, (
        f"snapshot_date={snapshot['snapshot_date']}: today's steps ({today_steps}) and "
        f"yesterday's steps ({yesterday_steps}) are identical and nonzero - "
        "one of the two queries resolved the wrong date."
    )

    # Rounded for display/citation only, after the exact-value assertion
    # above has already run - steps carry enough device noise that showing
    # the raw count implies false precision.
    _round_to_nearest_fields(
        snapshot["steps_pace"], 100, "actual", "expected_by_now", "delta", "required_rate"
    )

    return snapshot
