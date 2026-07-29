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
import re
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from garmin_tracker import config, db

GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 587

# How stale a source's last successful sync can be before the email flags it
# rather than coaching on old numbers. Sync runs twice daily (~04:30/11:00);
# 30h covers that cadence plus slack for a missed run.
STALE_AFTER_HOURS = 30


def _render_table(lines: list[str]) -> str:
    rows = [
        [c.strip() for c in line.strip().strip("|").split("|")]
        for line in lines
        if not re.match(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$", line.strip())
    ]
    if not rows:
        return ""
    header, *body = rows
    thead = "".join(f"<th style='text-align:left;padding:4px 10px;border-bottom:1px solid #ccc;'>{c}</th>" for c in header)
    trs = "".join(
        "<tr>" + "".join(f"<td style='padding:4px 10px;border-bottom:1px solid #eee;'>{c}</td>" for c in r) + "</tr>"
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


def _stale_sources(conn) -> list[str]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=STALE_AFTER_HOURS)
    stale = []
    for source in ("daily_metrics", "activities", "training_status"):
        rows = db.fetch_all_dicts(
            conn,
            "SELECT timestamp, status FROM sync_log WHERE source = ? ORDER BY timestamp DESC LIMIT 1",
            (source,),
        )
        if not rows:
            stale.append(f"{source} (never synced)")
            continue
        last = rows[0]
        last_time = datetime.fromisoformat(last["timestamp"])
        if last["status"] != "ok":
            stale.append(f"{source} (last sync errored)")
        elif last_time < cutoff:
            stale.append(f"{source} (last synced {last_time.isoformat()})")
    return stale


def _pace_rail_table_row(label: str, actual, target, unit: str = "") -> str:
    return (
        f"<tr><td style='padding:4px 12px 4px 0;color:#7C8794;'>{html.escape(label)}</td>"
        f"<td style='padding:4px 0;font-family:monospace;'>{actual}{unit} / {target}{unit}</td></tr>"
    )


def render_email(conn, brief: dict) -> tuple[str, str]:
    """Return (subject, html_body) for the given `briefs` row."""
    kind = brief["kind"]
    body_html = _markdown_to_html(brief["body_markdown"])

    stale = _stale_sources(conn)
    stale_banner = ""
    if stale:
        stale_banner = (
            "<p style='background:#3a1f1f;color:#F2545B;padding:10px 14px;"
            "border-radius:6px;'><b>Data may be stale</b> - not coaching on "
            f"fresh numbers: {html.escape(', '.join(stale))}.</p>"
        )

    pace_rows = ""
    today_rows = db.fetch_all_dicts(conn, "SELECT * FROM daily_metrics ORDER BY date DESC LIMIT 1")
    if today_rows:
        pace_rows = (
            "<table style='border-collapse:collapse;font-size:14px;'>"
            + _pace_rail_table_row("Steps today", today_rows[0].get("steps") or 0, config.GOALS["steps"]["daily_target"])
            + "</table>"
        )

    dashboard_link = (
        f"<p><a href='{config.DASHBOARD_URL}'>View the full dashboard</a></p>"
        if config.DASHBOARD_URL else ""
    )

    label = "Daily Brief" if kind == "daily" else "Weekly Review"
    subject = f"Your coach — {label} — {brief['date']}"
    html_body = f"""
    <html><body style="font-family: sans-serif; color: #222; max-width: 600px;">
      <h2>{label}</h2>
      {stale_banner}
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
        subject, html_body = render_email(conn, rows[0])

    if args.dry_run:
        print(f"SUBJECT: {subject}\n")
        print(html_body)
    else:
        send_email(subject, html_body)
        print(f"Sent: {subject}")


if __name__ == "__main__":
    main()
