"""FastAPI app serving the Today screen (Phase 2 - static, no LLM yet)."""
import base64
import json
import re
import secrets
from datetime import date, datetime

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from garmin_tracker import analytics, coach_email, config, db, stats_engine
from webapp import charts

METRIC_LABELS = {
    "resting_hr": "resting heart rate",
    "sleep_score": "sleep score",
    "body_battery_wake": "morning body battery",
    "hrv_status_numeric": "HRV status",
    "steps": "steps",
    "training_load": "training load",
    "stress_avg": "daytime stress",
    "training_day": "training days",
}

class BasicAuthMiddleware(BaseHTTPMiddleware):
    """Gates every request behind HTTP Basic Auth - only enforced when both
    DASHBOARD_USERNAME and DASHBOARD_PASSWORD are set, so local dev without
    them stays open. This is a single-user personal deployment, not a
    multi-tenant app, so one shared credential pair is enough."""

    async def dispatch(self, request: Request, call_next):
        if not (config.DASHBOARD_USERNAME and config.DASHBOARD_PASSWORD):
            return await call_next(request)

        auth = request.headers.get("authorization")
        if auth and auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8")
                username, _, password = decoded.partition(":")
            except Exception:
                username = password = ""
            if secrets.compare_digest(username, config.DASHBOARD_USERNAME) and \
                    secrets.compare_digest(password, config.DASHBOARD_PASSWORD):
                return await call_next(request)

        return HTMLResponse(
            "Authentication required.", status_code=401,
            headers={"WWW-Authenticate": "Basic realm=\"Fitness Dashboard\""},
        )


app = FastAPI()
app.add_middleware(BasicAuthMiddleware)
app.mount("/static", StaticFiles(directory="webapp/static"), name="static")
templates = Jinja2Templates(directory="webapp/templates")


def _rail(label: str, pace: dict, unit: str = "", not_synced: bool = False) -> dict:
    target = pace["target"] or 0
    actual = pace["actual"]
    expected = pace["expected_by_now"]
    delta = pace["delta"]
    # A missing actual (not yet synced) is never rendered as a real zero -
    # the fill bar shows empty and the template shows "not yet synced"
    # instead of a fabricated number.
    not_synced = not_synced or actual is None
    fill_pct = min((actual / target) * 100, 100) if target and actual is not None else 0
    tick_pct = min((expected / target) * 100, 100) if target and expected is not None else 0

    gap_label, gap_class = None, "idle"
    if not_synced:
        gap_label = "awaiting sync"
    elif delta is not None:
        if delta < 0:
            gap_label, gap_class = f"{abs(delta):g} behind", "behind"
        elif delta > 0:
            gap_label, gap_class = f"{delta:g} ahead", "ahead"
        else:
            gap_label, gap_class = "on pace", "idle"

    return {
        "label": label,
        "actual": actual,
        "target": pace["target"],
        "unit": unit,
        "fill_pct": round(fill_pct, 1),
        "tick_pct": round(tick_pct, 1),
        "on_pace": pace["on_pace"],
        "not_synced": not_synced,
        "gap_label": gap_label,
        "gap_class": gap_class,
        # the gap zone (the shaded region between the fill and the expected
        # tick) only makes sense when behind - "ahead" already reads clearly
        # from the fill overshooting the tick.
        "gapzone_left": min(fill_pct, tick_pct),
        "gapzone_width": abs(tick_pct - fill_pct) if gap_class == "behind" else 0,
    }


def _relative_time(dt: datetime, now: datetime) -> str:
    delta = now - dt
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{max(round(delta.total_seconds() / 60), 1)}m ago"
    if hours < 48:
        return f"{round(hours)}h ago"
    return f"{round(hours / 24)}d ago"


def _split_brief_action(text: str) -> tuple[str, str]:
    """The daily brief ends with one bolded action sentence, per the coach
    prompt's own hard constraint - split it out so the card can render it
    below a hairline instead of buried in the prose."""
    matches = list(re.finditer(r"\*\*(.+?)\*\*", text))
    if not matches:
        return text.strip(), None
    last = matches[-1]
    return text[:last.start()].strip(), last.group(1).strip()


def _brief_card(conn, today_date: date) -> dict:
    """The coach brief card must never render nothing - if today's brief is
    missing or fails the pre-send validation gate, the card says exactly
    which check failed instead of showing a blank space."""
    rows = db.fetch_all_dicts(conn, "SELECT * FROM briefs WHERE kind = 'daily' ORDER BY date DESC LIMIT 1")
    if not rows:
        return {"status": "missing"}

    brief = rows[0]
    failures = coach_email.validate_snapshot(conn, brief, today_date.isoformat())
    if failures:
        return {"status": "incomplete", "failures": failures}

    prose, action = _split_brief_action(brief["body_markdown"])
    return {
        "status": "ok",
        "prose_html": coach_email._markdown_to_html(prose),
        "action": action,
        "date": brief["date"],
    }


@app.get("/", response_class=HTMLResponse)
def today(request: Request):
    conn = db.get_connection()
    try:
        today_date = config.local_today()

        # Single source of truth: everything below reads from this one dict.
        # No separate aggregate query - see analytics.build_snapshot's
        # docstring for why.
        snapshot = analytics.build_snapshot(conn, config.GOALS, today_date)

        yesterday = snapshot["yesterday"]
        yesterday["activity_labels"] = [
            f"{(a['type'] or 'activity').replace('_', ' ').title()} {round(a['duration_min'])}min"
            for a in yesterday["activities"]
        ]

        steps_stale = snapshot["sync_status"]["sources"]["daily_metrics"]["stale"]
        rails = [
            _rail("Steps · Today", snapshot["steps_pace"], not_synced=steps_stale),
            _rail("Strength · This Month", snapshot["strength_pace"], unit=" sessions"),
            _rail("Racquet · This Week", snapshot["racquet_pace"], unit=" sessions"),
        ]

        brief_card = _brief_card(conn, today_date)

        last_sync_at = snapshot["sync_status"]["sources"]["daily_metrics"]["last_sync_at"]
        last_synced_label = None
        if last_sync_at:
            sync_dt_utc = datetime.fromisoformat(last_sync_at)
            local_time = sync_dt_utc.astimezone(config.LOCAL_TZ)
            now_local = datetime.now(config.LOCAL_TZ)
            time_str = local_time.strftime("%H:%M")
            last_synced_label = f"last sync {time_str} · {_relative_time(local_time, now_local)}"

        # ---- Steps, 30 days ----
        steps_data = analytics.steps_30_day(conn, config.GOALS, today_date)
        steps_module = {"state": steps_data["state"]}
        if steps_data["state"] == "insufficient":
            steps_module["requirement"] = f"needs {steps_data['min_required']} days, have {steps_data['n_available']}"
        else:
            steps_module.update({
                "svg": charts.steps_30day_svg(steps_data),
                "finding": f"You're averaging {steps_data['avg']:,} steps — "
                           f"{abs(steps_data['pct_vs_goal'])}% {'above' if steps_data['pct_vs_goal'] >= 0 else 'below'} goal",
                "subtitle": f"Daily steps · {charts._human_date(steps_data['range_start'])} – {charts._human_date(steps_data['range_end'])} · 7-day average overlaid"
                            + (f" · {steps_data['n_available']} of 30 days available" if steps_data["state"] == "partial" else ""),
            })

        # ---- Training calendar ----
        cal = analytics.training_calendar_weeks(conn, today_date)
        n_training_days = sum(1 for d in cal["days"] if d["buckets"])
        n_weeks_shown = -(-len(cal["days"]) // 7)
        calendar_module = {
            "svg": charts.training_calendar_weeks_svg(cal),
            "finding": f"{n_training_days} training days across {n_weeks_shown} weeks",
            "subtitle": f"{charts._human_date(cal['start'])} – {charts._human_date(cal['end'])} · one cell per day",
        }

        # ---- Sessions by month ----
        month_rows = analytics.sessions_by_month(conn, today=today_date)

        # ---- Weekly calories ----
        wc = analytics.weekly_calories_with_total(conn, today=today_date)
        calories_module = {"state": wc["state"]}
        if wc["state"] == "insufficient":
            calories_module["requirement"] = f"needs 3 complete weeks, have {wc['complete_weeks']}"
        else:
            dominant = analytics.weekly_calories_dominant_bucket(wc)
            finding = (
                f"{dominant['bucket'].capitalize()} carries {dominant['pct']}% of your logged active calories"
                if dominant else "Weekly active calories by type"
            )
            calories_module.update({
                "svg": charts.stacked_bar_svg(wc, total_line=wc["total_active_calories"], stretch=True),
                "finding": finding,
                "subtitle": "Weekly active calories by type · last 12 weeks"
                            + (f" · {wc['complete_weeks']} of 12 weeks available" if wc["state"] == "partial" else ""),
            })

        # ---- Recovery sparklines ----
        rs = analytics.recovery_sparklines(conn, today_date)
        recovery_module = {"state": rs["state"]}
        if rs["state"] == "insufficient":
            recovery_module["requirement"] = f"needs {rs['min_required']} days, have {rs['n_available']}"
        else:
            hr, sleep = rs["resting_hr"], rs["sleep_hours"]
            hr_band_low = (hr["mean_30d"] - hr["sd_30d"]) if hr["mean_30d"] is not None else None
            hr_band_high = (hr["mean_30d"] + hr["sd_30d"]) if hr["mean_30d"] is not None else None
            recovery_module.update({
                "subtitle": f"Resting HR and sleep · {charts._human_date(rs['range_start'])} – {charts._human_date(rs['range_end'])} · shaded band = normal range",
                "hr_svg": charts.sparkline_svg(hr["points"], band_low=hr_band_low, band_high=hr_band_high,
                                                label="Resting HR", unit="bpm", current=hr["current"]),
                "sleep_svg": charts.sparkline_svg(sleep["points"], band_low=sleep["band_low"], band_high=sleep["band_high"],
                                                   label="Sleep", unit="hours", current=sleep["current"]),
            })

        # ---- Weight ----
        wt = analytics.weight_chart_data(conn, config.GOALS, today_date)
        weight_module = {"state": wt["state"]}
        if wt["state"] == "insufficient":
            weight_module.update({
                "n_readings": wt["n_readings"], "since": wt["since"],
                "min_readings": wt["min_readings"], "min_span_days": wt["min_span_days"],
                "checkpoint": wt["checkpoint"],
                "ghost_svg": charts.ghost_preview_svg(),
            })
        else:
            weight_module.update({
                "svg": charts.weight_trend_svg(wt["chart_series"], wt["checkpoint"]),
                "trend_weight_lb": wt["trend_weight_lb"],
                "rate_lb_per_week": wt["rate_lb_per_week"],
                "checkpoint": wt["checkpoint"],
                "projected_range": wt["projected_checkpoint_date_range"],
            })

        return templates.TemplateResponse(
            request,
            "today.html",
            {
                "active_page": "today",
                "today_label": today_date.strftime("%A, %B ") + str(today_date.day),
                "readiness": snapshot["readiness"],
                "last_synced_label": last_synced_label,
                "rails": rails,
                "yesterday": yesterday,
                "brief_card": brief_card,
                "steps_module": steps_module,
                "calendar_module": calendar_module,
                "month_rows": month_rows,
                "calories_module": calories_module,
                "recovery_module": recovery_module,
                "weight_module": weight_module,
            },
        )
    finally:
        conn.close()


@app.post("/log-weight")
def log_weight(weight_lb: float = Form(...)):
    conn = db.get_connection()
    try:
        analytics.log_weight(conn, config.local_today(), weight_lb)
    finally:
        conn.close()
    return RedirectResponse(url="/", status_code=303)


@app.get("/activity", response_class=HTMLResponse)
def activity(request: Request):
    conn = db.get_connection()
    try:
        weekly_calories = analytics.weekly_calories_by_bucket(conn)
        weekday_cycle = analytics.weekday_step_cycle(conn)
        cycle_weeks = 12
        cycle_range = f"trailing {cycle_weeks} weeks"
        heatmap = analytics.calendar_heatmap_data(conn)
        avg_calories = analytics.avg_calories_per_session(conn)

        activity_rows = db.fetch_all_dicts(
            conn,
            "SELECT date, activity_type, name, duration_min, calories FROM activities "
            "ORDER BY date DESC LIMIT 100",
        )

        return templates.TemplateResponse(
            request,
            "activity.html",
            {
                "active_page": "activity",
                "stacked_bar_svg": charts.stacked_bar_svg(weekly_calories),
                "cycle_panels": [
                    (name, charts.cycle_plot_svg(name, panel))
                    for name, panel in weekday_cycle.items()
                ],
                "cycle_range": cycle_range,
                "heatmap_svg": charts.heatmap_svg(heatmap),
                "heatmap_month": heatmap["month"],
                "avg_calories": avg_calories,
                "activities": activity_rows,
            },
        )
    finally:
        conn.close()


@app.get("/insights", response_class=HTMLResponse)
def insights(request: Request):
    conn = db.get_connection()
    try:
        findings = db.fetch_all_dicts(
            conn, "SELECT * FROM findings WHERE status = 'surfaced' ORDER BY kind, computed_at DESC"
        )

        hypothesis_lookup = {
            (h["predictor"], h["outcome"], h["lag_days"]): h for h in stats_engine.HYPOTHESES
        }

        lagged_cards, comparison_cards, anomaly_cards = [], [], []
        for f in findings:
            if f["kind"] == "lagged_hypothesis":
                spec = hypothesis_lookup.get((f["predictor"], f["outcome"], f["lag_days"]))
                lagged_cards.append({
                    "description": spec["description"] if spec else f"{f['predictor']} → {f['outcome']}",
                    "effect_size": f["effect_size"],
                    "n_effective": f["n_effective"],
                    "q_value": f["q_value"],
                    "svg": charts.effect_ci_svg(f["effect_size"], f["ci_low"], f["ci_high"]),
                })
            elif f["kind"] == "training_rest_comparison":
                outcome_label = METRIC_LABELS.get(f["outcome"], f["outcome"])
                comparison_cards.append({
                    "description": f"{outcome_label.capitalize()} tends to differ between training days and rest days.",
                    "effect_size": f["effect_size"],
                    "n_effective": f["n_effective"],
                    "svg": charts.effect_ci_svg(f["effect_size"], f["ci_low"], f["ci_high"]),
                })
            elif f["kind"] == "anomaly":
                try:
                    detail = json.loads(f["detail_json"] or "{}")
                except ValueError:
                    detail = {}
                metric_label = METRIC_LABELS.get(f["predictor"], f["predictor"])
                anomaly_cards.append({
                    "description": f"Unusual {metric_label} reading on {detail.get('date', '?')} "
                                    f"({detail.get('value', '?')}, z={f['effect_size']:+.1f}).",
                })

        return templates.TemplateResponse(
            request,
            "insights.html",
            {
                "active_page": "insights",
                "lagged_cards": lagged_cards,
                "comparison_cards": comparison_cards,
                "anomaly_cards": anomaly_cards,
                "has_any": bool(lagged_cards or comparison_cards or anomaly_cards),
            },
        )
    finally:
        conn.close()
