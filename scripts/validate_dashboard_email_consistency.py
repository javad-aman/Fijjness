"""Regression test: the dashboard snapshot, the email/coach snapshot, and a
direct SQL query must all return identical values for the shared metrics
(steps, strength count, racquet count, active calories, ACWR, readiness
state). This is what prevents the three-reports-three-answers problem
(dashboard says 10 strength sessions, email says 2) from coming back.

Builds an ephemeral, in-memory copy of a real week's rows from whichever
database is currently configured (local SQLite or Turso, same as everything
else in this project) - real data, never a committed fixture, since
data/garmin.db is deliberately git-ignored personal health data. The copy is
frozen for the duration of this run so a sync happening mid-test can't
introduce a spurious mismatch unrelated to an actual bug.

Run before every deploy: python scripts/validate_dashboard_email_consistency.py
"""
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from garmin_tracker import analytics, coach, config, db

FIXTURE_TABLES = ["daily_metrics", "activities", "training_status", "intraday_steps"]
HISTORY_DAYS = 90  # enough trailing context for the 28d ACWR window + month-to-date strength pace
CHECK_DAYS = 7      # "one week" of target dates to check, per the spec

failures = []


def check(name: str, condition: bool, detail: str = ""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" - {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def build_fixture(source_conn, today) -> sqlite3.Connection:
    """Copies HISTORY_DAYS of real rows from the live database into a fresh
    in-memory SQLite database, so both computation paths below read from an
    identical, frozen snapshot."""
    fixture = sqlite3.connect(":memory:")
    fixture.row_factory = sqlite3.Row
    fixture.executescript(db.SCHEMA)

    start = (today - timedelta(days=HISTORY_DAYS)).isoformat()
    end = today.isoformat()
    for table in FIXTURE_TABLES:
        rows = db.fetch_all_dicts(
            source_conn, f"SELECT * FROM {table} WHERE date >= ? AND date <= ?", (start, end)
        )
        for row in rows:
            db.upsert(fixture, table, row)
    fixture.commit()
    return fixture


def direct_sql_strength_count(conn, target_date) -> int:
    month_start = target_date.replace(day=1)
    return db.fetch_all_dicts(
        conn, "SELECT COUNT(*) as n FROM activities WHERE date >= ? AND date <= ? AND bucket = 'strength'",
        (month_start.isoformat(), target_date.isoformat()),
    )[0]["n"]


def direct_sql_racquet_count(conn, target_date) -> int:
    week_start = target_date - timedelta(days=target_date.weekday())
    return db.fetch_all_dicts(
        conn, "SELECT COUNT(*) as n FROM activities WHERE date >= ? AND date <= ? AND bucket = 'racquet'",
        (week_start.isoformat(), target_date.isoformat()),
    )[0]["n"]


def direct_sql_steps(conn, target_date):
    rows = db.fetch_all_dicts(conn, "SELECT steps FROM daily_metrics WHERE date = ?", (target_date.isoformat(),))
    return rows[0]["steps"] if rows else None


def direct_sql_active_calories(conn, target_date):
    rows = db.fetch_all_dicts(conn, "SELECT active_calories FROM daily_metrics WHERE date = ?", (target_date.isoformat(),))
    return rows[0]["active_calories"] if rows else None


def direct_sql_acwr(conn, target_date):
    acute_rows = db.fetch_all_dicts(
        conn, "SELECT training_load FROM activities WHERE date >= ? AND date <= ?",
        ((target_date - timedelta(days=6)).isoformat(), target_date.isoformat()),
    )
    chronic_rows = db.fetch_all_dicts(
        conn, "SELECT training_load FROM activities WHERE date >= ? AND date <= ?",
        ((target_date - timedelta(days=27)).isoformat(), target_date.isoformat()),
    )
    acute = sum(r["training_load"] for r in acute_rows if r["training_load"] is not None)
    chronic = sum(r["training_load"] for r in chronic_rows if r["training_load"] is not None)
    chronic_weekly_avg = chronic / 4 if chronic else 0.0
    return round(acute / chronic_weekly_avg, 2) if chronic_weekly_avg else None


def direct_readiness_state(conn, target_date):
    rows = db.fetch_all_dicts(conn, "SELECT * FROM daily_metrics WHERE date = ?", (target_date.isoformat(),))
    row = rows[0] if rows else {}
    baseline_rows = db.fetch_all_dicts(
        conn, "SELECT resting_hr FROM daily_metrics WHERE date >= ? AND date < ? AND resting_hr IS NOT NULL",
        ((target_date - timedelta(days=30)).isoformat(), target_date.isoformat()),
    )
    baseline = sum(r["resting_hr"] for r in baseline_rows) / len(baseline_rows) if baseline_rows else None
    return analytics.readiness(
        row.get("body_battery_wake"), row.get("hrv_status"), row.get("sleep_score"),
        row.get("resting_hr"), baseline,
    )["state"]


def main():
    with db.connect() as source_conn:
        today = config.local_today()
        fixture = build_fixture(source_conn, today)

    print(f"Fixture built from real data: {HISTORY_DAYS} trailing days through {today.isoformat()}\n")

    for offset in range(1, CHECK_DAYS + 1):
        target = today - timedelta(days=offset)
        print(f"=== {target.isoformat()} ===")

        dashboard = analytics.build_snapshot(fixture, config.GOALS, target)
        email = coach.build_metrics_snapshot(fixture, config.GOALS, target)

        sql_steps = direct_sql_steps(fixture, target)
        dash_steps = dashboard["steps_pace"]["actual"]
        steps_match = (
            (sql_steps is None and dash_steps is None)
            or (sql_steps is not None and dash_steps is not None and round(sql_steps / 100) * 100 == dash_steps)
        )
        check(f"steps ({target}): dashboard rounds direct SQL correctly", steps_match,
              f"sql={sql_steps} dashboard_rounded={dash_steps}")

        sql_strength = direct_sql_strength_count(fixture, target)
        check(f"strength count ({target}): dashboard == email == direct SQL",
              dashboard["strength_pace"]["actual"] == email["strength_pace"]["actual"] == sql_strength,
              f"dashboard={dashboard['strength_pace']['actual']} email={email['strength_pace']['actual']} sql={sql_strength}")

        sql_racquet = direct_sql_racquet_count(fixture, target)
        check(f"racquet count ({target}): dashboard == email == direct SQL",
              dashboard["racquet_pace"]["actual"] == email["racquet_pace"]["actual"] == sql_racquet,
              f"dashboard={dashboard['racquet_pace']['actual']} email={email['racquet_pace']['actual']} sql={sql_racquet}")

        sql_active_cal = direct_sql_active_calories(fixture, target)
        yesterday_view = analytics.yesterday_summary(fixture, target + timedelta(days=1))
        check(f"active_calories ({target}): yesterday_summary == direct SQL",
              yesterday_view["active_calories"] == sql_active_cal,
              f"yesterday_summary={yesterday_view['active_calories']} sql={sql_active_cal}")

        sql_acwr = direct_sql_acwr(fixture, target)
        check(f"ACWR ({target}): dashboard == email == direct SQL",
              dashboard["acute_chronic_load_ratio"]["ratio"] == email["acute_chronic_load_ratio"]["ratio"] == sql_acwr,
              f"dashboard={dashboard['acute_chronic_load_ratio']['ratio']} email={email['acute_chronic_load_ratio']['ratio']} sql={sql_acwr}")

        sql_readiness = direct_readiness_state(fixture, target)
        check(f"readiness state ({target}): dashboard == email == direct recompute",
              dashboard["readiness"]["state"] == email["readiness"]["state"] == sql_readiness,
              f"dashboard={dashboard['readiness']['state']} email={email['readiness']['state']} sql={sql_readiness}")

        print()

    fixture.close()

    if failures:
        print(f"FAILED: {len(failures)} check(s): {failures}")
        sys.exit(1)
    print("All checks passed - dashboard, email, and direct SQL agree across the last week.")


if __name__ == "__main__":
    main()
