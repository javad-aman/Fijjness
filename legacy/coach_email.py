"""Build and send the daily coach email.

Usage:
    python -m garmin_tracker.coach_email            # send for real
    python -m garmin_tracker.coach_email --dry-run   # print the email, don't send
"""
from __future__ import annotations

import argparse
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from garmin_tracker import config, coach, db

GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 587


def _fmt(value, decimals=0, suffix=""):
    if value is None:
        return "no data"
    return f"{value:,.{decimals}f}{suffix}"


def render_email(report: dict) -> tuple[str, str]:
    """Return (subject, html_body)."""
    steps_gap = report["steps_gap"]
    steps_line = (
        f"You're averaging {_fmt(report['steps_avg_7d'])} steps/day this week"
        + (
            f", {_fmt(abs(steps_gap))} short of your {_fmt(report['steps_goal'])}/day goal."
            if steps_gap is not None and steps_gap < 0
            else f" — at or above your {_fmt(report['steps_goal'])}/day goal, nice work."
            if steps_gap is not None
            else "."
        )
    )

    sleep_gap = report["sleep_hours_gap"]
    sleep_line = (
        f"Sleep is averaging {_fmt(report['sleep_hours_avg_7d'], 1, 'h')}/night"
        + (
            f", {_fmt(abs(sleep_gap), 1, 'h')} under your {_fmt(report['sleep_hours_goal'], 1, 'h')} goal."
            if sleep_gap is not None and sleep_gap < 0
            else f" — meeting your {_fmt(report['sleep_hours_goal'], 1, 'h')} goal."
            if sleep_gap is not None
            else "."
        )
    )
    if report["sleep_score_avg_7d"] is not None:
        sleep_line += f" Avg sleep score: {_fmt(report['sleep_score_avg_7d'])}."

    workouts_gap = report["workouts_gap"]
    workouts_line = (
        f"{report['workouts_this_week']} workout(s) this week"
        + (
            f", {abs(workouts_gap)} short of your {report['workouts_goal']}/week goal."
            if workouts_gap < 0
            else f" — hit your {report['workouts_goal']}/week goal."
        )
    )

    hr_arrow = coach.trend_arrow(report["resting_hr_avg_7d"], report["resting_hr_avg_prev7d"])
    hr_line = f"Resting HR averaging {_fmt(report['resting_hr_avg_7d'], 1)} bpm this week ({hr_arrow} vs last week)."

    stress_line = ""
    if report["stress_avg_7d"] is not None:
        flag = " (above your comfort ceiling)" if report["stress_avg_7d"] > report["stress_max_goal"] else ""
        stress_line = f"Avg stress this week: {_fmt(report['stress_avg_7d'])}{flag}."

    y = report["yesterday_stats"] or {}
    yesterday_line = (
        f"Yesterday ({report['yesterday'].isoformat()}): "
        f"{_fmt(y.get('steps'))} steps, resting HR {_fmt(y.get('resting_hr'))}, "
        f"stress {_fmt(y.get('stress_avg'))}."
    )

    dashboard_line = (
        f'<p><a href="{config.DASHBOARD_URL}">View the full dashboard</a></p>'
        if config.DASHBOARD_URL
        else ""
    )

    subject = f"Your Garmin coach — {report['today'].isoformat()}"
    html = f"""
    <html><body style="font-family: sans-serif; color: #222;">
      <h2>Daily check-in</h2>
      <p>{yesterday_line}</p>
      <h3>This week</h3>
      <ul>
        <li>{steps_line}</li>
        <li>{sleep_line}</li>
        <li>{workouts_line}</li>
        <li>{hr_line}</li>
        {f"<li>{stress_line}</li>" if stress_line else ""}
      </ul>
      {dashboard_line}
    </body></html>
    """
    return subject, html


def send_email(subject: str, html_body: str) -> None:
    if not config.GMAIL_ADDRESS or not config.GMAIL_APP_PASSWORD:
        raise RuntimeError(
            "GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set in .env to send email"
        )
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
    parser = argparse.ArgumentParser(description="Send the daily Garmin coach email.")
    parser.add_argument("--dry-run", action="store_true", help="Print the email instead of sending it.")
    args = parser.parse_args()

    with db.connect() as conn:
        report = coach.build_report(conn)

    subject, html = render_email(report)

    if args.dry_run:
        print(f"SUBJECT: {subject}\n")
        print(html)
    else:
        send_email(subject, html)
        print(f"Sent: {subject}")


if __name__ == "__main__":
    main()
