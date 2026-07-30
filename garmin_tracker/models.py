"""Helpers that turn raw (and messy/inconsistent) Garmin Connect API
payloads into flat dicts matching our SQLite schema.

The unofficial Garmin API returns slightly different shapes depending on
account/device, so every lookup here is defensive (.get with a default)
and callers should tolerate `None` for fields a given device doesn't report.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from garmin_tracker.config import LOCAL_TZ

logger = logging.getLogger(__name__)


def _get(d: Optional[dict], *keys, default=None):
    """Try a list of possible key names (API has renamed fields across
    versions) and return the first present, non-None value."""
    if not d:
        return default
    for key in keys:
        if key in d and d[key] is not None:
            return d[key]
    return default


KG_TO_LB = 2.20462


def _wake_body_battery(body_battery: Optional[list], sleep: Optional[dict],
                        max_diff_ms: int = 3 * 60 * 60 * 1000) -> Optional[int]:
    """Body Battery reading closest to wake time, using the sleep session's
    end timestamp against get_body_battery's intraday bodyBatteryValuesArray
    ([timestamp_ms, value] pairs). Verified against real data: typically
    lands within ~10 minutes of actual wake. Falls back to None (not the
    day's max/min, which are materially different numbers - see Phase 2
    finding) if either signal is missing or nothing is close enough."""
    if not body_battery or not sleep:
        return None
    entry = body_battery[0] if isinstance(body_battery, list) else body_battery
    values = entry.get("bodyBatteryValuesArray")
    if not values:
        return None

    daily = sleep.get("dailySleepDTO") or {}
    wake_ts = _get(daily, "sleepEndTimestampGMT")
    if wake_ts is None:
        return None

    closest = min(values, key=lambda pair: abs(pair[0] - wake_ts))
    if abs(closest[0] - wake_ts) > max_diff_ms:
        return None
    return closest[1]


def _day_min_body_battery(body_battery: Optional[list]) -> Optional[int]:
    if not body_battery:
        return None
    entry = body_battery[0] if isinstance(body_battery, list) else body_battery
    return _get(entry, "drained", "bodyBatteryLowestValue")


def _weight_lb_from_body_composition(body_comp: Optional[dict]) -> Optional[float]:
    if not body_comp:
        return None
    day = body_comp.get("totalAverage", body_comp)
    weight_kg_raw = _get(day, "weight")
    if not weight_kg_raw:
        return None
    weight_kg = weight_kg_raw / 1000 if weight_kg_raw > 1000 else weight_kg_raw
    return round(weight_kg * KG_TO_LB, 2)


def parse_daily_metrics(date_str: str, stats: dict, stress: dict | None,
                         body_battery: list | None, respiration: dict | None,
                         spo2: dict | None, hrv: dict | None, sleep: dict | None,
                         body_comp: dict | None = None) -> dict:
    hrv_status = _get((hrv or {}).get("hrvSummary") or {}, "status")

    sleep_minutes = sleep_score = None
    if sleep:
        daily = sleep.get("dailySleepDTO") or {}
        sleep_sec = _get(daily, "sleepTimeSeconds")
        if sleep_sec:
            sleep_minutes = sleep_sec / 60.0
        overall_score = (daily.get("sleepScores") or {}).get("overall") or {}
        sleep_score = _get(overall_score, "value")

    return {
        "date": date_str,
        "steps": _get(stats, "totalSteps"),
        "distance_m": _get(stats, "totalDistanceMeters"),
        "total_calories": _get(stats, "totalKilocalories"),
        "active_calories": _get(stats, "activeKilocalories"),
        "resting_hr": _get(stats, "restingHeartRate"),
        "hrv_status": hrv_status,
        "body_battery_wake": _wake_body_battery(body_battery, sleep),
        "body_battery_min": _day_min_body_battery(body_battery),
        "sleep_minutes": sleep_minutes,
        "sleep_score": sleep_score,
        "weight_lb": _weight_lb_from_body_composition(body_comp),
        "stress_avg": _get(stress, "avgStressLevel", "overallStressLevel"),
        "respiration_avg": _get(respiration, "avgSleepRespirationValue",
                                 "avgWakingRespirationValue"),
        "spo2_avg": _get(spo2, "averageSpO2", "avgSpO2"),
    }


def parse_intraday_steps(date_str: str, entries: Optional[list[dict]]) -> list[dict]:
    """15-minute-interval entries (Garmin returns them GMT-timestamped, but
    already windowed to the account's local day) -> one row per local hour
    with that hour's step count, for the hourly pacing curve in analytics.py.
    Entries whose converted local date doesn't match date_str (can happen at
    the edges of Garmin's own day window) are dropped rather than misfiled
    into the wrong day."""
    hourly: dict[int, int] = {}
    for e in entries or []:
        start_gmt = e.get("startGMT")
        steps = e.get("steps")
        if not start_gmt or steps is None:
            continue
        dt_utc = datetime.fromisoformat(start_gmt).replace(tzinfo=ZoneInfo("UTC"))
        dt_local = dt_utc.astimezone(LOCAL_TZ)
        if dt_local.date().isoformat() != date_str:
            continue
        hourly[dt_local.hour] = hourly.get(dt_local.hour, 0) + steps

    return [{"date": date_str, "hour": h, "steps": s} for h, s in sorted(hourly.items())]


# Garmin's own account-level trainingLoad is unavailable for this account
# (mostRecentTrainingLoadBalance came back null across multiple real dates
# during Phase 1 verification) - so load is a homegrown TRIMP-style proxy
# from per-activity HR-zone minutes, which Garmin does reliably report.
ZONE_WEIGHTS = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5}


def _compute_training_load(activity: dict) -> Optional[float]:
    total = 0.0
    found = False
    for zone, weight in ZONE_WEIGHTS.items():
        secs = activity.get(f"hrTimeInZone_{zone}")
        if secs:
            total += weight * (secs / 60.0)
            found = True
    return round(total, 1) if found else None


# Activity type -> bucket, per the design spec's classification table.
BUCKET_MAP = {
    "strength_training": "strength",
    "tennis_v2": "racquet",
    "pickleball": "racquet",
    "padel": "racquet",
    "racquetball": "racquet",
    "running": "cardio",
    "treadmill_running": "cardio",
    "walking": "cardio",
    "cycling": "cardio",
    "indoor_cycling": "cardio",
    "elliptical": "cardio",
    "hiking": "cardio",
}


def _bucket_for(activity_type: Optional[str]) -> str:
    return BUCKET_MAP.get(activity_type, "other")


def parse_activity(activity: dict) -> dict:
    start_time = _get(activity, "startTimeLocal", "startTimeGMT")
    activity_type = _get(activity.get("activityType", {}), "typeKey")
    duration_sec = _get(activity, "duration")

    return {
        "garmin_id": activity.get("activityId"),
        "date": start_time[:10] if start_time else None,
        "start_time": start_time,
        "activity_type": activity_type,
        "bucket": _bucket_for(activity_type),
        "name": activity.get("activityName"),
        "duration_min": round(duration_sec / 60.0, 1) if duration_sec else None,
        "distance_m": _get(activity, "distance"),
        "calories": _get(activity, "calories"),
        "avg_hr": _get(activity, "averageHR"),
        "max_hr": _get(activity, "maxHR"),
        "training_load": _compute_training_load(activity),
        "raw_json": json.dumps(activity),
    }


def parse_training_status(date_str: str, status: dict, max_metrics: list | None) -> Optional[dict]:
    if not status and not max_metrics:
        return None

    vo2_running = vo2_cycling = None
    if max_metrics:
        for m in max_metrics:
            if not isinstance(m, dict):
                continue
            generic = m.get("generic") or {}
            if generic.get("vo2MaxPreciseValue") or generic.get("vo2MaxValue"):
                vo2_running = _get(generic, "vo2MaxPreciseValue", "vo2MaxValue")
            cycling = m.get("cycling") or {}
            if cycling.get("vo2MaxValue"):
                vo2_cycling = cycling.get("vo2MaxValue")

    most_recent = None
    training_load = None
    if status:
        most_recent_status = status.get("mostRecentTrainingStatus") or {}
        latest_data = most_recent_status.get("latestTrainingStatusData") or {}
        latest_key = next(iter(latest_data), None)
        if latest_key:
            data = latest_data[latest_key]
            most_recent = _get(data, "trainingStatus")
            training_load = _get(data, "weeklyTrainingLoad")

    if vo2_running is None and vo2_cycling is None and most_recent is None:
        return None

    return {
        "date": date_str,
        "vo2max_running": vo2_running,
        "vo2max_cycling": vo2_cycling,
        "training_status": most_recent,
        "training_load": training_load,
    }
