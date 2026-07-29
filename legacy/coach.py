"""Pure functions that turn raw DB rows into coach-style trends and
goal-gap numbers. Kept separate from email rendering/sending so it's
testable without mailing anything.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any

from garmin_tracker import goals


def _avg(conn: sqlite3.Connection, table: str, column: str, start: date, end: date) -> float | None:
    row = conn.execute(
        f"SELECT AVG({column}) FROM {table} WHERE date >= ? AND date <= ?",
        (start.isoformat(), end.isoformat()),
    ).fetchone()
    return row[0]


def _workout_count(conn: sqlite3.Connection, start: date, end: date) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM activities WHERE date(start_time) >= ? AND date(start_time) <= ?",
        (start.isoformat(), end.isoformat()),
    ).fetchone()
    return row[0] or 0


def _yesterday_row(conn: sqlite3.Connection, d: date) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM daily_stats WHERE date = ?", (d.isoformat(),)
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def _yesterday_sleep(conn: sqlite3.Connection, d: date) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM sleep WHERE date = ?", (d.isoformat(),)
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def build_report(conn: sqlite3.Connection, today: date | None = None) -> dict[str, Any]:
    """Compute yesterday's snapshot, 7-day trends, and goal gaps."""
    today = today or date.today()
    yesterday = today - timedelta(days=1)

    last7_start = today - timedelta(days=7)
    last7_end = today - timedelta(days=1)
    prev7_start = today - timedelta(days=14)
    prev7_end = today - timedelta(days=8)

    steps_avg_7d = _avg(conn, "daily_stats", "steps", last7_start, last7_end)
    steps_avg_prev7d = _avg(conn, "daily_stats", "steps", prev7_start, prev7_end)

    resting_hr_avg_7d = _avg(conn, "daily_stats", "resting_hr", last7_start, last7_end)
    resting_hr_avg_prev7d = _avg(conn, "daily_stats", "resting_hr", prev7_start, prev7_end)

    stress_avg_7d = _avg(conn, "daily_stats", "stress_avg", last7_start, last7_end)

    sleep_score_avg_7d = _avg(conn, "sleep", "sleep_score", last7_start, last7_end)
    sleep_hours_avg_7d = _avg(conn, "sleep", "duration_sec", last7_start, last7_end)
    if sleep_hours_avg_7d is not None:
        sleep_hours_avg_7d = sleep_hours_avg_7d / 3600.0

    workouts_this_week = _workout_count(conn, last7_start, last7_end)

    return {
        "today": today,
        "yesterday": yesterday,
        "yesterday_stats": _yesterday_row(conn, yesterday),
        "yesterday_sleep": _yesterday_sleep(conn, yesterday),
        "steps_avg_7d": steps_avg_7d,
        "steps_avg_prev7d": steps_avg_prev7d,
        "steps_goal": goals.DAILY_STEPS_GOAL,
        "steps_gap": (steps_avg_7d - goals.DAILY_STEPS_GOAL) if steps_avg_7d is not None else None,
        "resting_hr_avg_7d": resting_hr_avg_7d,
        "resting_hr_avg_prev7d": resting_hr_avg_prev7d,
        "resting_hr_target": goals.RESTING_HR_TARGET,
        "stress_avg_7d": stress_avg_7d,
        "stress_max_goal": goals.STRESS_AVG_MAX,
        "sleep_score_avg_7d": sleep_score_avg_7d,
        "sleep_hours_avg_7d": sleep_hours_avg_7d,
        "sleep_hours_goal": goals.SLEEP_HOURS_GOAL,
        "sleep_hours_gap": (sleep_hours_avg_7d - goals.SLEEP_HOURS_GOAL) if sleep_hours_avg_7d is not None else None,
        "workouts_this_week": workouts_this_week,
        "workouts_goal": goals.WORKOUTS_PER_WEEK_GOAL,
        "workouts_gap": workouts_this_week - goals.WORKOUTS_PER_WEEK_GOAL,
    }


def trend_arrow(current: float | None, previous: float | None, threshold: float = 0.5) -> str:
    """Directional arrow for current vs previous — purely descriptive,
    doesn't judge whether the direction is "good" (that depends on the metric)."""
    if current is None or previous is None:
        return ""
    delta = current - previous
    if abs(delta) < threshold:
        return "→"
    return "↑" if delta > 0 else "↓"
