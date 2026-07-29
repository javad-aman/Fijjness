"""Personal goals the coach email compares your stats against.

Edit these anytime — they're plain constants, no migration needed.
"""

DAILY_STEPS_GOAL = 10_000
SLEEP_HOURS_GOAL = 8.0
WORKOUTS_PER_WEEK_GOAL = 4
STRESS_AVG_MAX = 25  # "low stress" ceiling; above this is worth flagging

# No fixed resting-HR target by default — trend direction (down/flat/up
# over the last 7 vs prior 7 days) matters more than a specific number for
# most people. Set a number here if you have one from a doctor/coach.
RESTING_HR_TARGET = None
