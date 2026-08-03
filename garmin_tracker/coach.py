"""LLM coach layer: builds the metrics snapshot + prompt, calls the Claude
API, and stores the result in the `briefs` table.

Per the design spec: never call the LLM on page load or on email-send - only
on the daily/weekly schedule (see coach_email.py and the GitHub Actions
workflow). The LLM interprets a fully precomputed snapshot; it never does
arithmetic on raw data (that's all in analytics.py).
"""
from __future__ import annotations

import json
from datetime import date
from typing import Optional

import anthropic
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from garmin_tracker import analytics, config, db

MODEL = "claude-sonnet-5"

RETRYABLE_ERRORS = (
    anthropic.OverloadedError,
    anthropic.RateLimitError,
    anthropic.APIConnectionError,
    anthropic.InternalServerError,
)

DAILY_SYSTEM_PROMPT = """You are a strength and conditioning coach reviewing one athlete's data.

Tone: analytical by default - reason from the numbers and cite them. When the
athlete is behind pace or repeating a known pattern, say so bluntly. No
cheerleading, no filler openers.

Output format - exactly these four labeled bullet groups, in this order,
using these literal headers verbatim. Format each header as its own markdown
line, exactly `### HEADER TEXT` (a level-3 heading, all caps, nothing else on
that line), so the sections can be parsed out programmatically. Every bullet
under a header is its own line starting with `- `. Each group is a handful
of short bullets, not a prose paragraph. Omit a group entirely (heading line
included) if it has nothing genuine to say - a placeholder restating another
group's number is worse than a missing section.

### WHERE YOU STAND
- One bullet per pace rail (steps_pace, strength_pace, racquet_pace) that
  isn't in the "cleared" state: sessions/steps remaining and days remaining,
  as plain integers only. "2 left - 1 day" is correct; "1.6 behind" is a bug
  - never print a fraction for a countable quantity.
- A rail in the "dead" state is not reachable this month (required_rate
  exceeds what the athlete has actually sustained in the last 90 days) - say
  so plainly and move on; don't urge a rate the data says isn't happening.
- Each rail's target is read from that rail's own "target" field in the
  snapshot, not assumed constant month to month - a lower target some month
  is a deliberate goal, never a shortfall to flag.
- If resting_hr_elevation.elevated is true, state it here (current vs.
  baseline, consecutive_days) - this is the overreaching signal and must not
  go unmentioned while it's active.

### WHAT YOU DID
- Yesterday's session(s) from yesterday.activities, or state plainly that it
  was a rest day (yesterday.is_rest_day) - never leave this implicit.
- Steps and active calories vs. this month's average so far, using the
  snapshot's own pct_delta fields - never compute your own percentage.

### WHAT TO DO
- One concrete instruction for today, bolded, with its consequence stated
  when a pace rail is at risk ("do X, or Y becomes unreachable by Z").
- Respect readiness: if readiness.state is "red", do not prescribe hard
  work. If readiness.state is "unknown" (Body Battery or sleep score
  missing), say plainly that readiness data is missing rather than reasoning
  from a state that isn't actually known, in either direction.

### INSIGHT
- One relationship from the findings array, worded as an association not a
  cause, that is not visible from the numbers in the other three groups. If
  nothing in findings clears that bar, omit this whole section.

Hard constraints:
- Every count of sessions or days is a plain integer, everywhere in the
  brief. Never print a decimal for a countable quantity.
- Cite specific numbers from the snapshot. Never invent one.
- You know only that a session was "strength training." You do NOT know which
  muscles were trained. Never speculate about arms, chest, or any body part
  based on session data.
- Never compute or reference a calorie deficit. Calorie burn estimates,
  especially for strength training, are unreliable. Use trend weight rate as
  the measure of energy balance.
- Do not assume any weekly schedule. Racquet sessions happen when they happen.
- You may only reference relationships present in the findings array. Absence
  means insufficient evidence, not evidence of absence - never say "no effect."
- Correlational findings are associations, not causes. Word them that way.
- If yesterday's advice was not followed, note it once, without moralizing."""

WEEKLY_SYSTEM_PROMPT = DAILY_SYSTEM_PROMPT + """

This is the WEEKLY REVIEW, not the daily brief - the four-bullet-group format
above does not apply here. Produce exactly these five sections, in this order:
1. Scorecard: each goal, target vs actual, one line each
2. What went well
3. What didn't, with the likely cause from the data
4. Next week's session plan - count and type, no day assignments unless the
   user's own history supports one
5. One goal adjustment if the data says a target is miscalibrated

State the exact date range from review_window (start to end) at the top of
the review, verbatim from the snapshot. This is always the last 7 complete
days, not necessarily the calendar week - if this review is running off its
normal schedule, the date range makes that explicit instead of silently
mislabeling a partial period as "this week". Use racquet_minutes_jump's
period_start/period_end/prior_period_start/prior_period_end (not
"this week"/"last week") when describing that comparison."""


def build_metrics_snapshot(conn, goals: dict, today: Optional[date] = None) -> dict:
    today = today or config.snapshot_date()
    snapshot = analytics.build_snapshot(conn, goals, today)
    snapshot["weekly_calories_by_bucket"] = analytics.weekly_calories_by_bucket(conn, today=today)
    snapshot["avg_calories_per_session"] = analytics.avg_calories_per_session(conn, today=today)
    # Populated starting Phase 5 - deliberately empty (not stubbed with fake
    # data) until the statistics engine exists, per the prompt's own rule
    # that absence means insufficient evidence, not evidence of absence.
    snapshot["findings"] = db.fetch_all_dicts(
        conn,
        "SELECT predictor, outcome, lag_days, effect_size, ci_low, ci_high, q_value, n_effective "
        "FROM findings WHERE status = 'surfaced' ORDER BY computed_at DESC",
    )
    return snapshot


def build_weekly_review_snapshot(conn, goals: dict, today: Optional[date] = None) -> dict:
    """Same as build_metrics_snapshot, but racquet_minutes_jump is replaced
    with the rolling (always-two-complete-weeks) version and a review_window
    is stamped in - the weekly review must never use the calendar-week
    "this week so far" comparison the daily brief uses."""
    today = today or config.snapshot_date()
    snapshot = build_metrics_snapshot(conn, goals, today)
    snapshot["review_window"] = analytics.weekly_review_window(today)
    snapshot["racquet_minutes_jump"] = analytics.rolling_racquet_minutes_jump(conn, today)
    return snapshot


def recent_briefs(conn, kind: str, limit: int) -> list[dict]:
    return db.fetch_all_dicts(
        conn,
        "SELECT date, kind, body_markdown FROM briefs WHERE kind = ? ORDER BY date DESC LIMIT ?",
        (kind, limit),
    )


def _client() -> anthropic.Anthropic:
    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY must be set in .env to generate a coach brief")
    return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def _store_brief(conn, kind: str, today: date, body: str, snapshot: dict) -> dict:
    # Delete-then-insert on (date, kind) keeps re-runs idempotent, same
    # pattern as the upsert-by-natural-key convention used elsewhere in db.py
    # (briefs' autoincrement id isn't a natural key, so db.upsert() doesn't apply).
    conn.execute("DELETE FROM briefs WHERE date = ? AND kind = ?", (today.isoformat(), kind))
    row = {
        "date": today.isoformat(),
        "kind": kind,
        "body_markdown": body,
        "metrics_snapshot_json": json.dumps(snapshot, default=str),
    }
    db.upsert(conn, "briefs", row)
    conn.commit()
    return row


@retry(
    retry=retry_if_exception_type(RETRYABLE_ERRORS),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    reraise=True,
)
def _create_message(client, system_prompt: str, user_content: str, max_tokens: int):
    return client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )


def _call_claude(system_prompt: str, user_content: str, max_tokens: int) -> str:
    client = _client()
    response = _create_message(client, system_prompt, user_content, max_tokens)
    if response.stop_reason == "max_tokens":
        raise RuntimeError(
            f"Claude response was truncated at max_tokens={max_tokens} - raise it and retry."
        )
    # Concatenate every text block, not just the first - a longer structured
    # response (e.g. the weekly review's markdown table) can come back split
    # across multiple text content blocks.
    return "".join(b.text for b in response.content if b.type == "text")


def generate_daily_brief(conn, goals: dict, today: Optional[date] = None) -> dict:
    today = today or config.snapshot_date()
    snapshot = build_metrics_snapshot(conn, goals, today)
    user_content = json.dumps({
        "metrics_snapshot": snapshot,
        "last_7_daily_briefs": recent_briefs(conn, "daily", 7),
        "last_weekly_review": (recent_briefs(conn, "weekly", 1) or [None])[0],
    }, default=str)

    body = _call_claude(DAILY_SYSTEM_PROMPT, user_content, max_tokens=2048)
    return _store_brief(conn, "daily", today, body, snapshot)


def generate_weekly_review(conn, goals: dict, today: Optional[date] = None) -> dict:
    today = today or config.snapshot_date()
    snapshot = build_weekly_review_snapshot(conn, goals, today)
    user_content = json.dumps({
        "metrics_snapshot": snapshot,
        "last_7_daily_briefs": recent_briefs(conn, "daily", 7),
        "last_weekly_review": (recent_briefs(conn, "weekly", 1) or [None])[0],
    }, default=str)

    body = _call_claude(WEEKLY_SYSTEM_PROMPT, user_content, max_tokens=8192)
    return _store_brief(conn, "weekly", today, body, snapshot)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Generate and store a coach brief/review.")
    parser.add_argument("--kind", choices=["daily", "weekly"], default="daily")
    args = parser.parse_args()

    with db.connect() as conn:
        if args.kind == "daily":
            row = generate_daily_brief(conn, config.GOALS)
        else:
            row = generate_weekly_review(conn, config.GOALS)
    print(f"Stored {row['kind']} brief for {row['date']} ({len(row['body_markdown'])} chars)")


if __name__ == "__main__":
    main()
