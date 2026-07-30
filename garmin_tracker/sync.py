"""Pull data from Garmin Connect into the database (local SQLite or Turso).

Usage:
    python -m garmin_tracker.sync                # incremental sync (default)
    python -m garmin_tracker.sync --full          # full historical backfill
    python -m garmin_tracker.sync --full --days 90
"""
from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, timedelta

from garmin_tracker import config, db, models
from garmin_tracker.garmin_client import call_with_retry, connect, pace

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SOURCES = ["daily_metrics", "activities", "training_status", "intraday_steps"]

# intraday_steps is a new data type (Fix 6 of the dashboard/email consistency
# pass) - it has no history yet, and per that fix's own scope it should
# accumulate going forward rather than trigger a big historical backfill the
# first time it runs, unlike the other sources' default full-year window.
INITIAL_BACKFILL_DAYS_OVERRIDE = {"intraday_steps": 3}


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def resolve_window(conn, source: str, full: bool, days: int) -> tuple[date, date]:
    today = config.local_today()
    if full:
        return today - timedelta(days=days), today

    last = db.get_last_synced(conn, source)
    if last is None:
        initial_days = INITIAL_BACKFILL_DAYS_OVERRIDE.get(source, days)
        return today - timedelta(days=initial_days), today

    last_date = datetime.strptime(last, "%Y-%m-%d").date()
    return last_date + timedelta(days=1), today


def sync_daily_metrics(client, conn, start: date, end: date) -> None:
    if start > end:
        logger.info("daily_metrics: already up to date")
        return

    count = 0
    error = None
    for d in daterange(start, end):
        d_str = d.isoformat()
        try:
            stats = call_with_retry(client.get_stats, d_str)
        except Exception as exc:
            logger.warning("daily_metrics: failed to fetch stats for %s: %s", d_str, exc)
            stats, error = {}, str(exc)
        pace()

        try:
            stress = call_with_retry(client.get_stress_data, d_str)
        except Exception as exc:
            logger.warning("daily_metrics: failed to fetch stress for %s: %s", d_str, exc)
            stress, error = {}, str(exc)
        pace()

        try:
            body_battery = call_with_retry(client.get_body_battery, d_str, d_str)
        except Exception as exc:
            logger.warning("daily_metrics: failed to fetch body battery for %s: %s", d_str, exc)
            body_battery, error = None, str(exc)
        pace()

        try:
            respiration = call_with_retry(client.get_respiration_data, d_str)
        except Exception as exc:
            logger.warning("daily_metrics: failed to fetch respiration for %s: %s", d_str, exc)
            respiration, error = {}, str(exc)
        pace()

        try:
            spo2 = call_with_retry(client.get_spo2_data, d_str)
        except Exception as exc:
            logger.warning("daily_metrics: failed to fetch spo2 for %s: %s", d_str, exc)
            spo2, error = {}, str(exc)
        pace()

        try:
            hrv = call_with_retry(client.get_hrv_data, d_str)
        except Exception as exc:
            logger.warning("daily_metrics: failed to fetch hrv for %s: %s", d_str, exc)
            hrv, error = None, str(exc)
        pace()

        try:
            sleep = call_with_retry(client.get_sleep_data, d_str)
        except Exception as exc:
            logger.warning("daily_metrics: failed to fetch sleep for %s: %s", d_str, exc)
            sleep, error = None, str(exc)
        pace()

        try:
            body_comp = call_with_retry(client.get_body_composition, d_str)
        except Exception as exc:
            logger.warning("daily_metrics: failed to fetch body composition for %s: %s", d_str, exc)
            body_comp, error = None, str(exc)
        pace()

        row = models.parse_daily_metrics(
            d_str, stats, stress, body_battery, respiration, spo2, hrv, sleep, body_comp
        )
        db.upsert(conn, "daily_metrics", row)
        count += 1

    conn.commit()
    # Only advance the checkpoint on a clean run - otherwise a partial
    # failure would permanently skip re-fetching the days/items that didn't
    # actually save (upserts are idempotent, so retrying successful ones too
    # is harmless).
    if error is None:
        db.set_last_synced(conn, "daily_metrics", end.isoformat())
    db.log_sync(conn, "daily_metrics", "error" if error else "ok", error)
    logger.info("daily_metrics: synced %d day(s)", count)


def sync_activities(client, conn, start: date, end: date) -> None:
    if start > end:
        logger.info("activities: already up to date")
        return

    start_str, end_str = start.isoformat(), end.isoformat()
    error = None
    try:
        activities = call_with_retry(
            client.get_activities_by_date, start_str, end_str
        )
    except Exception as exc:
        logger.warning("activities: failed to fetch activities %s..%s: %s", start_str, end_str, exc)
        activities = []
        error = str(exc)
    pace()

    count = 0
    for activity in activities or []:
        try:
            row = models.parse_activity(activity)
            if row.get("garmin_id") is not None:
                db.upsert(conn, "activities", row)
                count += 1
        except Exception as exc:
            logger.warning("activities: failed to parse activity %s: %s", activity.get("activityId"), exc)
            error = str(exc)

    conn.commit()
    if error is None:
        db.set_last_synced(conn, "activities", end.isoformat())
    db.log_sync(conn, "activities", "error" if error else "ok", error)
    logger.info("activities: synced %d activities", count)


def sync_training_status(client, conn, start: date, end: date) -> None:
    if start > end:
        logger.info("training_status: already up to date")
        return

    count = 0
    error = None
    for d in daterange(start, end):
        d_str = d.isoformat()
        try:
            status = call_with_retry(client.get_training_status, d_str)
        except Exception as exc:
            logger.warning("training_status: failed to fetch status for %s: %s", d_str, exc)
            status, error = None, str(exc)
        pace()

        try:
            max_metrics = call_with_retry(client.get_max_metrics, d_str)
        except Exception as exc:
            logger.warning("training_status: failed to fetch max metrics for %s: %s", d_str, exc)
            max_metrics, error = None, str(exc)
        pace()

        row = models.parse_training_status(d_str, status, max_metrics)
        if row:
            db.upsert(conn, "training_status", row)
            count += 1

    conn.commit()
    if error is None:
        db.set_last_synced(conn, "training_status", end.isoformat())
    db.log_sync(conn, "training_status", "error" if error else "ok", error)
    logger.info("training_status: synced %d day(s) with data", count)


def sync_intraday_steps(client, conn, start: date, end: date) -> None:
    if start > end:
        logger.info("intraday_steps: already up to date")
        return

    count = 0
    error = None
    for d in daterange(start, end):
        d_str = d.isoformat()
        try:
            entries = call_with_retry(client.get_steps_data, d_str)
        except Exception as exc:
            logger.warning("intraday_steps: failed to fetch steps data for %s: %s", d_str, exc)
            entries, error = [], str(exc)
        pace()

        for row in models.parse_intraday_steps(d_str, entries):
            db.upsert(conn, "intraday_steps", row)
        count += 1

    conn.commit()
    if error is None:
        db.set_last_synced(conn, "intraday_steps", end.isoformat())
    db.log_sync(conn, "intraday_steps", "error" if error else "ok", error)
    logger.info("intraday_steps: synced %d day(s)", count)


SYNC_FUNCS = {
    "daily_metrics": sync_daily_metrics,
    "activities": sync_activities,
    "training_status": sync_training_status,
    "intraday_steps": sync_intraday_steps,
}


def run(full: bool, days: int) -> None:
    client = connect()

    with db.connect() as conn:
        for source in SOURCES:
            fn = SYNC_FUNCS[source]
            start, end = resolve_window(conn, source, full, days)
            logger.info("=== %s: syncing %s .. %s ===", source, start, end)
            try:
                fn(client, conn, start, end)
            except Exception as exc:
                logger.error("%s: sync failed: %s", source, exc)
                db.log_sync(conn, source, "error", str(exc))


def main():
    parser = argparse.ArgumentParser(description="Sync Garmin Connect data to the database.")
    parser.add_argument(
        "--full", action="store_true",
        help="Full historical backfill instead of incremental sync since last run.",
    )
    parser.add_argument(
        "--days", type=int, default=config.BACKFILL_DAYS,
        help=f"Number of days to backfill when --full is used or no prior sync exists (default {config.BACKFILL_DAYS}).",
    )
    args = parser.parse_args()

    run(full=args.full, days=args.days)


if __name__ == "__main__":
    main()
