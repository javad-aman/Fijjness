"""Derived metrics - pace, trend weight, readiness, load, consistency.

Pure functions: every calculation lives here, consumed by both the (future)
web app and the (future) LLM coach. Per the design spec: "the LLM must never
do arithmetic on raw data" - this module is where all of the arithmetic
happens, once.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

import numpy as np
from scipy.stats import theilslopes

from garmin_tracker import config, db


def _parse_date(d) -> date:
    if isinstance(d, date):
        return d
    return datetime.strptime(str(d), "%Y-%m-%d").date()


def _activity_dates(conn, start: date, end: date, bucket: str) -> list[date]:
    rows = db.fetch_all_dicts(
        conn,
        "SELECT date FROM activities WHERE date >= ? AND date <= ? AND bucket = ?",
        (start.isoformat(), end.isoformat(), bucket),
    )
    return sorted(_parse_date(r["date"]) for r in rows if r["date"])


# ---- Monthly pace rails (steps/strength/racquet) --------------------------
#
# All three goals are monthly, integers only. A rail is "reachable" only if
# the rate still required exceeds nothing more than the best sustained rate
# the user has actually managed in the trailing 90 days - reachability is
# checked against real behavior, not an arbitrary cutoff.

def _month_bounds(d: date) -> tuple[date, date]:
    month_start = d.replace(day=1)
    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    month_end = next_month - timedelta(days=1)
    return month_start, month_end


def _steps_target_for_month(goals: dict, month_start: date) -> int:
    key = month_start.strftime("%Y-%m")
    targets = goals["steps"].get("monthly_targets") or {}
    return targets.get(key, goals["steps"]["monthly_default"])


def _daily_step_counts(conn, start: date, end: date) -> dict:
    rows = db.fetch_all_dicts(
        conn, "SELECT date, steps FROM daily_metrics WHERE date >= ? AND date <= ? AND steps IS NOT NULL",
        (start.isoformat(), end.isoformat()),
    )
    return {r["date"]: r["steps"] for r in rows}


def _daily_session_counts(conn, start: date, end: date, bucket: str) -> dict:
    """1 for each day at least one session in `bucket` happened - used for
    the reachability ceiling (a realistic sessions/day rate), not raw
    activity counts (multiple same-day sessions are rare and would distort
    the "best rate you've actually sustained" ceiling)."""
    counts: dict[str, int] = {}
    for d in _activity_dates(conn, start, end, bucket):
        counts[d.isoformat()] = counts.get(d.isoformat(), 0) + 1
    return counts


def _best_rate_90d(daily_counts: dict, snapshot_date: date, window: int = 7) -> float:
    """Best sustained rate/day in any `window`-day rolling window across the
    trailing 90 days - the ceiling a remaining pace requirement is checked
    against to decide whether it's still reachable."""
    start = snapshot_date - timedelta(days=89)
    values = [daily_counts.get((start + timedelta(days=i)).isoformat(), 0) for i in range(90)]
    if len(values) < window:
        return sum(values) / max(len(values), 1)
    return max(sum(values[i:i + window]) / window for i in range(len(values) - window + 1))


def _became_unreachable_date(daily_counts: dict, month_start: date, month_end: date,
                              snapshot_date: date, target: int, best_rate: float) -> Optional[str]:
    """First day within the month where the rate still required for the
    rest of the month first exceeded the trailing-90d best rate - the day
    the goal quietly became out of reach, not just "today"."""
    cumulative = 0
    d = month_start
    while d <= snapshot_date:
        cumulative += daily_counts.get(d.isoformat(), 0)
        days_remaining_from_d = (month_end - d).days
        if days_remaining_from_d > 0:
            required = (target - cumulative) / days_remaining_from_d
            if required > best_rate:
                return d.isoformat()
        d += timedelta(days=1)
    return None


def _monthly_pace(daily_counts: dict, target: int, snapshot_date: date,
                   month_start: date, month_end: date, best_rate: float) -> dict:
    """The one pace-rail model shared by steps/strength/racquet. Integers
    only - a fractional pace may position the expected tick, but it is
    never printed. States: cleared / behind (reachable) / dead (not
    reachable, so grey rather than red - it's over, not urgent)."""
    days_in_month = (month_end - month_start).days + 1
    days_elapsed = (snapshot_date - month_start).days + 1
    days_remaining = (month_end - snapshot_date).days  # days AFTER snapshot_date still to come
    actual = sum(v for k, v in daily_counts.items() if month_start.isoformat() <= k <= snapshot_date.isoformat())
    expected_by_now = round(target * days_elapsed / days_in_month)
    remaining = target - actual

    base = {
        "actual": actual, "target": target,
        "expected_by_now": expected_by_now,
        "days_remaining": days_remaining,
        "days_elapsed": days_elapsed,
        "days_in_month": days_in_month,
        "avg_rate": round(actual / days_elapsed, 2) if days_elapsed else 0.0,
        "original_required_rate": round(target / days_in_month, 2),
    }

    if remaining <= 0:
        base.update({"state": "cleared", "over": actual - target})
        return base

    if days_remaining <= 0:
        base.update({"state": "dead", "remaining": remaining, "reachable": False,
                      "required_rate": None, "became_unreachable_date": None})
        return base

    required_rate = remaining / days_remaining
    # A ceiling of 0 (no sessions at all in the trailing 90 days) still
    # allows "1 more, whenever" to read as reachable rather than
    # automatically dead - only a genuinely demanding rate is flagged dead.
    reachable = required_rate <= max(best_rate, 1.0 / 7)
    base.update({
        "remaining": remaining,
        "required_rate": round(required_rate, 2),
        "reachable": reachable,
        "state": "behind" if reachable else "dead",
    })
    if not reachable:
        base["became_unreachable_date"] = _became_unreachable_date(
            daily_counts, month_start, month_end, snapshot_date, target, best_rate
        )
    return base


def steps_pace(conn, goals: dict, today: Optional[date] = None) -> dict:
    """Monthly steps pace (replaces the old daily/intraday rail entirely -
    see config.snapshot_date's docstring for why "today" no longer has a
    partial-day rail)."""
    snap = today or config.snapshot_date()
    month_start, month_end = _month_bounds(snap)
    target = _steps_target_for_month(goals, month_start)
    lookback_start = snap - timedelta(days=89)
    daily_counts = _daily_step_counts(conn, min(month_start, lookback_start), snap)
    best_rate = _best_rate_90d(daily_counts, snap)
    return _monthly_pace(daily_counts, target, snap, month_start, month_end, best_rate)


def strength_pace(conn, goals: dict, today: Optional[date] = None) -> dict:
    snap = today or config.snapshot_date()
    month_start, month_end = _month_bounds(snap)
    target = goals["strength"]["monthly_sessions"]
    lookback_start = snap - timedelta(days=89)
    daily_counts = _daily_session_counts(conn, min(month_start, lookback_start), snap, "strength")
    best_rate = _best_rate_90d(daily_counts, snap)
    return _monthly_pace(daily_counts, target, snap, month_start, month_end, best_rate)


def racquet_pace(conn, goals: dict, today: Optional[date] = None) -> dict:
    """Monthly (was weekly) - see goals.yaml's racquet.monthly_sessions."""
    snap = today or config.snapshot_date()
    month_start, month_end = _month_bounds(snap)
    target = goals["racquet"]["monthly_sessions"]
    lookback_start = snap - timedelta(days=89)
    daily_counts = _daily_session_counts(conn, min(month_start, lookback_start), snap, "racquet")
    best_rate = _best_rate_90d(daily_counts, snap)
    result = _monthly_pace(daily_counts, target, snap, month_start, month_end, best_rate)
    # Racquet's own footer reads naturally as a weekly rate ("2.1/week")
    # rather than daily, per dashboard-prototype-v3.html.
    result["avg_rate_per_week"] = round(result["avg_rate"] * 7, 1)
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
    """goals.yaml's checkpoint dates are unquoted YAML dates, so PyYAML hands
    them back as native `date` objects, not strings - normalize to isoformat
    here so the dict that flows into build_snapshot() is always JSON-safe
    (the snapshot gets json.dumps'd into the briefs table)."""
    today = today or config.snapshot_date()
    upcoming = [c for c in checkpoints if _parse_date(c["date"]) >= today]
    if not upcoming:
        return None
    nearest = min(upcoming, key=lambda c: _parse_date(c["date"]))
    return {**nearest, "date": _parse_date(nearest["date"]).isoformat()}


def trend_weight(conn, goals: dict, today: Optional[date] = None) -> dict:
    """7-day EWMA as the primary number (raw points kept only for faint-dot
    display). Rate and projection use Theil-Sen (median-of-slopes) rather
    than ordinary least squares - robust to the single-outlier weigh-ins a
    lone subject produces - over the trailing 21 days, with a 90% CI on the
    slope propagated into an earliest/latest projected-checkpoint-date band
    rather than a single false-precision date."""
    today = today or config.snapshot_date()
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


def weight_projection_path(current_trend: float, rate_per_week: Optional[float],
                            rate_ci: Optional[tuple], checkpoint: Optional[dict],
                            today: date, points_per_week: int = 1) -> Optional[list[dict]]:
    """Straight-line projection from today's trend weight to the next
    checkpoint, using the same Theil-Sen rate + 90% CI trend_weight()
    already computes - never a fabricated curve. Each point carries the
    central estimate plus the CI-bound "best"/"worst" case, where best/
    worst are direction-aware (the CI bound that moves toward the target
    faster is "best", regardless of whether the goal is to gain or lose).
    Returns None if there's no checkpoint or no rate to project - a
    projection implies a real destination and a real observed rate; this
    never invents either."""
    if not checkpoint or rate_per_week is None or rate_ci is None:
        return None

    checkpoint_date = _parse_date(checkpoint["date"])
    target = checkpoint["target"]
    total_days = (checkpoint_date - today).days
    if total_days <= 0:
        return None

    losing_toward_target = target < current_trend
    low, high = rate_ci
    best_rate = min(low, high) if losing_toward_target else max(low, high)
    worst_rate = max(low, high) if losing_toward_target else min(low, high)

    step_days = max(7 // points_per_week, 1)
    path = []
    d = 0
    while d <= total_days:
        day = today + timedelta(days=d)
        weeks = d / 7
        path.append({
            "date": day.isoformat(),
            "expected": round(current_trend + rate_per_week * weeks, 1),
            "best": round(current_trend + best_rate * weeks, 1),
            "worst": round(current_trend + worst_rate * weeks, 1),
        })
        d += step_days
    if path[-1]["date"] != checkpoint_date.isoformat():
        weeks = total_days / 7
        path.append({
            "date": checkpoint_date.isoformat(),
            "expected": round(current_trend + rate_per_week * weeks, 1),
            "best": round(current_trend + best_rate * weeks, 1),
            "worst": round(current_trend + worst_rate * weeks, 1),
        })
    return path


# ---- Consistency / burst detection --------------------------------------

def front_load_index(conn, bucket: str = "strength", today: Optional[date] = None) -> Optional[float]:
    today = today or config.snapshot_date()
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
    today = today or config.snapshot_date()
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
    today = today or config.snapshot_date()
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
    today = today or config.snapshot_date()
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
    """The last 7 COMPLETE days. `today` already defaults to
    config.snapshot_date() - the latest complete day - so no further offset
    is applied here; end == snapshot_date by construction, matching every
    other figure in the same brief. This is always a fixed rolling window
    regardless of what day-of-week the weekly review actually runs on, and
    gets stamped into the review's own header so an off-schedule run is
    self-documenting rather than silently mislabeled as "this week"."""
    end = today or config.snapshot_date()
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


def _baseline_resting_hr(conn, before: date, days: int = 30) -> Optional[float]:
    start = before - timedelta(days=days)
    rows = db.fetch_all_dicts(
        conn,
        "SELECT resting_hr FROM daily_metrics WHERE date >= ? AND date < ? AND resting_hr IS NOT NULL",
        (start.isoformat(), before.isoformat()),
    )
    return sum(r["resting_hr"] for r in rows) / len(rows) if rows else None


def readiness_today(conn, today: Optional[date] = None) -> dict:
    today = today or config.snapshot_date()
    row = db.fetch_all_dicts(
        conn, "SELECT * FROM daily_metrics WHERE date = ?", (today.isoformat(),)
    )
    row = row[0] if row else {}

    baseline = _baseline_resting_hr(conn, today)

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

    if result["state"] == "unknown":
        sync_rows = db.fetch_all_dicts(
            conn, "SELECT timestamp FROM sync_log WHERE source = 'daily_metrics' AND status = 'ok' "
                  "ORDER BY timestamp DESC LIMIT 1",
        )
        if sync_rows:
            last_sync_utc = datetime.fromisoformat(sync_rows[0]["timestamp"])
            last_sync_local = last_sync_utc.astimezone(config.LOCAL_TZ)
            time_str = last_sync_local.strftime("%I:%M%p").lstrip("0").lower()
            result["unknown_reason"] = f"no sync since {time_str}"
        else:
            result["unknown_reason"] = "never synced"

    return result


def resting_hr_elevation(conn, today: Optional[date] = None, threshold: float = 5.0) -> dict:
    """Resting HR >= threshold above the 30-day baseline, sustained for N
    consecutive days ending at snapshot_date - the overreaching signal bug 7
    asks to be wired into both the brief and Recovery, not left sitting
    next to the numbers that would explain it without ever being connected
    to them."""
    snap = today or config.snapshot_date()
    baseline = _baseline_resting_hr(conn, snap)
    current_row = db.fetch_all_dicts(conn, "SELECT resting_hr FROM daily_metrics WHERE date = ?", (snap.isoformat(),))
    current = current_row[0]["resting_hr"] if current_row else None

    if baseline is None or current is None:
        return {"elevated": False, "current": current, "baseline": None, "diff": None, "consecutive_days": 0}

    diff = round(current - baseline, 1)
    consecutive = 0
    d = snap
    while True:
        row = db.fetch_all_dicts(conn, "SELECT resting_hr FROM daily_metrics WHERE date = ?", (d.isoformat(),))
        rhr = row[0]["resting_hr"] if row else None
        day_baseline = _baseline_resting_hr(conn, d)
        if rhr is None or day_baseline is None or (rhr - day_baseline) < threshold:
            break
        consecutive += 1
        d -= timedelta(days=1)

    return {
        "elevated": diff >= threshold and consecutive >= 2,
        "current": current,
        "baseline": round(baseline, 1),
        "diff": diff,
        "consecutive_days": consecutive,
    }


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


def _month_avg(conn, column: str, month_start: date, snap: date) -> Optional[float]:
    rows = db.fetch_all_dicts(
        conn, f"SELECT {column} as v FROM daily_metrics WHERE date >= ? AND date <= ? AND {column} IS NOT NULL",
        (month_start.isoformat(), snap.isoformat()),
    )
    return sum(r["v"] for r in rows) / len(rows) if rows else None


def _pct_delta(actual: Optional[float], avg: Optional[float]) -> Optional[float]:
    if actual is None or avg is None or avg == 0:
        return None
    return round((actual - avg) / avg * 100)


def yesterday_summary(conn, today: Optional[date] = None) -> dict:
    """The most recent COMPLETE day's raw metrics (= snapshot_date itself -
    under the midnight cutoff there's only one canonical date, not a
    separate "today vs yesterday" pair to get the offset wrong on), each
    compared against this month's average so far. Rest days get an
    explicit tag rather than a blank activity row."""
    snap = today or config.snapshot_date()
    month_start, _ = _month_bounds(snap)

    rows = db.fetch_all_dicts(conn, "SELECT * FROM daily_metrics WHERE date = ?", (snap.isoformat(),))
    row = rows[0] if rows else {}

    sleep_hours = round(row["sleep_minutes"] / 60.0, 1) if row.get("sleep_minutes") is not None else None
    sleep_avg_minutes = _month_avg(conn, "sleep_minutes", month_start, snap)
    sleep_avg_hours = round(sleep_avg_minutes / 60.0, 1) if sleep_avg_minutes is not None else None

    activity_rows = db.fetch_all_dicts(
        conn,
        "SELECT activity_type, name, duration_min FROM activities WHERE date = ? ORDER BY start_time",
        (snap.isoformat(),),
    )

    baseline_resting_hr = _baseline_resting_hr(conn, snap)
    steps_avg = _month_avg(conn, "steps", month_start, snap)
    cal_avg = _month_avg(conn, "active_calories", month_start, snap)

    return {
        "date": snap.isoformat(),
        "is_rest_day": len(activity_rows) == 0,
        "steps": row.get("steps"),
        "steps_avg_month": round(steps_avg) if steps_avg is not None else None,
        "steps_pct_delta": _pct_delta(row.get("steps"), steps_avg),
        "activities": [
            {"type": a["activity_type"], "name": a["name"], "duration_min": a["duration_min"]}
            for a in activity_rows
        ],
        "active_calories": row.get("active_calories"),
        "active_calories_avg_month": round(cal_avg) if cal_avg is not None else None,
        "active_calories_pct_delta": _pct_delta(row.get("active_calories"), cal_avg),
        "sleep_hours": sleep_hours,
        "sleep_hours_avg_month": sleep_avg_hours,
        "sleep_hours_delta": round(sleep_hours - sleep_avg_hours, 1) if sleep_hours is not None and sleep_avg_hours is not None else None,
        "resting_hr": row.get("resting_hr"),
        "resting_hr_baseline_30d": round(baseline_resting_hr, 1) if baseline_resting_hr else None,
        "resting_hr_delta": round(row["resting_hr"] - baseline_resting_hr, 1) if row.get("resting_hr") is not None and baseline_resting_hr else None,
    }


# ---- Activity & Calories page (Phase 3) ----------------------------------

BUCKETS = ["strength", "racquet", "cardio", "other"]
_WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def weekly_calories_by_bucket(conn, weeks: int = 8, today: Optional[date] = None) -> dict:
    """Per-week active-calorie totals broken out by activity bucket, trailing
    N weeks - the main calorie chart per spec §4 ("where is my output
    actually coming from")."""
    today = today or config.snapshot_date()
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
    today = today or config.snapshot_date()
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
    month = month or config.snapshot_date().replace(day=1)
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
    today = today or config.snapshot_date()
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
    never re-derived, never inferred from "the latest row in the table".

    Defaults to config.snapshot_date() (the latest COMPLETE day, i.e.
    local_today() - 1) - the midnight cutoff. Every figure here covers data
    through that date; nothing reports on the current, necessarily-partial
    calendar day. This is also why the old today-vs-yesterday collision
    assertion is gone: there's only one canonical date now, so there's
    nothing left for a "today" query and a "yesterday" query to
    accidentally collide on."""
    today = today or config.snapshot_date()
    readiness = readiness_today(conn, today)
    rhr_elevation = resting_hr_elevation(conn, today)
    acwr = acute_chronic_load_ratio(conn, today)
    yesterday = yesterday_summary(conn, today)
    snapshot = {
        "snapshot_date": today.isoformat(),
        "steps_pace": steps_pace(conn, goals, today),
        "strength_pace": strength_pace(conn, goals, today),
        "racquet_pace": racquet_pace(conn, goals, today),
        "trend_weight": trend_weight(conn, goals, today),
        "front_load_index": front_load_index(conn, today=today),
        "burst_pattern": burst_pattern(conn, today=today),
        "acute_chronic_load_ratio": acwr,
        "racquet_minutes_jump": racquet_minutes_jump(conn, today),
        "readiness": readiness,
        "resting_hr_elevation": rhr_elevation,
        "recovery": recovery_summary(readiness, rhr_elevation, acwr, yesterday),
        "yesterday": yesterday,
        "sync_status": sync_status(conn),
    }
    return snapshot


# ---- Today dashboard v2 (descriptive charts, no inference gate) -----------
#
# Everything below just shows measured data - no statistical claims, so none
# of Phase 5's validation machinery applies here. The one rule that does
# apply everywhere: never render a chart that implies more data exists than
# it does, and never zero-fill a missing day - a gap is a gap.

def coverage_state(n: int, min_required: int, full_target: int) -> str:
    """full / partial / insufficient given how much real data exists against
    a module's own minimum-useful and full-window requirements."""
    if n < min_required:
        return "insufficient"
    if n < full_target:
        return "partial"
    return "full"


def data_coverage(conn) -> dict:
    """One query per metric at page load, per spec sec.0 - min/max date and
    row count for every metric a Today-page chart might need."""
    coverage = {}
    for col in ("steps", "weight_lb", "resting_hr", "sleep_minutes", "active_calories"):
        rows = db.fetch_all_dicts(
            conn, f"SELECT MIN(date) as earliest, MAX(date) as latest, COUNT(*) as n "
                  f"FROM daily_metrics WHERE {col} IS NOT NULL"
        )
        coverage[col] = rows[0] if rows else {"earliest": None, "latest": None, "n": 0}
    activity_rows = db.fetch_all_dicts(
        conn, "SELECT MIN(date) as earliest, MAX(date) as latest, COUNT(*) as n FROM activities"
    )
    coverage["activities"] = activity_rows[0] if activity_rows else {"earliest": None, "latest": None, "n": 0}
    return coverage


def steps_30_day(conn, goals: dict, today: Optional[date] = None,
                  window_days: int = 30, min_required: int = 7) -> dict:
    today = today or config.snapshot_date()
    start = today - timedelta(days=window_days - 1)
    rows = db.fetch_all_dicts(
        conn, "SELECT date, steps FROM daily_metrics WHERE date >= ? AND date <= ? AND steps IS NOT NULL ORDER BY date",
        (start.isoformat(), today.isoformat()),
    )
    n = len(rows)
    state = coverage_state(n, min_required, window_days)
    if state == "insufficient":
        return {"state": "insufficient", "min_required": min_required, "n_available": n}

    by_date = {r["date"]: r["steps"] for r in rows}
    days = []
    d = start
    while d <= today:
        days.append({"date": d.isoformat(), "steps": by_date.get(d.isoformat())})  # None = gap, never 0
        d += timedelta(days=1)

    values = [r["steps"] for r in rows]
    avg = sum(values) / len(values)
    goal = goals["steps"]["daily_target"]
    pct_vs_goal = round((avg - goal) / goal * 100)

    moving_avg_7d = []
    for i in range(len(days)):
        window = [d2["steps"] for d2 in days[max(0, i - 6):i + 1] if d2["steps"] is not None]
        moving_avg_7d.append(round(sum(window) / len(window), 1) if window else None)

    return {
        "state": state,
        "days": days,
        "moving_avg_7d": moving_avg_7d,
        "goal": goal,
        "avg": round(avg),
        "pct_vs_goal": pct_vs_goal,
        "range_start": start.isoformat(),
        "range_end": today.isoformat(),
        "n_available": n,
    }


def weekly_calories_with_total(conn, weeks: int = 12, today: Optional[date] = None,
                                min_required_weeks: int = 3) -> dict:
    """Extends weekly_calories_by_bucket with the total-active-calories
    overlay line, so the gap between "logged activity" and "total movement"
    is visible - per spec, usually the interesting part."""
    today = today or config.snapshot_date()
    base = weekly_calories_by_bucket(conn, weeks=weeks, today=today)

    this_week_start = today - timedelta(days=today.weekday())
    start = this_week_start - timedelta(weeks=weeks - 1)
    rows = db.fetch_all_dicts(
        conn, "SELECT date, active_calories FROM daily_metrics WHERE date >= ? AND date <= ? AND active_calories IS NOT NULL",
        (start.isoformat(), today.isoformat()),
    )

    totals = []
    cursor = start
    complete_weeks = 0
    for _ in range(weeks):
        week_start = cursor
        week_end = week_start + timedelta(days=6)
        week_total = sum(
            r["active_calories"] for r in rows
            if week_start.isoformat() <= r["date"] <= week_end.isoformat()
        )
        totals.append(round(week_total))
        if week_end < today:
            complete_weeks += 1
        cursor += timedelta(weeks=1)

    base["total_active_calories"] = totals
    base["state"] = coverage_state(complete_weeks, min_required_weeks, weeks)
    base["complete_weeks"] = complete_weeks
    return base


def recovery_sparklines(conn, today: Optional[date] = None, days: int = 60,
                         min_required: int = 14) -> dict:
    today = today or config.snapshot_date()
    start = today - timedelta(days=days - 1)
    rows = db.fetch_all_dicts(
        conn, "SELECT date, resting_hr, sleep_minutes FROM daily_metrics WHERE date >= ? AND date <= ? ORDER BY date",
        (start.isoformat(), today.isoformat()),
    )
    n = min(
        sum(1 for r in rows if r["resting_hr"] is not None),
        sum(1 for r in rows if r["sleep_minutes"] is not None),
    )
    state = coverage_state(n, min_required, days)
    if state == "insufficient":
        return {"state": "insufficient", "min_required": min_required, "n_available": n}

    hr_points = [(r["date"], r["resting_hr"]) for r in rows if r["resting_hr"] is not None]
    sleep_points = [(r["date"], round(r["sleep_minutes"] / 60.0, 1)) for r in rows if r["sleep_minutes"] is not None]

    baseline_start = today - timedelta(days=29)
    hr_baseline_vals = [v for d_str, v in hr_points if d_str >= baseline_start.isoformat()]
    hr_mean = sum(hr_baseline_vals) / len(hr_baseline_vals) if hr_baseline_vals else None
    hr_sd = (
        (sum((v - hr_mean) ** 2 for v in hr_baseline_vals) / len(hr_baseline_vals)) ** 0.5
        if hr_baseline_vals else None
    )

    return {
        "state": state,
        "range_start": start.isoformat(),
        "range_end": today.isoformat(),
        "resting_hr": {
            "points": [{"date": d_str, "value": v} for d_str, v in hr_points],
            "mean_30d": round(hr_mean, 1) if hr_mean is not None else None,
            "sd_30d": round(hr_sd, 1) if hr_sd is not None else None,
            "current": hr_points[-1][1] if hr_points else None,
        },
        "sleep_hours": {
            "points": [{"date": d_str, "value": v} for d_str, v in sleep_points],
            "band_low": 7, "band_high": 8,
            "current": sleep_points[-1][1] if sleep_points else None,
        },
    }


def weight_chart_data(conn, goals: dict, today: Optional[date] = None,
                       min_readings: int = 8, min_span_days: int = 21) -> dict:
    today = today or config.snapshot_date()
    rows = db.fetch_all_dicts(conn, "SELECT date, weight_lb FROM daily_metrics WHERE weight_lb IS NOT NULL ORDER BY date")
    n = len(rows)
    span_days = (today - _parse_date(rows[0]["date"])).days if rows else 0

    if n < min_readings or span_days < min_span_days:
        checkpoint = next_checkpoint(goals["weight"]["checkpoints"], today)
        return {
            "state": "insufficient",
            "n_readings": n,
            "since": rows[0]["date"] if rows else None,
            "min_readings": min_readings,
            "min_span_days": min_span_days,
            "checkpoint": checkpoint,
            # Real raw readings, not a decorative placeholder - per v3, show
            # what data exists (just no trend line) rather than a fake preview.
            "raw_points": [{"date": r["date"], "weight_lb": r["weight_lb"]} for r in rows],
        }

    result = trend_weight(conn, goals, today)
    result["state"] = "full"

    # Full-resolution series for the chart itself (trend_weight's own
    # raw_points is capped at the last 30 for the JSON snapshot/LLM - the
    # chart needs the whole window plus a matched EWMA line, faint dots,
    # a straight line to the checkpoint target, and the projection band).
    full_rows = [r for r in rows if _parse_date(r["date"]) >= today - timedelta(days=180)]
    full_ewma = _ewma([r["weight_lb"] for r in full_rows])
    result["chart_series"] = [
        {"date": r["date"], "weight_lb": r["weight_lb"], "ewma": round(e, 2)}
        for r, e in zip(full_rows, full_ewma)
    ]
    result["projection"] = weight_projection_path(
        result["trend_weight_lb"], result["rate_lb_per_week"],
        result["rate_lb_per_week_ci"], result["checkpoint"], today,
    )
    return result


def log_weight(conn, target_date: date, weight_lb: float) -> None:
    """The [Log weight] button - simplest possible manual entry, one row."""
    existing = db.fetch_all_dicts(conn, "SELECT date FROM daily_metrics WHERE date = ?", (target_date.isoformat(),))
    if existing:
        conn.execute("UPDATE daily_metrics SET weight_lb = ? WHERE date = ?", (weight_lb, target_date.isoformat()))
        conn.commit()
    else:
        db.upsert(conn, "daily_metrics", {"date": target_date.isoformat(), "weight_lb": weight_lb})
        conn.commit()


def training_calendar_weeks(conn, today: Optional[date] = None, weeks: int = 26) -> dict:
    """GitHub-style: columns = weeks, rows = weekdays. Renders whatever
    range actually exists, up to `weeks` - days before real coverage began
    are marked so the template renders nothing there, not an empty cell."""
    today = today or config.snapshot_date()
    earliest_rows = db.fetch_all_dicts(conn, "SELECT MIN(date) as earliest FROM activities")
    earliest = earliest_rows[0]["earliest"] if earliest_rows and earliest_rows[0]["earliest"] else None
    earliest_date = _parse_date(earliest) if earliest else None

    requested_start = today - timedelta(weeks=weeks)
    start = max(requested_start, earliest_date) if earliest_date else requested_start
    start = start - timedelta(days=start.weekday())  # align to Monday for clean columns

    rows = db.fetch_all_dicts(
        conn, "SELECT date, bucket FROM activities WHERE date >= ? AND date <= ?",
        (start.isoformat(), today.isoformat()),
    )
    by_date: dict[str, set] = {}
    for r in rows:
        by_date.setdefault(r["date"], set()).add(r["bucket"])

    days = []
    d = start
    while d <= today:
        d_str = d.isoformat()
        days.append({
            "date": d_str,
            "buckets": sorted(by_date.get(d_str, [])),
            "before_coverage": earliest_date is not None and d < earliest_date,
        })
        d += timedelta(days=1)

    return {"start": start.isoformat(), "end": today.isoformat(), "days": days, "requested_weeks": weeks}


def sessions_by_month(conn, months: int = 6, today: Optional[date] = None) -> list[dict]:
    """Session counts by bucket, one row per calendar month, trailing N
    months (current month included, partial)."""
    today = today or config.snapshot_date()
    month_starts = []
    cursor = today.replace(day=1)
    for _ in range(months):
        month_starts.append(cursor)
        cursor = (cursor - timedelta(days=1)).replace(day=1)
    month_starts.reverse()

    rows = []
    for m_start in month_starts:
        next_month = (m_start.replace(day=28) + timedelta(days=4)).replace(day=1)
        m_end = min(next_month - timedelta(days=1), today)
        counts = db.fetch_all_dicts(
            conn, "SELECT bucket, COUNT(*) as n FROM activities WHERE date >= ? AND date <= ? GROUP BY bucket",
            (m_start.isoformat(), m_end.isoformat()),
        )
        by_bucket = {r["bucket"]: r["n"] for r in counts}
        rows.append({
            "month_label": m_start.strftime("%B"),
            "strength": by_bucket.get("strength", 0),
            "racquet": by_bucket.get("racquet", 0),
            "cardio": by_bucket.get("cardio", 0),
            "other": by_bucket.get("other", 0),
            "total": sum(by_bucket.values()),
        })
    return rows


def weekly_calories_dominant_bucket(wc: dict) -> Optional[dict]:
    """Which bucket carries the plurality of logged active calories across
    the displayed window - feeds the chart's computed-finding title."""
    totals = {b: sum(wc["data"][b]) for b in wc["buckets"]}
    grand_total = sum(totals.values())
    if grand_total <= 0:
        return None
    top_bucket = max(totals, key=totals.get)
    return {"bucket": top_bucket, "pct": round(totals[top_bucket] / grand_total * 100)}


# ---- Today dashboard v3 (calendar-month charts, no moving averages, one
# dominant bucket per day) - replaces the v2 30-day/26-week/weekly-calories
# set above for the dashboard specifically; those functions stay as-is for
# any other caller (e.g. the Activity page still uses the trailing-window
# versions). ------------------------------------------------------------

def _day_dominant_bucket(conn, start: date, end: date) -> dict:
    """One dominant bucket per day - strength beats racquet beats cardio -
    for charts that color a whole day by what was actually done, as
    distinct categories (not lumped together the way calendar_heatmap_data's
    strength==racquet tie does)."""
    rows = db.fetch_all_dicts(
        conn, "SELECT date, bucket FROM activities WHERE date >= ? AND date <= ?",
        (start.isoformat(), end.isoformat()),
    )
    priority = {"strength": 4, "racquet": 3, "cardio": 2, "other": 1}
    by_date: dict[str, str] = {}
    for r in rows:
        cur = by_date.get(r["date"])
        if cur is None or priority.get(r["bucket"], 0) > priority.get(cur, 0):
            by_date[r["date"]] = r["bucket"]
    return by_date


def steps_current_month(conn, goals: dict, today: Optional[date] = None) -> dict:
    """Daily steps for the current calendar month through snapshot_date - no
    moving average (removed per v3 spec, see dashboard-prototype-v3.html's
    dSteps()); just the goal line and the month's own average, both drawn
    directly as labeled reference lines rather than a legend."""
    today = today or config.snapshot_date()
    month_start, _ = _month_bounds(today)
    rows = db.fetch_all_dicts(
        conn, "SELECT date, steps FROM daily_metrics WHERE date >= ? AND date <= ? AND steps IS NOT NULL ORDER BY date",
        (month_start.isoformat(), today.isoformat()),
    )
    if not rows:
        return {"state": "insufficient"}

    by_date = {r["date"]: r["steps"] for r in rows}
    goal = goals["steps"]["daily_target"]
    days = []
    d = month_start
    while d <= today:
        days.append({"date": d.isoformat(), "steps": by_date.get(d.isoformat())})
        d += timedelta(days=1)

    values = [r["steps"] for r in rows]
    total = sum(values)
    avg = round(total / len(values))

    return {
        "state": "full",
        "days": days,
        "goal": goal,
        "month_total": total,
        "daily_avg": avg,
        "days_at_goal": sum(1 for v in values if v >= goal),
        "days_in_period": len(values),
        "best_day": max(values),
        "pct_vs_goal": round((avg - goal) / goal * 100),
        "range_start": month_start.isoformat(),
        "range_end": today.isoformat(),
    }


def daily_calories_current_month(conn, today: Optional[date] = None) -> dict:
    """Daily active calories for the current calendar month, each day
    colored by its own dominant session bucket (or none = rest day) - per
    v3, calories "follow your sessions, not your steps"."""
    today = today or config.snapshot_date()
    month_start, _ = _month_bounds(today)
    rows = db.fetch_all_dicts(
        conn, "SELECT date, active_calories FROM daily_metrics WHERE date >= ? AND date <= ? "
              "AND active_calories IS NOT NULL ORDER BY date",
        (month_start.isoformat(), today.isoformat()),
    )
    if not rows:
        return {"state": "insufficient"}

    dominant = _day_dominant_bucket(conn, month_start, today)
    days = [
        {"date": r["date"], "active_calories": r["active_calories"], "bucket": dominant.get(r["date"])}
        for r in rows
    ]
    values = [r["active_calories"] for r in rows]
    total = sum(values)
    avg = round(total / len(values))
    rest_values = [d["active_calories"] for d in days if d["bucket"] is None]

    counts = {"strength": 0, "racquet": 0, "cardio": 0, "other": 0, "rest": 0}
    for d in days:
        counts[d["bucket"] or "rest"] += 1

    return {
        "state": "full",
        "days": days,
        "month_total": total,
        "daily_avg": avg,
        "best_day": max(values),
        "rest_day_avg": round(sum(rest_values) / len(rest_values)) if rest_values else None,
        "counts": counts,
        "range_start": month_start.isoformat(),
        "range_end": today.isoformat(),
    }


def monthly_steps_bars(conn, goals: dict, today: Optional[date] = None, months: int = 6) -> list[dict]:
    """Total steps by calendar month, trailing N months, each against that
    month's own target (goals.yaml's monthly_targets lookup falling back to
    monthly_default) - a deliberately lower target some month must never
    read as a shortfall."""
    today = today or config.snapshot_date()
    month_starts = []
    cursor = today.replace(day=1)
    for _ in range(months):
        month_starts.append(cursor)
        cursor = (cursor - timedelta(days=1)).replace(day=1)
    month_starts.reverse()

    result = []
    for m_start in month_starts:
        next_month = (m_start.replace(day=28) + timedelta(days=4)).replace(day=1)
        m_end = min(next_month - timedelta(days=1), today)
        rows = db.fetch_all_dicts(
            conn, "SELECT SUM(steps) as total FROM daily_metrics WHERE date >= ? AND date <= ? AND steps IS NOT NULL",
            (m_start.isoformat(), m_end.isoformat()),
        )
        total = round(rows[0]["total"] or 0)
        target = _steps_target_for_month(goals, m_start)
        result.append({
            "month_label": m_start.strftime("%b"),
            "steps": total,
            "target": target,
            "cleared": total >= target,
        })
    return result


def monthly_calories_by_source(conn, today: Optional[date] = None, months: int = 6) -> list[dict]:
    """Monthly active calories split into racquet/strength/cardio (logged
    sessions) plus "unlogged movement" - total active calories minus what
    sessions accounted for - so where the burn actually comes from is
    visible, not just how much."""
    today = today or config.snapshot_date()
    month_starts = []
    cursor = today.replace(day=1)
    for _ in range(months):
        month_starts.append(cursor)
        cursor = (cursor - timedelta(days=1)).replace(day=1)
    month_starts.reverse()

    result = []
    for m_start in month_starts:
        next_month = (m_start.replace(day=28) + timedelta(days=4)).replace(day=1)
        m_end = min(next_month - timedelta(days=1), today)

        session_rows = db.fetch_all_dicts(
            conn, "SELECT bucket, SUM(calories) as total FROM activities "
                  "WHERE date >= ? AND date <= ? AND calories IS NOT NULL GROUP BY bucket",
            (m_start.isoformat(), m_end.isoformat()),
        )
        by_bucket = {r["bucket"]: round(r["total"] or 0) for r in session_rows}

        total_rows = db.fetch_all_dicts(
            conn, "SELECT SUM(active_calories) as total FROM daily_metrics "
                  "WHERE date >= ? AND date <= ? AND active_calories IS NOT NULL",
            (m_start.isoformat(), m_end.isoformat()),
        )
        month_total = round(total_rows[0]["total"] or 0)
        # "other" (yoga, golf, ...) is still a logged session - only calories
        # from no session at all belong in "unlogged".
        logged = sum(v for k, v in by_bucket.items() if k in ("strength", "racquet", "cardio", "other"))

        result.append({
            "month_label": m_start.strftime("%b"),
            "racquet": by_bucket.get("racquet", 0),
            "strength": by_bucket.get("strength", 0),
            "cardio": by_bucket.get("cardio", 0),
            "other": by_bucket.get("other", 0),
            "unlogged": max(month_total - logged, 0),
            "total": month_total,
        })
    return result


def current_month_calendar(conn, today: Optional[date] = None) -> dict:
    """One cell per day of the current month through snapshot_date - the
    month isn't over, so future days simply don't exist yet, no placeholder
    cells for them (replaces the 26-week training_calendar_weeks on the
    dashboard). Colored by dominant bucket; also returns the longest
    training streak and longest rest gap for the chart's own subtitle."""
    today = today or config.snapshot_date()
    month_start, _ = _month_bounds(today)
    dominant = _day_dominant_bucket(conn, month_start, today)

    days = []
    d = month_start
    while d <= today:
        days.append({"date": d.isoformat(), "bucket": dominant.get(d.isoformat())})
        d += timedelta(days=1)

    counts = {"strength": 0, "racquet": 0, "cardio": 0, "other": 0, "rest": 0}
    longest_streak = longest_gap = cur_streak = cur_gap = 0
    for day in days:
        counts[day["bucket"] or "rest"] += 1
        if day["bucket"]:
            cur_streak += 1
            cur_gap = 0
        else:
            cur_gap += 1
            cur_streak = 0
        longest_streak = max(longest_streak, cur_streak)
        longest_gap = max(longest_gap, cur_gap)

    return {
        "month_start": month_start.isoformat(),
        "days": days,
        "counts": counts,
        "longest_streak": longest_streak,
        "longest_gap": longest_gap,
    }


def recovery_summary(readiness: dict, rhr_elevation: dict, acwr: dict, yesterday: dict) -> dict:
    """Deterministic bullets + a one-line verdict for the Recovery module.
    No LLM call happens on page load, so this has to be built purely from
    fields already computed elsewhere in build_snapshot (readiness_today,
    resting_hr_elevation, acute_chronic_load_ratio, yesterday_summary) -
    never a fresh query of its own (rule 2: build_snapshot is the only
    function that computes a metric)."""
    bullets = []

    if rhr_elevation.get("elevated"):
        bullets.append({
            "cls": "bad",
            "text": f"Resting HR is {rhr_elevation['current']} bpm, {rhr_elevation['diff']} above your "
                    f"{rhr_elevation['baseline']} baseline, and has stayed elevated "
                    f"{rhr_elevation['consecutive_days']} days running. Two days is the usual flag.",
        })
    elif rhr_elevation.get("current") is not None and rhr_elevation.get("baseline") is not None:
        bullets.append({
            "cls": "good",
            "text": f"Resting HR is {rhr_elevation['current']} bpm against a "
                    f"{rhr_elevation['baseline']} baseline — within normal range.",
        })
    else:
        bullets.append({"cls": "info", "text": "Resting HR baseline unavailable — not enough synced history yet."})

    sleep_delta = yesterday.get("sleep_hours_delta")
    if yesterday.get("sleep_hours") is not None and sleep_delta is not None:
        sign = "+" if sleep_delta >= 0 else ""
        bullets.append({
            "cls": "warn" if sleep_delta < -1.0 else "good",
            "text": f"Sleep was {yesterday['sleep_hours']}h last night, {sign}{sleep_delta}h vs. your "
                    f"{yesterday['sleep_hours_avg_month']}h month average.",
        })

    if readiness.get("state") == "unknown":
        bullets.append({
            "cls": "info",
            "text": f"Body Battery and sleep score are missing — {readiness.get('unknown_reason', 'no recent sync')}.",
        })

    ratio = acwr.get("ratio")
    if ratio is not None:
        flag_high = bool(acwr.get("flag_high"))
        bullets.append({
            "cls": "bad" if flag_high else "good",
            "text": f"Acute:chronic load is {ratio}, {'above' if flag_high else 'well below'} the 1.5 overload line"
                    + (" — training volume may be the driver." if flag_high else ", so total volume isn't the problem."),
        })

    elevated = bool(rhr_elevation.get("elevated"))
    high_load = bool(acwr.get("flag_high"))
    if elevated and high_load:
        verdict = ("Resting HR is elevated and training load is high - the clearest overreaching signal "
                   "available here. Take a lighter day.")
    elif elevated:
        baseline = rhr_elevation.get("baseline")
        verdict = "Elevated resting HR with flat training load usually means sleep debt or illness, not overtraining."
        if baseline is not None:
            verdict += f" Train if you feel fine, but keep it moderate until resting HR drops back under {baseline}."
    elif high_load:
        verdict = "Training load is elevated but resting HR hasn't followed yet - watch the next two days."
    else:
        verdict = "No recovery flags today - resting HR and training load are both within normal range."

    return {"bullets": bullets, "verdict": verdict}


# ---- Nutrition tab (from a manually-imported MyFitnessPal export - see
# scripts/import_myfitnesspal.py) - MyFitnessPal has no public API, so this
# data has no live sync counterpart and only updates when the user
# re-exports and re-runs the importer. Kept off the coach's single-snapshot
# contract (build_snapshot) on purpose - this is a standalone descriptive
# page, not part of the daily brief. -----------------------------------

MEAL_ORDER = ["Breakfast", "Lunch", "Dinner", "Snacks"]


def nutrition_daily_series(conn, days: int = 60, today: Optional[date] = None) -> dict:
    """Daily nutrition totals for the charting window. state: insufficient
    if nothing has been imported yet."""
    today = today or config.snapshot_date()
    start = today - timedelta(days=days - 1)
    rows = db.fetch_all_dicts(
        conn, "SELECT * FROM nutrition_daily WHERE date >= ? AND date <= ? ORDER BY date",
        (start.isoformat(), today.isoformat()),
    )
    if not rows:
        return {"state": "insufficient"}

    values = [r["calories"] for r in rows]
    return {
        "state": "full",
        "days": rows,
        "n_days": len(rows),
        "range_start": rows[0]["date"],
        "range_end": rows[-1]["date"],
        "avg_calories": round(sum(values) / len(values)),
        "avg_protein": round(sum(r["protein_g"] for r in rows) / len(rows)),
        "avg_carbs": round(sum(r["carbs_g"] for r in rows) / len(rows)),
        "avg_fat": round(sum(r["fat_g"] for r in rows) / len(rows)),
        "avg_sodium": round(sum(r["sodium_mg"] for r in rows) / len(rows)),
        "best_day_calories": round(max(values)),
        "lowest_day_calories": round(min(values)),
    }


def meal_breakdown(conn, days: int = 60, today: Optional[date] = None) -> dict:
    """% of calories by meal (Breakfast/Lunch/Dinner/Snacks) across the
    window - where the calories actually come from in the day."""
    today = today or config.snapshot_date()
    start = today - timedelta(days=days - 1)
    rows = db.fetch_all_dicts(
        conn, "SELECT meal, SUM(calories) as total FROM nutrition_meals WHERE date >= ? AND date <= ? GROUP BY meal",
        (start.isoformat(), today.isoformat()),
    )
    if not rows:
        return {"state": "insufficient"}

    totals = {r["meal"]: round(r["total"]) for r in rows}
    grand_total = sum(totals.values())
    meals = [
        {"meal": m, "calories": totals[m], "pct": round(totals[m] / grand_total * 100) if grand_total else 0}
        for m in MEAL_ORDER if m in totals
    ]
    return {"state": "full", "meals": meals, "total": grand_total}


def protein_per_bodyweight(conn, days: int = 60, today: Optional[date] = None) -> dict:
    """Daily protein_g / weight_lb (nearest prior-day weight if that exact
    day has none) - a standard, real fitness metric. Shown as the athlete's
    own trend only; no external target line unless goals.yaml ever defines
    one, since none does today."""
    today = today or config.snapshot_date()
    start = today - timedelta(days=days - 1)
    nutrition_rows = db.fetch_all_dicts(
        conn, "SELECT date, protein_g FROM nutrition_daily WHERE date >= ? AND date <= ? ORDER BY date",
        (start.isoformat(), today.isoformat()),
    )
    if not nutrition_rows:
        return {"state": "insufficient"}

    weight_rows = db.fetch_all_dicts(
        conn, "SELECT date, weight_lb FROM daily_metrics WHERE date >= ? AND date <= ? AND weight_lb IS NOT NULL ORDER BY date",
        ((start - timedelta(days=30)).isoformat(), today.isoformat()),
    )
    weight_by_date = {r["date"]: r["weight_lb"] for r in weight_rows}
    sorted_weight_dates = sorted(weight_by_date)

    points = []
    for r in nutrition_rows:
        d = r["date"]
        w = weight_by_date.get(d)
        if w is None:
            prior = [wd for wd in sorted_weight_dates if wd <= d]
            w = weight_by_date[prior[-1]] if prior else None
        if w:
            points.append({"date": d, "ratio": round(r["protein_g"] / w, 2)})

    if not points:
        return {"state": "insufficient"}
    return {
        "state": "full",
        "points": points,
        "current": points[-1]["ratio"],
        "avg": round(sum(p["ratio"] for p in points) / len(points), 2),
    }


def nutrition_training_vs_rest(conn, today: Optional[date] = None) -> dict:
    """Reuses stats_engine's already-validated Hedges' g + bootstrap
    comparison (same n>=10-per-group gate the Insights page holds itself
    to) to check whether calorie/protein intake differs on training days
    vs. rest days. Honestly reports insufficient if there isn't enough
    nutrition history yet for either group to clear that bar - expected
    with only a few weeks of MyFitnessPal history."""
    from garmin_tracker import stats_engine

    rows = db.fetch_all_dicts(conn, "SELECT date, calories, protein_g FROM nutrition_daily ORDER BY date")
    if not rows:
        return {"state": "insufficient"}

    dates = [r["date"] for r in rows]
    start, end = min(dates), max(dates)
    activity_dates = {
        r["date"] for r in db.fetch_all_dicts(
            conn, "SELECT DISTINCT date FROM activities WHERE date >= ? AND date <= ?", (start, end)
        )
    }

    comparisons = {}
    for metric, label in (("calories", "Calories"), ("protein_g", "Protein")):
        training = np.array([r[metric] for r in rows if r["date"] in activity_dates], dtype=float)
        rest = np.array([r[metric] for r in rows if r["date"] not in activity_dates], dtype=float)
        result = stats_engine.compare_training_vs_rest(training, rest)
        result["label"] = label
        result["training_avg"] = round(float(training.mean()), 1) if len(training) else None
        result["rest_avg"] = round(float(rest.mean()), 1) if len(rest) else None
        comparisons[metric] = result

    return {"state": "full", "comparisons": comparisons}


def weight_and_calories_series(conn, days: int = 60, today: Optional[date] = None) -> dict:
    """Trend weight + daily calories over the overlapping window, for a
    side-by-side visual only - no correlation coefficient, no causal
    language. Matches this project's own rule (see stats_engine.py):
    brute-force correlation from a single subject's few weeks of data is
    worse than nothing - the athlete's own eye does the comparing here."""
    today = today or config.snapshot_date()
    start = today - timedelta(days=days - 1)

    nutrition_rows = db.fetch_all_dicts(
        conn, "SELECT date, calories FROM nutrition_daily WHERE date >= ? AND date <= ? ORDER BY date",
        (start.isoformat(), today.isoformat()),
    )
    weight_rows = db.fetch_all_dicts(
        conn, "SELECT date, weight_lb FROM daily_metrics WHERE date >= ? AND date <= ? AND weight_lb IS NOT NULL ORDER BY date",
        (start.isoformat(), today.isoformat()),
    )
    if not nutrition_rows or not weight_rows:
        return {"state": "insufficient"}

    return {
        "state": "full",
        "calories": nutrition_rows,
        "weight": weight_rows,
        "range_start": min(nutrition_rows[0]["date"], weight_rows[0]["date"]),
        "range_end": today.isoformat(),
    }


# A day (or week/month average) doesn't need to be a strict pass/fail
# against a nutrient goal - 15% short of a minimum or 15% over a maximum
# reads as "near", not "under"/"over", so a single slightly-off day doesn't
# paint the whole picture red. Only genuinely missing the target by more
# than that reads as the harder under/over state.
NUTRIENT_NEAR_BAND = 0.15


def _nutrient_status(value: float, goal: float, direction: str) -> str:
    if direction == "min":
        if value >= goal:
            return "good"
        return "near" if value >= goal * (1 - NUTRIENT_NEAR_BAND) else "under"
    if value <= goal:
        return "good"
    return "near" if value <= goal * (1 + NUTRIENT_NEAR_BAND) else "over"


# (key, label, unit, goals.yaml key or None, direction "min"/"max"). The 4
# %DV nutrients have no goals.yaml key - 100% *is* the reference by
# definition of %DV, nothing to look up. See goals.yaml's nutrition section
# for which numbers are the user's own vs. a researched FDA Daily Value.
NUTRIENT_SPECS = [
    ("protein_g", "Protein", "g", "protein_g_min", "min"),
    ("fat_g", "Fat", "g", "fat_g_min", "min"),
    ("sugar_g", "Sugar", "g", "sugar_g_max", "max"),
    ("saturated_fat_g", "Saturated fat", "g", "saturated_fat_g_max", "max"),
    ("fiber_g", "Fiber", "g", "fiber_g_min", "min"),
    ("sodium_mg", "Sodium", "mg", "sodium_mg_max", "max"),
    ("potassium_mg", "Potassium", "mg", "potassium_mg_min", "min"),
    ("cholesterol_mg", "Cholesterol", "mg", "cholesterol_mg_max", "max"),
    ("vitamin_a_pct", "Vitamin A", "% DV", None, "min"),
    ("vitamin_c_pct", "Vitamin C", "% DV", None, "min"),
    ("calcium_pct", "Calcium", "% DV", None, "min"),
    ("iron_pct", "Iron", "% DV", None, "min"),
]


def nutrition_window_summary(conn, goals: dict, window: str, today: Optional[date] = None) -> dict:
    """Every tracked nutrient vs. its goal for one of three views:
    "day" (the most recent logged day, a raw total), "week" (rolling last 7
    days, averaged per day), "month" (calendar month to date, averaged per
    day). Averaging per day (not summing) is what makes the week/month
    views comparable to the same daily goal a single day is checked
    against."""
    today = today or config.snapshot_date()
    nutrition_goals = goals.get("nutrition", {})

    if window == "day":
        rows = db.fetch_all_dicts(
            conn, "SELECT * FROM nutrition_daily WHERE date <= ? ORDER BY date DESC LIMIT 1",
            (today.isoformat(),),
        )
        is_average = False
        range_label = rows[0]["date"] if rows else None
    elif window == "week":
        start = today - timedelta(days=6)
        rows = db.fetch_all_dicts(
            conn, "SELECT * FROM nutrition_daily WHERE date >= ? AND date <= ? ORDER BY date",
            (start.isoformat(), today.isoformat()),
        )
        is_average = True
        range_label = f"{start.isoformat()} – {today.isoformat()}"
    elif window == "month":
        month_start = today.replace(day=1)
        rows = db.fetch_all_dicts(
            conn, "SELECT * FROM nutrition_daily WHERE date >= ? AND date <= ? ORDER BY date",
            (month_start.isoformat(), today.isoformat()),
        )
        is_average = True
        range_label = f"{month_start.isoformat()} – {today.isoformat()}"
    else:
        raise ValueError(f"unknown window: {window!r}")

    if not rows:
        return {"state": "insufficient", "window": window}

    n = len(rows)
    nutrients = []
    for key, label, unit, goal_key, direction in NUTRIENT_SPECS:
        total = sum(r[key] or 0 for r in rows)
        value = round(total / n, 1) if is_average else round(total, 1)
        goal = 100 if goal_key is None else nutrition_goals.get(goal_key)
        status = _nutrient_status(value, goal, direction) if goal is not None else None
        nutrients.append({
            "key": key, "label": label, "unit": unit, "value": value,
            "goal": goal, "direction": direction, "status": status,
        })

    cal_total = sum(r["calories"] or 0 for r in rows)
    cal_value = round(cal_total / n) if is_average else round(cal_total)

    return {
        "state": "full",
        "window": window,
        "n_days": n,
        "range_label": range_label,
        "is_average": is_average,
        "calories": {"value": cal_value, "target": nutrition_goals.get("calorie_avg_target")},
        "nutrients": nutrients,
    }


def calorie_catch_up(conn, goals: dict, today: Optional[date] = None) -> Optional[dict]:
    """If the goal is to keep this calendar week's (Mon-Sun) daily average
    at calorie_avg_target, what should the average be for the *remaining*
    days, given what's already logged? Same remaining/days_remaining shape
    as the pace rails, just applied to a week instead of a month. Returns
    None if there's no calorie_avg_target set, nothing logged yet this
    week, or the week is already over."""
    today = today or config.snapshot_date()
    target = goals.get("nutrition", {}).get("calorie_avg_target")
    if target is None:
        return None

    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)
    rows = db.fetch_all_dicts(
        conn, "SELECT date, calories FROM nutrition_daily WHERE date >= ? AND date <= ?",
        (week_start.isoformat(), today.isoformat()),
    )
    days_elapsed = (today - week_start).days + 1
    days_remaining = (week_end - today).days
    n_logged = len(rows)
    if n_logged == 0 or days_remaining <= 0:
        return None

    logged_total = sum(r["calories"] for r in rows)
    remaining_budget = target * 7 - logged_total

    return {
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "days_elapsed": days_elapsed,
        "days_remaining": days_remaining,
        "n_logged": n_logged,
        # If a day this week wasn't logged at all, it's silently excluded
        # from logged_total rather than assumed to be zero calories - the
        # required-average number below is only as good as that gap is
        # small, which fully_logged tells the caller honestly.
        "fully_logged": n_logged == days_elapsed,
        "logged_avg_so_far": round(logged_total / n_logged),
        "required_avg_remaining": round(remaining_budget / days_remaining),
        "target": target,
    }


def expected_weight_from_calories(conn, goals: dict, today: Optional[date] = None) -> dict:
    """Energy-balance weight estimate - a different thing from
    trend_weight()'s pure logged-weight trend (which never looks at
    calories). Anchors on trend_weight()'s own EWMA - the trusted smoothed
    number shown everywhere else on this dashboard - then walks forward
    day by day accumulating (calories_in - calories_out) / 3500 lb for
    every day with BOTH a real nutrition_daily.calories and a real
    daily_metrics.total_calories. total_calories is Garmin's own
    totalKilocalories (BMR + activity already combined - confirmed against
    real data), never added to active_calories separately, which would
    double-count the workout portion. A day missing either value is
    skipped, not zero-filled - "disregard nulls" per the ask. The 3,500
    kcal/lb rule is a standard approximation (water weight, input error on
    both sides), so this is an estimate, not a forecast - the caller
    should label it that way, not just this docstring."""
    today = today or config.snapshot_date()
    trend = trend_weight(conn, goals, today)
    anchor_weight = trend.get("trend_weight_lb")
    if anchor_weight is None:
        return {"state": "insufficient"}

    anchor_rows = db.fetch_all_dicts(
        conn, "SELECT date FROM daily_metrics WHERE weight_lb IS NOT NULL AND date <= ? ORDER BY date DESC LIMIT 1",
        (today.isoformat(),),
    )
    anchor_date = anchor_rows[0]["date"]

    rows = db.fetch_all_dicts(
        conn,
        "SELECT nd.date, nd.calories as calories_in, dm.total_calories as calories_out "
        "FROM nutrition_daily nd JOIN daily_metrics dm ON dm.date = nd.date "
        "WHERE nd.date > ? AND nd.date <= ? AND nd.calories IS NOT NULL AND dm.total_calories IS NOT NULL "
        "ORDER BY nd.date",
        (anchor_date, today.isoformat()),
    )

    result = {
        "state": "full" if rows else "insufficient",
        "anchor_date": anchor_date,
        "anchor_weight_lb": anchor_weight,
        "days_in_window": (today - _parse_date(anchor_date)).days,
        "days_with_both_values": len(rows),
    }
    if not rows:
        return result

    accumulated_lb = sum(r["calories_in"] - r["calories_out"] for r in rows) / 3500
    expected_weight_today = round(anchor_weight + accumulated_lb, 1)
    result["expected_weight_today_lb"] = expected_weight_today

    # "If this week keeps going like it has been" - need at least 3
    # qualifying days in the trailing week to say anything (same
    # don't-overreact-to-one-day instinct as the nutrient near-band).
    week_lookback_start = (today - timedelta(days=6)).isoformat()
    week_rows = [r for r in rows if r["date"] >= week_lookback_start]
    if len(week_rows) >= 3:
        avg_net = sum(r["calories_in"] - r["calories_out"] for r in week_rows) / len(week_rows)
        avg_daily_net_lb = avg_net / 3500
        iso_week_start = today - timedelta(days=today.weekday())
        week_end = iso_week_start + timedelta(days=6)
        days_remaining = (week_end - today).days
        result.update({
            "week_avg_daily_net_lb": round(avg_daily_net_lb, 3),
            "days_with_both_values_this_week": len(week_rows),
            "week_end_date": week_end.isoformat(),
            "projected_end_of_week_lb": round(expected_weight_today + avg_daily_net_lb * days_remaining, 1),
        })
    return result
