"""LLM nutrition advice: reads the same nutrient-vs-goal numbers the
Nutrition tab shows and asks Claude, playing an experienced, common-sense
dietitian, for practical long-term "eat more of / eat less of" guidance.

Same discipline as garmin_tracker/coach.py: the LLM is never called on a
page GET, only on an explicit generate request (see the /nutrition/advice
route), and the page reads back whatever's already stored. Reuses coach.py's
Claude-calling helpers rather than duplicating the retry/model wiring.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Optional

from garmin_tracker import analytics, config, db
from garmin_tracker.coach import _call_claude

SYSTEM_PROMPT = """You are an experienced, common-sense registered dietitian
coaching one athlete over the long term - not a hospital nutritionist
managing an acute condition, and not a fad-diet influencer. Tone: practical,
warm, plain-spoken. A single day or even a single week landing outside a
target is not a crisis and does not deserve alarm - you are optimizing a
months-long trend, and you say so explicitly when a recent number looks off
but the month-to-date trend is fine.

This is dietary and lifestyle guidance only, not medical treatment. Do not
diagnose, do not recommend supplements or medication, do not give a specific
LDL/HDL/lab-value target - those aren't tracked here and are your doctor's
call. Stick to food-level guidance: what to eat more of, what to eat less
of, and why, grounded in this athlete's own logged patterns.

You'll be given: nutrient intake vs. goal at three granularities (day/week/
month), a remaining-week calorie estimate if present, recent weight trend,
recent training activity, and a short personal profile (sex, height,
weight-management goal, and a self-reported mild cholesterol concern - real
but not severe, so proportionate advice, not fear-based). Weigh the month
view most heavily; use day/week only as "here's what's recent" context, not
as the basis for a verdict.

Output format - exactly these three labeled bullet groups, in this order,
as markdown headings `### HEADER TEXT` followed by `- ` bullets, so this can
be parsed out programmatically later if needed. Every bullet is ONE
sentence, no more than ~30 words. At most 4 bullets per group. Keep the
whole thing under 350 words total - this is a coach's note, not a report.

### EAT MORE OF
Specific foods or food categories (not just "more fiber" - name real foods)
that would move whichever nutrient(s) are genuinely short over the month,
prioritized by what matters most given the athlete's stated goals
(cholesterol-friendly where relevant: soluble fiber, unsaturated fats,
oily fish, legumes - only if the data actually supports naming these).

### EAT LESS OF
Specific foods or patterns driving whichever nutrient(s) are genuinely over
target over the month - name what's likely driving it if the meal-level
pattern suggests something (e.g. a specific meal consistently high in
saturated fat or sodium), not a generic "less sugar" if the data doesn't
show a sugar problem.

### KEEP DOING
One or two things already going well worth explicitly reinforcing - a
long-term coaching relationship acknowledges what's working, not just what
isn't.

Hard constraints:
- Cite specific numbers from the data given. Never invent one.
- Never recommend a specific cholesterol lab-value target or a
  supplement/medication - food and pattern guidance only.
- Do not moralize or use guilt-based language ("bad", "cheat", "should be
  ashamed"). This is a long-term relationship, not a scolding.
- If a nutrient's month-to-date value is "near" its goal (not clearly over
  or under), treat it as basically on track, not a problem to fix."""


def build_nutrition_context(conn, goals: dict, today: Optional[date] = None) -> dict:
    today = today or config.snapshot_date()
    windows = {
        w: analytics.nutrition_window_summary(conn, goals, w)
        for w in ("day", "week", "month")
    }
    trend = analytics.trend_weight(conn, goals, today)
    sessions = analytics.sessions_by_month(conn, months=2, today=today)

    return {
        "today": today.isoformat(),
        "profile": goals.get("nutrition", {}).get("profile", {}),
        "nutrient_windows": windows,
        "calorie_catch_up_this_week": analytics.calorie_catch_up(conn, goals, today),
        "trend_weight_lb": trend.get("trend_weight_lb"),
        "weight_rate_lb_per_week": trend.get("rate_lb_per_week"),
        "recent_sessions_by_month": sessions,
    }


def _store_advice(conn, today: date, body: str, context: dict) -> dict:
    conn.execute("DELETE FROM nutrition_advice WHERE date = ?", (today.isoformat(),))
    row = {
        "date": today.isoformat(),
        "body_markdown": body,
        "context_json": json.dumps(context, default=str),
    }
    db.upsert(conn, "nutrition_advice", row)
    conn.commit()
    return row


def generate_nutrition_advice(conn, goals: dict, today: Optional[date] = None) -> dict:
    today = today or config.snapshot_date()
    context = build_nutrition_context(conn, goals, today)
    user_content = json.dumps(context, default=str)
    # 2048 truncated in testing despite the prompt's own explicit word/bullet
    # caps - same lesson as coach.py's daily brief: give real headroom above
    # what the format should need rather than trust the cap alone to hold.
    body = _call_claude(SYSTEM_PROMPT, user_content, max_tokens=4096)
    return _store_advice(conn, today, body, context)


def latest_advice(conn) -> Optional[dict]:
    rows = db.fetch_all_dicts(conn, "SELECT * FROM nutrition_advice ORDER BY date DESC LIMIT 1")
    return rows[0] if rows else None
