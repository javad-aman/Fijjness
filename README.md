# Garmin Health Tracker

Pulls your historical and ongoing Garmin Connect data into a local SQLite
database, then visualizes it with a Streamlit dashboard.

Uses the unofficial [`garminconnect`](https://pypi.org/project/garminconnect/)
package (built on `garth` for OAuth) to talk to Garmin Connect.

## What it tracks

- **Daily stats**: resting HR, steps, stress, Body Battery, respiration, SpO2
- **Sleep**: duration, sleep score, sleep stages (deep/light/REM/awake)
- **Body composition**: weight, BMI, body fat %, muscle/bone mass, body water %
- **Activities**: type, duration, distance, avg/max HR, calories, pace, training effect
- **Training status**: VO2 max (running/cycling), training status, training load

## Setup

1. Create and activate a virtual environment:

   ```bash
   python -m venv venv
   # Windows
   venv\Scripts\activate
   # macOS/Linux
   source venv/bin/activate
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and fill in your Garmin Connect credentials:

   ```bash
   cp .env.example .env
   ```

   `.env` is git-ignored and never read by anything but your local machine.
   Your OAuth session is cached in `.garmin_tokens/` after the first login, so
   you won't be prompted to log in again on subsequent runs (until the token
   expires, at which point it will silently re-authenticate with your
   credentials from `.env`).

## Usage

**First run — full 12-month historical backfill:**

```bash
python -m garmin_tracker.sync --full
```

This walks day-by-day over the last 365 days (configurable via `--days N` or
`GARMIN_BACKFILL_DAYS` in `.env`) and can take a while since it paces requests
to stay rate-limit friendly (~0.6s between calls by default).

**Ongoing — incremental daily sync:**

```bash
python -m garmin_tracker.sync
```

Each data source (`daily_stats`, `sleep`, `body_composition`, `activities`,
`training_status`) tracks its own last-synced date in the `sync_state` table,
so this only fetches what's new since the last run. Safe to re-run any time —
writes are idempotent (`INSERT OR REPLACE` keyed by date/activity id).

To automate this, schedule it daily, e.g. with Windows Task Scheduler or a
cron job:

```bash
0 6 * * * cd /path/to/jfit && venv/bin/python -m garmin_tracker.sync
```

**Dashboard:**

```bash
streamlit run dashboard.py
```

Shows:
- Summary cards (workouts, avg sleep score, avg stress, avg resting HR) for a selectable time range
- Workout frequency over time, stacked by activity type
- Resting HR, sleep score, and stress trend lines
- A calendar heatmap of workout days
- A raw activities table for spot-checking

## Data & storage

- SQLite database: `data/garmin.db` (git-ignored — this is your personal health data)
- Cached OAuth session: `.garmin_tokens/` (git-ignored)
- To start over from scratch, delete `data/garmin.db` and re-run `--full`

## Backups

`data/garmin.db` is continuously backed up to a private Cloudflare R2 bucket
(free tier) via [Litestream](https://litestream.io), which streams SQLite's
WAL to object storage in near-real-time.

- Binary + config: `tools/litestream.exe`, `litestream.yml` (both git-ignored —
  `litestream.yml` is generated from `litestream.yml.example`, credentials
  come from the `LITESTREAM_*` vars in `.env`)
- Runs automatically at login via a script in the Windows Startup folder
  (`tools/run_litestream.ps1`, launched by a `.vbs` wrapper so it starts
  hidden, no console window) — Task Scheduler wasn't available on this
  account, so Startup is the persistence mechanism instead
- Logs: `litestream.log` / `litestream.err.log` (git-ignored)
- **Restore** (e.g. after a disk failure): 
  ```bash
  tools/litestream.exe restore -config litestream.yml -o data/garmin.db data/garmin.db
  ```
- The database runs in WAL journal mode (`PRAGMA journal_mode=WAL`, set
  automatically in `garmin_tracker/db.py`) — required for Litestream to work,
  and a nice side effect: the dashboard can read while a sync is writing.

Setup steps (only needed once, already done for this install): create a free
Cloudflare account → R2 Object Storage → create a bucket → create an R2 API
token (Object Read & Write) → put the bucket name, endpoint URL, access key
ID, and secret access key into `.env` as `LITESTREAM_BUCKET`,
`LITESTREAM_ENDPOINT`, `LITESTREAM_ACCESS_KEY_ID`, `LITESTREAM_SECRET_ACCESS_KEY`.

## Daily coach email

`garmin_tracker/coach_email.py` sends a short daily digest (yesterday's
stats, this week's averages, progress vs. your goals in `garmin_tracker/goals.py`)
via Gmail SMTP.

```bash
python -m garmin_tracker.coach_email --dry-run   # print instead of sending
python -m garmin_tracker.coach_email             # send for real
```

Requires in `.env`: `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` (a Google Account →
Security → **App passwords** value, not your normal Gmail password), and
optionally `TO_EMAIL` (defaults to `GMAIL_ADDRESS`) and `DASHBOARD_URL` (adds
a link to the dashboard at the bottom of the email).

Edit `garmin_tracker/goals.py` anytime to change the step/sleep/workout
targets it compares against.

## Cloud setup — always-on email + a dashboard link

The local setup above only runs when your machine does. To get a daily email
and a dashboard reachable from any device even when your PC is off, three
pieces move to the cloud: a shared database (Turso), a scheduled job (GitHub
Actions), and a hosted dashboard (Streamlit Community Cloud). None of this
is required for local-only use — skip it if you don't need always-on access.

**1. Turso (shared database)**

Both the GitHub Actions job and the hosted dashboard need to read/write the
same database, and it has to be separate from the (public) code repo since
it holds your personal health data. [Turso](https://turso.tech) is a free,
SQLite-compatible hosted database that fits this.

- Create a free account and a database at turso.tech (or via their CLI).
- Get the database's `libsql://...` URL and an auth token
  (`turso db tokens create <database-name>`).
- Add both to your local `.env` as `TURSO_DATABASE_URL` / `TURSO_AUTH_TOKEN`
  and confirm a local sync writes there correctly before trusting it in CI:
  ```bash
  python -m garmin_tracker.sync --days 7
  ```
  (`db.py` talks to Turso over its plain HTTP API directly — no extra native
  dependency required, since the official Turso Python client only ships
  prebuilt wheels for Linux/macOS.)

**2. Gmail app password**

Google Account → Security → 2-Step Verification → **App passwords** → create
one for "Mail". Use that (not your normal password) as `GMAIL_APP_PASSWORD`.

**3. GitHub repo + Actions secrets**

- Push this repo to GitHub (public is fine — `.gitignore` already keeps
  `.env`, `.garmin_tokens/`, and `data/` out of it, so no personal data or
  secrets ever get committed).
- In the repo's Settings → Secrets and variables → Actions, add:
  `GARMIN_EMAIL`, `GARMIN_PASSWORD`, `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`,
  `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `TO_EMAIL`, `DASHBOARD_URL`.
- Also add `GARMIN_TOKENS_SEED_B64`: a one-time bootstrap so the first cloud
  run doesn't hit Garmin's MFA/bot-detection wall. Generate it from your
  already-working local session:
  ```bash
  base64 -w0 .garmin_tokens/garmin_tokens.json   # macOS: base64 .garmin_tokens/garmin_tokens.json
  ```
  Paste the output as the secret's value. After the first successful run,
  the workflow's rolling cache takes over and this seed is no longer needed
  (safe to leave in place).
- `.github/workflows/daily.yml` runs on a daily schedule (defaults to ~7am
  US Central — edit the `cron:` line for a different time/timezone) and can
  also be triggered manually from the Actions tab (`workflow_dispatch`) to
  test it immediately.

**4. Streamlit Community Cloud (dashboard link)**

- Sign in at [share.streamlit.io](https://share.streamlit.io) with GitHub,
  point it at this repo and `dashboard.py`.
- In the app's Settings → Secrets, add: `TURSO_DATABASE_URL`,
  `TURSO_AUTH_TOKEN`, `DASHBOARD_PASSWORD` (a password of your choice — the
  dashboard shows a password prompt before any data loads whenever this is
  set, and skips the gate entirely if it's unset).
- You'll get a persistent `*.streamlit.app` URL that always shows current
  data, refreshed daily by the GitHub Actions sync.

## Notes & limitations

- Garmin's API is unofficial/undocumented and can change without notice or
  return slightly different fields depending on your device — the sync code
  is defensive (missing fields become `NULL` rather than crashing the run)
  and logs a warning per failed field/date rather than aborting.
- True HRV isn't exposed via this API for most consumer accounts, so it's not
  tracked here; resting HR, sleep score, and stress are used as the closest
  available recovery signals.
- Be mindful of Garmin's terms of service and rate limits — this project
  paces requests and caches your session specifically to avoid hammering
  their API.

## Project layout

```
jfit/
  .env.example          # template for credentials/config
  requirements.txt
  garmin_tracker/
    config.py           # loads .env
    db.py                # DB schema + upsert helpers (local SQLite or Turso)
    garmin_client.py     # auth, token caching, retry/backoff
    models.py            # raw API payload -> DB row parsing
    sync.py              # CLI: full backfill or incremental sync
    goals.py             # editable step/sleep/workout targets
    coach.py             # trend + goal-gap calculations
    coach_email.py        # CLI: build + send the daily coach email
  dashboard.py           # Streamlit app (password-gated when deployed)
  .github/workflows/daily.yml   # scheduled cloud sync + email
  data/garmin.db         # created on first sync (git-ignored, local mode only)
```
