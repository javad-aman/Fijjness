"""Send the daily coach brief or weekly review email.

Per spec: the LLM is only ever called on the generation schedule (see
coach.py / the GitHub Actions workflow) - this script just renders and sends
whatever is already stored in the `briefs` table. It never calls Claude.

Usage:
    python -m garmin_tracker.coach_email --kind daily            # send
    python -m garmin_tracker.coach_email --kind daily --dry-run   # print
    python -m garmin_tracker.coach_email --kind weekly
"""
from __future__ import annotations

import argparse
import html
import json
import re
import smtplib
import ssl
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from garmin_tracker import analytics, config, db

GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 587

# How far a monthly/weekly session count is allowed to drift from a direct
# SQL recount before the gate blocks the send - a real discrepancy here means
# the snapshot and the database have already disagreed once (see fix #1/#2).
SESSION_COUNT_TOLERANCE = 1
# How far ACWR is allowed to move day-over-day without a logged activity to
# explain it - a jump bigger than this with no activity behind it means one
# of the two computations resolved a different date window (see fix #3).
ACWR_JUMP_TOLERANCE = 0.3


def _render_cell(text: str) -> str:
    """Escape and render inline markdown (currently just **bold**) within a
    table cell - _render_table used to insert cell text raw, so a cell like
    "**no data**" leaked its literal asterisks into the email instead of
    rendering as bold."""
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html.escape(text))


def _render_table(lines: list[str]) -> str:
    rows = [
        [c.strip() for c in line.strip().strip("|").split("|")]
        for line in lines
        if not re.match(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$", line.strip())
    ]
    if not rows:
        return ""
    header, *body = rows
    thead = "".join(f"<th style='text-align:left;padding:4px 10px;border-bottom:1px solid #ccc;'>{_render_cell(c)}</th>" for c in header)
    trs = "".join(
        "<tr>" + "".join(f"<td style='padding:4px 10px;border-bottom:1px solid #eee;'>{_render_cell(c)}</td>" for c in r) + "</tr>"
        for r in body
    )
    return f"<table style='border-collapse:collapse;font-size:14px;margin:8px 0;'><thead><tr>{thead}</tr></thead><tbody>{trs}</tbody></table>"


def _markdown_to_html(text: str) -> str:
    """Minimal markdown rendering - headers, **bold**, pipe tables, and
    paragraph breaks. Covers what Claude actually produces for these two
    brief types (short prose for the daily brief; headers/tables/lists for
    the weekly review); not meant to handle arbitrary markdown."""
    blocks = [b for b in text.split("\n\n") if b.strip()]
    out = []
    for block in blocks:
        lines = block.strip("\n").split("\n")
        if all("|" in l for l in lines) and len(lines) >= 2:
            out.append(_render_table(lines))
            continue

        if len(lines) == 1 and re.match(r"^#{1,3}\s+.*$", lines[0]):
            m = re.match(r"^(#{1,3})\s+(.*)$", lines[0])
            level = len(m.group(1)) + 2  # markdown h1 -> html h3, etc. (email body, not a page)
            out.append(f"<h{level}>{html.escape(m.group(2))}</h{level}>")
            continue

        rendered_lines = []
        for line in lines:
            m = re.match(r"^(#{1,3})\s+(.*)$", line)
            if m:
                level = len(m.group(1)) + 2
                rendered_lines.append(f"</p><h{level}>{html.escape(m.group(2))}</h{level}><p>")
            else:
                escaped = html.escape(line)
                escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
                rendered_lines.append(escaped)
        out.append("<p>" + "<br>".join(rendered_lines) + "</p>")
    return "".join(out)


def _pace_rail_table_row(label: str, actual, target, unit: str = "") -> str:
    value = "not yet synced" if actual is None else f"{actual}{unit} / {target}{unit}"
    return (
        f"<tr><td style='padding:4px 12px 4px 0;color:#7C8794;'>{html.escape(label)}</td>"
        f"<td style='padding:4px 0;font-family:monospace;'>{value}</td></tr>"
    )


def validate_snapshot(conn, brief: dict, intended_date: Optional[str] = None) -> list[str]:
    """Pre-send gate: every check that must pass before a coaching brief is
    allowed out the door. A wrong brief is worse than no brief - this gate
    is the point. Returns a list of failure descriptions; empty means clean.
    Deliberately redundant with checks already done at generation time (the
    build_snapshot assertion, the readiness gate) - this re-verifies against
    the stored snapshot independently, since generation and send are
    separate jobs that could in principle disagree."""
    intended_date = intended_date or config.local_today().isoformat()
    snapshot = json.loads(brief["metrics_snapshot_json"])
    failures = []

    snapshot_date_str = snapshot.get("snapshot_date")
    if snapshot_date_str != intended_date:
        failures.append(f"snapshot_date ({snapshot_date_str}) does not match intended date ({intended_date})")
        return failures  # every other check below assumes a valid, parseable date

    snapshot_date = date.fromisoformat(snapshot_date_str)

    today_steps = (snapshot.get("steps_pace") or {}).get("actual")
    yesterday_steps = (snapshot.get("yesterday") or {}).get("steps")
    if today_steps is not None and yesterday_steps is not None and today_steps != 0 and today_steps == yesterday_steps:
        failures.append(f"today's steps ({today_steps}) equal yesterday's steps - likely a date-resolution bug")

    if re.search(r"\bNone\b|\bnull\b|\bNaN\b", brief.get("body_markdown", "")):
        failures.append("brief text contains a literal None/null/NaN token")

    month_start = snapshot_date.replace(day=1)
    strength_actual = (snapshot.get("strength_pace") or {}).get("actual")
    if strength_actual is not None:
        sql_count = db.fetch_all_dicts(
            conn, "SELECT COUNT(*) as n FROM activities WHERE date >= ? AND date <= ? AND bucket = 'strength'",
            (month_start.isoformat(), snapshot_date.isoformat()),
        )[0]["n"]
        if abs(strength_actual - sql_count) > SESSION_COUNT_TOLERANCE:
            failures.append(f"strength session count ({strength_actual}) vs. direct SQL count ({sql_count}) differ by more than {SESSION_COUNT_TOLERANCE}")

    week_start = snapshot_date - timedelta(days=snapshot_date.weekday())
    racquet_actual = (snapshot.get("racquet_pace") or {}).get("actual")
    if racquet_actual is not None:
        sql_count = db.fetch_all_dicts(
            conn, "SELECT COUNT(*) as n FROM activities WHERE date >= ? AND date <= ? AND bucket = 'racquet'",
            (week_start.isoformat(), snapshot_date.isoformat()),
        )[0]["n"]
        if abs(racquet_actual - sql_count) > SESSION_COUNT_TOLERANCE:
            failures.append(f"racquet session count ({racquet_actual}) vs. direct SQL count ({sql_count}) differ by more than {SESSION_COUNT_TOLERANCE}")

    acwr = (snapshot.get("acute_chronic_load_ratio") or {}).get("ratio")
    if acwr is not None:
        prior_ratio = analytics.acute_chronic_load_ratio(conn, snapshot_date - timedelta(days=1)).get("ratio")
        if prior_ratio is not None and abs(acwr - prior_ratio) > ACWR_JUMP_TOLERANCE:
            activity_logged = bool(db.fetch_all_dicts(
                conn, "SELECT 1 FROM activities WHERE date = ? LIMIT 1", (snapshot_date.isoformat(),)
            ))
            if not activity_logged:
                failures.append(f"ACWR jumped from {prior_ratio} to {acwr} with no logged activity to explain it")

    if (snapshot.get("sync_status") or {}).get("any_stale"):
        stale = [n for n, i in snapshot["sync_status"]["sources"].items() if i.get("stale")]
        failures.append(f"sync not completed within the last {analytics.STALE_AFTER_HOURS}h: {', '.join(stale)}")

    return failures


def render_incomplete_notice(kind: str, intended_date: str, failures: list[str]) -> tuple[str, str]:
    """What gets sent instead of a coaching brief when validate_snapshot()
    finds a problem - names the failed check rather than guessing at a
    number that might be wrong."""
    label = "Daily Brief" if kind == "daily" else "Weekly Review"
    subject = f"Your coach — {label} — data incomplete — {intended_date}"
    failure_list = "".join(f"<li>{html.escape(f)}</li>" for f in failures)
    html_body = f"""
    <html><body style="font-family: sans-serif; color: #222; max-width: 600px;">
      <h2>{label} — {intended_date}</h2>
      <p style='background:#3a1f1f;color:#F2545B;padding:10px 14px;border-radius:6px;'>
        <b>Data incomplete - no brief sent today.</b> Failed checks:
      </p>
      <ul>{failure_list}</ul>
    </body></html>
    """
    return subject, html_body


def render_email(brief: dict) -> tuple[str, str]:
    """Return (subject, html_body) for the given `briefs` row. Reads only
    from the snapshot already persisted alongside this brief - never its own
    live query - so the numbers here can never drift from the numbers the
    brief's own narrative text was generated from. Only called after
    validate_snapshot() has passed - assumes clean data, no staleness
    banner needed here anymore (a stale sync blocks the send entirely)."""
    kind = brief["kind"]
    body_html = _markdown_to_html(brief["body_markdown"])
    snapshot = json.loads(brief["metrics_snapshot_json"])

    steps_pace = snapshot.get("steps_pace") or {}
    pace_rows = (
        "<table style='border-collapse:collapse;font-size:14px;'>"
        + _pace_rail_table_row("Steps today", steps_pace.get("actual"), steps_pace.get("target"))
        + "</table>"
    )

    dashboard_link = (
        f"<p><a href='{config.DASHBOARD_URL}'>View the full dashboard</a></p>"
        if config.DASHBOARD_URL else ""
    )

    label = "Daily Brief" if kind == "daily" else "Weekly Review"
    snapshot_date = snapshot.get("snapshot_date", brief["date"])
    subject = f"Your coach — {label} — {snapshot_date}"
    html_body = f"""
    <html><body style="font-family: sans-serif; color: #222; max-width: 600px;">
      <h2>{label} — {snapshot_date}</h2>
      {body_html}
      {pace_rows}
      {dashboard_link}
    </body></html>
    """
    return subject, html_body


def send_email(subject: str, html_body: str) -> None:
    if not config.GMAIL_ADDRESS or not config.GMAIL_APP_PASSWORD:
        raise RuntimeError("GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set in .env to send email")
    if not config.TO_EMAIL:
        raise RuntimeError("TO_EMAIL must be set (or GMAIL_ADDRESS as a fallback)")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config.GMAIL_ADDRESS
    msg["To"] = config.TO_EMAIL
    msg.attach(MIMEText(html_body, "html"))

    context = ssl.create_default_context()
    with smtplib.SMTP(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT) as server:
        server.starttls(context=context)
        server.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
        server.sendmail(config.GMAIL_ADDRESS, config.TO_EMAIL, msg.as_string())


def main():
    parser = argparse.ArgumentParser(description="Send the latest stored coach brief/review email.")
    parser.add_argument("--kind", choices=["daily", "weekly"], default="daily")
    parser.add_argument("--dry-run", action="store_true", help="Print the email instead of sending it.")
    args = parser.parse_args()

    with db.connect() as conn:
        rows = db.fetch_all_dicts(
            conn, "SELECT * FROM briefs WHERE kind = ? ORDER BY date DESC LIMIT 1", (args.kind,)
        )
        if not rows:
            raise RuntimeError(f"No stored '{args.kind}' brief to send - run coach generation first.")
        brief = rows[0]

        intended_date = config.local_today().isoformat()
        failures = validate_snapshot(conn, brief, intended_date)
        if failures:
            subject, html_body = render_incomplete_notice(args.kind, intended_date, failures)
        else:
            subject, html_body = render_email(brief)

    if args.dry_run:
        print(f"SUBJECT: {subject}\n")
        print(html_body)
    else:
        send_email(subject, html_body)
        print(f"Sent: {subject}")


if __name__ == "__main__":
    main()
