"""FastAPI app serving the Today screen (Phase 2 - static, no LLM yet)."""
from datetime import date

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from garmin_tracker import analytics, config, db
from webapp import charts

app = FastAPI()
app.mount("/static", StaticFiles(directory="webapp/static"), name="static")
templates = Jinja2Templates(directory="webapp/templates")


def _rail(label: str, pace: dict, unit: str = "") -> dict:
    target = pace["target"] or 0
    actual = pace["actual"]
    expected = pace["expected_by_now"]
    fill_pct = min((actual / target) * 100, 100) if target else 0
    tick_pct = min((expected / target) * 100, 100) if target and expected is not None else 0
    return {
        "label": label,
        "actual": actual,
        "target": pace["target"],
        "unit": unit,
        "fill_pct": round(fill_pct, 1),
        "tick_pct": round(tick_pct, 1),
        "on_pace": pace["on_pace"],
    }


@app.get("/", response_class=HTMLResponse)
def today(request: Request):
    conn = db.get_connection()
    try:
        goals = config.GOALS
        today_date = date.today()

        readiness = analytics.readiness_today(conn, today_date)
        steps = analytics.steps_pace(conn, goals, today_date)
        strength = analytics.strength_pace(conn, goals, today_date)
        racquet = analytics.racquet_pace(conn, goals, today_date)
        yesterday = analytics.yesterday_summary(conn, today_date)
        yesterday["activity_labels"] = [
            f"{(a['type'] or 'activity').replace('_', ' ').title()} {round(a['duration_min'])}min"
            for a in yesterday["activities"]
        ]

        today_rows = db.fetch_all_dicts(
            conn, "SELECT * FROM daily_metrics WHERE date = ?", (today_date.isoformat(),)
        )
        today_metrics = today_rows[0] if today_rows else {}

        rails = [
            _rail("Steps · Today", steps),
            _rail("Strength · This Month", strength, unit=" sessions"),
            _rail("Racquet · This Week", racquet, unit=" sessions"),
        ]

        return templates.TemplateResponse(
            request,
            "today.html",
            {
                "active_page": "today",
                "today_label": (today_date.strftime("%A, %B ") + str(today_date.day)).upper(),
                "readiness": readiness,
                "today_metrics": today_metrics,
                "rails": rails,
                "yesterday": yesterday,
            },
        )
    finally:
        conn.close()


@app.get("/activity", response_class=HTMLResponse)
def activity(request: Request):
    conn = db.get_connection()
    try:
        weekly_calories = analytics.weekly_calories_by_bucket(conn)
        weekday_cycle = analytics.weekday_step_cycle(conn)
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
                "heatmap_svg": charts.heatmap_svg(heatmap),
                "heatmap_month": heatmap["month"],
                "avg_calories": avg_calories,
                "activities": activity_rows,
            },
        )
    finally:
        conn.close()
