"""FastAPI app serving the Today screen (Phase 2 - static, no LLM yet)."""
import base64
import html
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


def _rail(label: str, pace: dict, unit: str = "", rate_style: str = "steps") -> dict:
    """Builds one pace rail's display dict from analytics.py's monthly pace
    dict (state: cleared/behind/dead, integers only - see _monthly_pace's
    own docstring). `rate_style` picks how the footer rate reads: steps is
    always a daily rate; racquet reads naturally as a weekly rate; strength
    has no natural rate unit besides "needs N/day" while behind."""
    target = pace["target"] or 0
    actual = pace["actual"]
    expected = pace["expected_by_now"]
    state = pace["state"]

    fill_pct = min((actual / target) * 100, 100) if target else 0
    tick_pct = min((expected / target) * 100, 100) if target and expected is not None else 0
    css_state = "ahead" if state == "cleared" else state

    if state == "cleared":
        over = pace["over"]
        remain_label = f"cleared · +{over:,} over" if over > 0 else "cleared"
    else:
        days_remaining = pace["days_remaining"]
        day_word = "day" if days_remaining == 1 else "days"
        remain_label = f"{pace['remaining']:,} left · {days_remaining} {day_word}"
        if state == "dead":
            remain_label += " · not reachable"

    rate_label = None
    if rate_style == "steps":
        rate_label = f"{pace['avg_rate']:,.0f}/day avg · needed {pace['original_required_rate']:,.0f}"
    elif state == "dead":
        rate_label = f"became unreachable {charts._human_date(pace['became_unreachable_date'])}"
    elif rate_style == "racquet":
        rate_label = f"{pace['avg_rate_per_week']}/week" if state == "cleared" else f"needs {round(pace['required_rate'] * 7, 1)}/week"
    elif rate_style == "strength" and state == "behind":
        rate_label = f"needs {pace['required_rate']}/day"

    return {
        "label": label,
        "actual": actual,
        "target": target,
        "unit": unit,
        "fill_pct": round(fill_pct, 1),
        "tick_pct": round(tick_pct, 1),
        "css_state": css_state,
        "remain_label": remain_label,
        "rate_label": rate_label,
        "big": rate_style == "steps",
        # the gap zone (shaded region between fill and the expected tick)
        # only makes sense once behind - "cleared" already reads clearly
        # from the fill covering (or overshooting) the tick.
        "gapzone_left": min(fill_pct, tick_pct),
        "gapzone_width": abs(tick_pct - fill_pct) if state in ("behind", "dead") else 0,
    }


def _yesterday_display(y: dict) -> dict:
    """Presentation-only formatting (sign, %, up/dn class) of already-
    computed yesterday_summary() fields - no metric is computed here, just
    strings built from numbers analytics.py already produced."""
    def pct_chip(value, pct_delta):
        if value is None or pct_delta is None:
            return None
        sign = "+" if pct_delta >= 0 else "−"
        return {"label": f"{sign}{abs(round(pct_delta))}%", "cls": "up" if pct_delta >= 0 else "dn"}

    def abs_chip(delta, unit=""):
        if delta is None:
            return None
        sign = "+" if delta >= 0 else ""
        return {"label": f"{sign}{delta}{unit}", "cls": "up" if delta >= 0 else "dn"}

    activity_labels = [
        f"{(a['type'] or 'activity').replace('_', ' ').title()} {round(a['duration_min'])}min"
        for a in y["activities"]
    ]
    return {
        "date_label": charts._human_date(y["date"]),
        "steps": y["steps"],
        "steps_chip": pct_chip(y["steps"], y["steps_pct_delta"]),
        "is_rest_day": y["is_rest_day"],
        "activity_labels": activity_labels,
        "active_calories": y["active_calories"],
        "active_calories_chip": pct_chip(y["active_calories"], y["active_calories_pct_delta"]),
        "sleep_hours": y["sleep_hours"],
        "sleep_chip": abs_chip(y["sleep_hours_delta"], "h"),
        "resting_hr": y["resting_hr"],
        "resting_hr_chip": abs_chip(y["resting_hr_delta"]),
    }


def _relative_time(dt: datetime, now: datetime) -> str:
    delta = now - dt
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{max(round(delta.total_seconds() / 60), 1)}m ago"
    if hours < 48:
        return f"{round(hours)}h ago"
    return f"{round(hours / 24)}d ago"


def _inline_md(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html.escape(text))


def _parse_brief_sections(text: str) -> list[dict]:
    """Splits the coach brief's `### HEADER` / `- bullet` markdown (per
    coach.py's DAILY_SYSTEM_PROMPT) into an ordered list of labeled bullet
    groups. Pure text structuring, not a metric computation - the LLM
    already decided what to say and in what order; this only navigates the
    markdown it agreed to produce."""
    sections = []
    current = None
    for line in text.splitlines():
        header = re.match(r"^#{1,3}\s+(.+?)\s*$", line)
        if header:
            current = {"label": header.group(1).strip(), "bullets": []}
            sections.append(current)
            continue
        bullet = re.match(r"^[-*]\s+(.+?)\s*$", line)
        if bullet and current is not None:
            current["bullets"].append(_inline_md(bullet.group(1)))
        elif line.strip() and current is not None and not current["bullets"]:
            # A section with prose instead of bullets (INSIGHT is usually a
            # paragraph, not a bullet list) - keep it as a single "bullet".
            current["bullets"].append(_inline_md(line.strip()))
    return sections


def _brief_card(conn, snapshot_date_val: date) -> dict:
    """The coach brief card must never render nothing - if today's brief is
    missing or fails the pre-send validation gate, the card says exactly
    which check failed instead of showing a blank space."""
    rows = db.fetch_all_dicts(conn, "SELECT * FROM briefs WHERE kind = 'daily' ORDER BY date DESC LIMIT 1")
    if not rows:
        return {"status": "missing"}

    brief = rows[0]
    failures = coach_email.validate_snapshot(conn, brief, snapshot_date_val.isoformat())
    if failures:
        return {"status": "incomplete", "failures": failures}

    sections = _parse_brief_sections(brief["body_markdown"])
    insight = next((s for s in sections if s["label"].upper() == "INSIGHT"), None)
    groups = [s for s in sections if s["label"].upper() != "INSIGHT"]
    return {
        "status": "ok",
        "groups": groups,
        "insight": insight,
        "date": brief["date"],
    }


@app.get("/", response_class=HTMLResponse)
def today(request: Request):
    conn = db.get_connection()
    try:
        real_today = config.local_today()
        snap_date = config.snapshot_date()

        # Single source of truth: everything below reads from this one dict.
        # No separate aggregate query - see analytics.build_snapshot's
        # docstring for why. Every figure covers data through snap_date
        # (rule 1: midnight cutoff) - real_today is used only for the page
        # header's own calendar date, never for a metric.
        snapshot = analytics.build_snapshot(conn, config.GOALS, snap_date)

        rails = [
            _rail("Steps · this month", snapshot["steps_pace"], rate_style="steps"),
            _rail("Strength · this month", snapshot["strength_pace"], unit=" sessions", rate_style="strength"),
            _rail("Racquet · this month", snapshot["racquet_pace"], unit=" sessions", rate_style="racquet"),
        ]
        days_remaining = snapshot["steps_pace"]["days_remaining"]
        day_word = "day" if days_remaining == 1 else "days"
        rails_eyebrow = f"Pace · {snap_date.strftime('%B')} · {days_remaining} {day_word} remaining"

        brief_card = _brief_card(conn, snap_date)

        last_sync_at = snapshot["sync_status"]["sources"]["daily_metrics"]["last_sync_at"]
        last_synced_label = None
        if last_sync_at:
            sync_dt_utc = datetime.fromisoformat(last_sync_at)
            local_time = sync_dt_utc.astimezone(config.LOCAL_TZ)
            now_local = datetime.now(config.LOCAL_TZ)
            time_str = local_time.strftime("%H:%M")
            last_synced_label = f"last sync {time_str} · {_relative_time(local_time, now_local)}"

        # ---- Steps, current month ----
        steps_data = analytics.steps_current_month(conn, config.GOALS, snap_date)
        steps_module = {"state": steps_data["state"]}
        if steps_data["state"] == "insufficient":
            steps_module["requirement"] = "no synced steps yet this month"
        else:
            direction = "above" if steps_data["pct_vs_goal"] >= 0 else "below"
            steps_module.update({
                "svg": charts.steps_month_bars_svg(steps_data),
                "finding": f"{snap_date.strftime('%B')} averaged {steps_data['daily_avg']:,} steps a day — "
                           f"{abs(steps_data['pct_vs_goal'])}% {direction} your {steps_data['goal']:,} goal",
                "subtitle": f"Daily steps · {charts._human_date(steps_data['range_start'])} – "
                            f"{charts._human_date(steps_data['range_end'])} · hover any bar for its exact count",
                "month_total": steps_data["month_total"],
                "daily_avg": steps_data["daily_avg"],
                "days_at_goal": steps_data["days_at_goal"],
                "days_in_period": steps_data["days_in_period"],
                "best_day": steps_data["best_day"],
            })

        # ---- Yesterday ----
        yesterday = _yesterday_display(snapshot["yesterday"])

        # ---- Monthly steps (6 months) ----
        monthly_steps = analytics.monthly_steps_bars(conn, config.GOALS, snap_date)
        # The current month is still in progress within this window (it
        # only ever runs through snap_date), so comparing its partial total
        # against the full-month target would always read as "missed" -
        # the headline finding below only judges completed months; the
        # current month still renders in the chart itself.
        completed_months = monthly_steps[:-1]
        n_cleared = sum(1 for r in completed_months if r["cleared"])
        misses = [r for r in completed_months if not r["cleared"]]
        if not completed_months:
            monthly_steps_finding = "Steps by month"
        elif misses:
            worst = min(misses, key=lambda r: r["steps"] - r["target"])
            monthly_steps_finding = (
                f"{n_cleared} of {len(completed_months)} completed months cleared target — "
                f"{worst['month_label']} missed by {abs(worst['steps'] - worst['target']):,}"
            )
        else:
            monthly_steps_finding = f"All {len(completed_months)} completed months cleared target"
        monthly_steps_module = {
            "svg": charts.monthly_steps_bars_svg(monthly_steps),
            "finding": monthly_steps_finding,
            "subtitle": "Total steps by month · dashed tick = that month's own target",
        }

        # ---- Sessions by month ----
        month_rows = analytics.sessions_by_month(conn, today=snap_date)
        sessions_module = {
            "svg": charts.sessions_month_stacked_svg(month_rows),
            "finding": "Strength sessions only start appearing in June"
                       if month_rows[0]["strength"] == 0 and month_rows[-1]["strength"] > 0
                       else "Sessions by month and type",
            "subtitle": f"{month_rows[0]['month_label']} – {month_rows[-1]['month_label']}",
        }

        # ---- Daily calories, current month ----
        dcal = analytics.daily_calories_current_month(conn, snap_date)
        calories_module = {"state": dcal["state"]}
        if dcal["state"] == "insufficient":
            calories_module["requirement"] = "no synced active-calorie data yet this month"
        else:
            calories_module.update({
                "svg": charts.daily_calories_month_svg(dcal),
                "subtitle": f"Daily active calories · {charts._human_date(dcal['range_start'])} – "
                            f"{charts._human_date(dcal['range_end'])} · colored by what you logged that day",
                "month_total": dcal["month_total"],
                "daily_avg": dcal["daily_avg"],
                "best_day": dcal["best_day"],
                "rest_day_avg": dcal["rest_day_avg"],
                "counts": dcal["counts"],
            })

        # ---- Monthly calories by source ----
        mcal = analytics.monthly_calories_by_source(conn, snap_date)
        latest = mcal[-1]
        top_key = max(("racquet", "strength", "cardio", "other", "unlogged"), key=lambda k: latest[k])
        top_pct = round(latest[top_key] / latest["total"] * 100) if latest["total"] else 0
        top_names = {"racquet": "Racquet sports", "strength": "Strength", "cardio": "Cardio", "other": "Other activity", "unlogged": "Unlogged movement"}
        monthly_calories_module = {
            "svg": charts.monthly_calories_stacked_svg(mcal),
            "finding": f"{top_names[top_key]} {'are' if top_key != 'strength' else 'is'} {top_pct}% of your {latest['month_label']} burn",
            "subtitle": "Monthly active calories by source · unlogged movement is everything counted outside a session",
        }

        # ---- July (current month) calendar ----
        cal = analytics.current_month_calendar(conn, snap_date)
        n_training = cal["counts"]["strength"] + cal["counts"]["racquet"] + cal["counts"]["cardio"] + cal["counts"]["other"]
        calendar_module = {
            "svg": charts.month_calendar_svg(cal),
            "finding": f"{n_training} training days, {cal['counts']['rest']} rest days in {snap_date.strftime('%B')}",
            "subtitle": f"One cell per day · longest streak {cal['longest_streak']} days · longest gap {cal['longest_gap']} days",
            "counts": cal["counts"],
        }

        # ---- Recovery ----
        recovery_module = snapshot["recovery"]

        # ---- Weight ----
        wt = analytics.weight_chart_data(conn, config.GOALS, snap_date)
        weight_module = {"state": wt["state"]}
        if wt["state"] == "insufficient":
            weight_module.update({
                "n_readings": wt["n_readings"], "since": wt["since"],
                "min_readings": wt["min_readings"], "min_span_days": wt["min_span_days"],
                "checkpoint": wt["checkpoint"],
                "points_svg": charts.weight_raw_points_svg(wt["raw_points"]),
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
                "today_label": real_today.strftime("%A, %B ") + str(real_today.day),
                "through_label": f"all figures through midnight · {snap_date.strftime('%a %b')} {snap_date.day}",
                "last_synced_label": last_synced_label,
                "rails": rails,
                "rails_eyebrow": rails_eyebrow,
                "yesterday": yesterday,
                "brief_card": brief_card,
                "steps_module": steps_module,
                "monthly_steps_module": monthly_steps_module,
                "sessions_module": sessions_module,
                "calendar_module": calendar_module,
                "calories_module": calories_module,
                "monthly_calories_module": monthly_calories_module,
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


@app.get("/nutrition", response_class=HTMLResponse)
def nutrition(request: Request):
    conn = db.get_connection()
    try:
        daily = analytics.nutrition_daily_series(conn)
        meals = analytics.meal_breakdown(conn)
        protein = analytics.protein_per_bodyweight(conn)
        weight_cal = analytics.weight_and_calories_series(conn)
        train_rest = analytics.nutrition_training_vs_rest(conn)

        daily_module = {"state": daily["state"]}
        if daily["state"] == "full":
            daily_module.update({
                "svg": charts.nutrition_daily_bars_svg(daily),
                "subtitle": f"Daily calories · {charts._human_date(daily['range_start'])} – "
                            f"{charts._human_date(daily['range_end'])} · {daily['n_days']} days logged",
                "avg_calories": daily["avg_calories"],
                "avg_protein": daily["avg_protein"],
                "avg_carbs": daily["avg_carbs"],
                "avg_fat": daily["avg_fat"],
                "avg_sodium": daily["avg_sodium"],
                "best_day_calories": daily["best_day_calories"],
                "lowest_day_calories": daily["lowest_day_calories"],
            })

        meals_module = {"state": meals["state"]}
        if meals["state"] == "full":
            meals_module["svg"] = charts.meal_breakdown_svg(meals)
            meals_module["meals"] = meals["meals"]

        protein_module = {"state": protein["state"]}
        if protein["state"] == "full":
            protein_module.update({
                "svg": charts.protein_per_bodyweight_svg(protein),
                "current": protein["current"],
                "avg": protein["avg"],
            })

        weight_cal_module = {"state": weight_cal["state"]}
        if weight_cal["state"] == "full":
            weight_cal_module["svg"] = charts.weight_and_calories_dual_svg(weight_cal)

        comparison_cards = []
        if train_rest["state"] == "full":
            for metric, c in train_rest["comparisons"].items():
                if c["status"] != "surfaced":
                    continue
                comparison_cards.append({
                    "description": f"{c['label']} tends to differ between training days and rest days "
                                    f"({c['training_avg']:,.0f} vs {c['rest_avg']:,.0f} average).",
                    "svg": charts.effect_ci_svg(c["effect_size"], c["ci_low"], c["ci_high"]),
                    "n_effective": c["n_effective"],
                })
        train_rest_insufficient = train_rest["state"] != "full" or not comparison_cards

        return templates.TemplateResponse(
            request,
            "nutrition.html",
            {
                "active_page": "nutrition",
                "daily_module": daily_module,
                "meals_module": meals_module,
                "protein_module": protein_module,
                "weight_cal_module": weight_cal_module,
                "comparison_cards": comparison_cards,
                "train_rest_insufficient": train_rest_insufficient,
            },
        )
    finally:
        conn.close()
