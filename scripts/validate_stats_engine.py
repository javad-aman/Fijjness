"""Synthetic validation harness for garmin_tracker/stats_engine.py.

The design spec's explicit rule: this must pass BEFORE the stats engine is
ever run against real data. It generates synthetic daily series with (a)
known injected lagged effects at realistic effect sizes and (b) known nulls
(pure noise, and noise with day-of-week seasonality only - the case that
would fool a naive correlation but must not fool the weekday-partialled
test), then asserts the known effects get surfaced and the known nulls get
suppressed. If it surfaces the nulls, the gates are wrong - do not skip this
and do not weaken it to make it pass.

Run: python scripts/validate_stats_engine.py
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from garmin_tracker import stats_engine as se

SEED = 42
N_DAYS = 300
START = date(2025, 1, 1)

failures = []


def check(name: str, condition: bool, detail: str = ""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" - {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def make_dates(n: int, start: date = START) -> list:
    return [start + timedelta(days=i) for i in range(n)]


def weekday_effect(dates: list, amplitude: float) -> np.ndarray:
    """A deterministic per-weekday offset - shared structure that a naive
    (non-partialled) correlation test would mistake for a real relationship
    between two otherwise-independent series."""
    weekly = np.array([0.0, 0.3, -0.2, 0.5, -0.4, 0.8, -0.9])  # arbitrary weekly shape
    return np.array([amplitude * weekly[d.weekday()] for d in dates])


# ---- Section 1: lagged hypotheses (the core anti-p-hacking machinery) -----

def build_synthetic_by_date(seed: int = SEED) -> dict:
    rng = np.random.default_rng(seed)
    dates = make_dates(N_DAYS)
    n = N_DAYS
    lag = 1

    # H1 (REAL EFFECT): training_load(D) -> resting_hr(D+1), coef chosen for
    # a realistic-but-clear true correlation (~0.5) once weekday is partialled.
    training_load = np.clip(30 + 15 * rng.normal(size=n), 0, None)
    resting_hr = np.full(n, 58.0)
    resting_hr[lag:] += 0.35 * training_load[:-lag]
    resting_hr += weekday_effect(dates, amplitude=1.5)
    resting_hr += rng.normal(scale=4.0, size=n)

    # H2 (REAL EFFECT): sleep_score(D) -> body_battery_wake(D+1).
    sleep_score = np.clip(75 + 12 * rng.normal(size=n), 0, 100)
    body_battery_wake = np.full(n, 40.0)
    body_battery_wake[lag:] += 0.5 * (sleep_score[:-lag] - 75)
    body_battery_wake += weekday_effect(dates, amplitude=2.0)
    body_battery_wake += rng.normal(scale=8.0, size=n)
    body_battery_wake = np.clip(body_battery_wake, 5, 100)

    # H3 (KNOWN NULL): stress_avg(D) -> sleep_score(D+1). Pure noise, no
    # relationship, no shared structure at all.
    stress_avg = np.clip(35 + 15 * rng.normal(size=n), 0, 100)
    # (sleep_score already generated above with no dependence on stress_avg)

    # H4 (KNOWN NULL, WITH SHARED WEEKDAY SEASONALITY): training_day(D) ->
    # hrv_status_numeric(D+1). Both series carry independent weekday
    # structure but no causal link - the trap a non-partialled test would
    # fail on.
    training_day_prob = 0.3 + 0.15 * np.array([1 if d.weekday() in (1, 3) else 0 for d in dates])
    training_day = (rng.random(n) < training_day_prob).astype(float)
    hrv_numeric = weekday_effect(dates, amplitude=1.0) + rng.normal(scale=1.0, size=n)
    hrv_numeric = np.clip(np.round(hrv_numeric), -1, 1)

    # H5 (KNOWN NULL): resting_hr(D) -> steps(D+1). Pure noise.
    steps = np.clip(8000 + 2000 * rng.normal(size=n), 0, None)

    by_date = {}
    for i, d in enumerate(dates):
        by_date[d.isoformat()] = {
            "date": d.isoformat(),
            "training_load": float(training_load[i]),
            "resting_hr": float(resting_hr[i]),
            "sleep_score": float(sleep_score[i]),
            "body_battery_wake": float(body_battery_wake[i]),
            "stress_avg": float(stress_avg[i]),
            "training_day": bool(training_day[i]),
            "hrv_status_numeric": float(hrv_numeric[i]),
            "steps": float(steps[i]),
        }
    return by_date


def test_lagged_hypotheses():
    by_date = build_synthetic_by_date()
    series_by_hypothesis = {}
    for spec in se.HYPOTHESES:
        dates, x, y = se._aligned_pairs(by_date, spec["predictor"], spec["outcome"], spec["lag_days"])
        check(f"{spec['id']}: enough aligned data ({len(dates)} days)", len(dates) >= se.MIN_N_EFFECTIVE)
        series_by_hypothesis[spec["id"]] = (dates, x, y)

    results = se.run_lagged_hypotheses(series_by_hypothesis)
    by_id = {r["id"]: r for r in results}

    check("H1 (real effect: training_load->resting_hr) surfaced",
          by_id["H1"]["status"] == "surfaced",
          f"effect={by_id['H1']['effect_size']} q={by_id['H1']['q_value']} ci=({by_id['H1']['ci_low']},{by_id['H1']['ci_high']})")
    check("H2 (real effect: sleep_score->body_battery_wake) surfaced",
          by_id["H2"]["status"] == "surfaced",
          f"effect={by_id['H2']['effect_size']} q={by_id['H2']['q_value']} ci=({by_id['H2']['ci_low']},{by_id['H2']['ci_high']})")
    check("H3 (known null: stress_avg->sleep_score) suppressed",
          by_id["H3"]["status"] == "suppressed",
          f"effect={by_id['H3']['effect_size']} q={by_id['H3']['q_value']}")
    check("H4 (known null WITH shared weekday seasonality) suppressed",
          by_id["H4"]["status"] == "suppressed",
          f"effect={by_id['H4']['effect_size']} q={by_id['H4']['q_value']} - "
          "this is the case a non-partialled test would have falsely surfaced")
    check("H5 (known null: resting_hr->steps) suppressed",
          by_id["H5"]["status"] == "suppressed",
          f"effect={by_id['H5']['effect_size']} q={by_id['H5']['q_value']}")


# ---- Section 2: STL anomaly detection --------------------------------------

def test_anomaly_detection():
    n = 120
    dates = make_dates(n)

    # A single clean draw's flag count is itself a noisy statistic - even a
    # perfectly-calibrated z>3 test on iid Gaussian noise flags ~0.27% of
    # points by chance. The honest check is the false-positive RATE across
    # many independent draws, not "exactly zero in one run".
    n_trials = 30
    total_points = 0
    total_flagged = 0
    for trial in range(n_trials):
        rng = np.random.default_rng(SEED + 100 + trial)
        baseline = 55 + 3 * np.sin(2 * np.pi * np.arange(n) / 7)  # mild weekly rhythm
        clean = baseline + rng.normal(scale=1.5, size=n)
        result = se.stl_anomalies(dates, list(clean))
        total_points += n
        total_flagged += len(result["anomalies"])

    false_positive_rate = total_flagged / total_points
    check("Clean seasonal+noise series: false-positive rate stays low across draws",
          false_positive_rate < 0.01,
          f"{total_flagged}/{total_points} = {false_positive_rate:.4f} "
          f"(threshold z>{se.ANOMALY_Z_THRESHOLD}, expect roughly 1 flag per year on a 90-day window)")

    rng = np.random.default_rng(SEED + 1)
    baseline = 55 + 3 * np.sin(2 * np.pi * np.arange(n) / 7)
    clean = baseline + rng.normal(scale=1.5, size=n)
    injected = clean.copy()
    spike_idx = 60
    injected[spike_idx] += 25  # a real, large one-day spike
    result_spike = se.stl_anomalies(dates, list(injected))
    flagged_dates = {a["date"] for a in result_spike["anomalies"]}
    check("Injected 1-day spike is flagged as an anomaly",
          dates[spike_idx].isoformat() in flagged_dates,
          f"flagged: {flagged_dates}")


# ---- Section 3: rest-recommendation hard gate ------------------------------

def test_rest_gate():
    baseline_hr = 55.0
    good_days = [
        {"resting_hr": 55, "hrv_status": "BALANCED", "sleep_score": 80},
        {"resting_hr": 56, "hrv_status": "BALANCED", "sleep_score": 78},
        {"resting_hr": 55, "hrv_status": "BALANCED", "sleep_score": 82},
    ]
    check("Rest gate: normal days -> not flagged",
          se.rest_recommendation_gate(good_days, baseline_hr)["flagged"] is False)

    bad_days = [
        {"resting_hr": 58, "hrv_status": "LOW", "sleep_score": 70},
        {"resting_hr": 61, "hrv_status": "LOW", "sleep_score": 60},
        {"resting_hr": 62, "hrv_status": "UNBALANCED", "sleep_score": 50},
    ]
    check("Rest gate: HR+HRV+falling sleep for 2 consecutive days -> flagged",
          se.rest_recommendation_gate(bad_days, baseline_hr)["flagged"] is True)

    one_bad_day = [
        {"resting_hr": 55, "hrv_status": "BALANCED", "sleep_score": 80},
        {"resting_hr": 56, "hrv_status": "BALANCED", "sleep_score": 78},
        {"resting_hr": 62, "hrv_status": "LOW", "sleep_score": 60},
    ]
    check("Rest gate: only 1 bad day (not sustained) -> not flagged",
          se.rest_recommendation_gate(one_bad_day, baseline_hr)["flagged"] is False)


# ---- Section 4: Hedges' g training-day vs. rest-day comparison ------------

def test_hedges_g():
    rng = np.random.default_rng(SEED + 2)

    training_real = rng.normal(loc=68, scale=6, size=40)  # real effect
    rest_real = rng.normal(loc=75, scale=6, size=40)
    result_real = se.compare_training_vs_rest(training_real, rest_real,
                                               rng=np.random.default_rng(SEED + 3))
    check("Hedges' g: known real group difference surfaced",
          result_real["status"] == "surfaced",
          f"g={result_real['effect_size']} ci=({result_real['ci_low']},{result_real['ci_high']})")

    training_null = rng.normal(loc=72, scale=6, size=40)  # known null
    rest_null = rng.normal(loc=72, scale=6, size=40)
    result_null = se.compare_training_vs_rest(training_null, rest_null,
                                               rng=np.random.default_rng(SEED + 4))
    check("Hedges' g: known null (no group difference) suppressed",
          result_null["status"] == "suppressed",
          f"g={result_null['effect_size']} ci=({result_null['ci_low']},{result_null['ci_high']})")

    result_small_n = se.compare_training_vs_rest(np.array([1.0, 2.0]), np.array([3.0, 4.0]))
    check("Hedges' g: n<10 per group is skipped (not surfaced)",
          result_small_n["status"] == "suppressed" and result_small_n["effect_size"] is None)


def main():
    print("=== Section 1: pre-registered lagged hypotheses ===")
    test_lagged_hypotheses()
    print("\n=== Section 2: STL anomaly detection ===")
    test_anomaly_detection()
    print("\n=== Section 3: rest-recommendation hard gate ===")
    test_rest_gate()
    print("\n=== Section 4: Hedges' g training-day vs. rest-day ===")
    test_hedges_g()

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s) did not pass: {failures}")
        sys.exit(1)
    print("All checks passed. Safe to run stats_engine against real data.")


if __name__ == "__main__":
    main()
