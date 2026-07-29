# Legacy (retired) code

Superseded by the Fitness Dashboard rebuild (see the design spec / plan).
Kept for reference, not imported by anything active:

- `dashboard.py` — old Streamlit dashboard; replaced by a FastAPI + HTML/CSS/JS
  instrument-panel app (Phase 2).
- `goals.py` — old Python-constant goals; replaced by `config/goals.yaml`.
- `coach.py` — old rule-based goal-gap text generator; replaced by
  `garmin_tracker/analytics.py` (pure pace/trend math) + an LLM-based coach
  module (Phase 3).
- `coach_email.py` — old SMTP-sending script; the Gmail SMTP send logic will
  be reused in Phase 3 to deliver the new LLM-generated brief/review, but the
  content-building code here is retired along with `coach.py`.

None of these reference the current schema (`daily_metrics` etc.) - they still
expect the old `daily_stats`/`sleep` tables and will not run against the
current database.
