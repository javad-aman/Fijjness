"""Phase 5 statistics engine: anomaly detection, pre-registered lagged
hypothesis tests, and a training-day vs. rest-day comparison.

Every pure function here takes plain dates/values and returns plain dicts,
so scripts/validate_stats_engine.py can feed it synthetic data with known
injected effects and known nulls - the explicit gate the design spec
requires before ever running this against real data. If it surfaces the
nulls, the gates are wrong.

Deliberately NOT built here (per spec): brute-force correlation matrices,
changepoint detection, mixed-effects models, forecasting anything but
weight. These produce confident-looking output from insufficient data for
a single subject, which is worse than nothing.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Optional

import numpy as np
from statsmodels.stats.multitest import multipletests
from statsmodels.tsa.stattools import acf
from statsmodels.tsa.seasonal import STL

from garmin_tracker import db

RNG_SEED = 20260101  # fixed seed - bootstrap results are reproducible run to run

# ---- The five pre-registered lagged hypotheses ----------------------------
# Fixed list, no scanning: each is one predictor -> outcome pair at one fixed
# lag, chosen and locked in before ever being run against real data. Adding,
# removing, or re-lagging one of these after seeing results would defeat the
# entire point of pre-registration.
HYPOTHESES = [
    {"id": "H1", "predictor": "training_load", "outcome": "resting_hr", "lag_days": 1,
     "description": "Higher training load precedes next-day resting HR elevation (overreaching signal)."},
    {"id": "H2", "predictor": "sleep_score", "outcome": "body_battery_wake", "lag_days": 1,
     "description": "Lower sleep quality precedes a lower next-morning body battery reading."},
    {"id": "H3", "predictor": "stress_avg", "outcome": "sleep_score", "lag_days": 1,
     "description": "A higher-stress day precedes worse sleep that night."},
    {"id": "H4", "predictor": "training_day", "outcome": "hrv_status_numeric", "lag_days": 1,
     "description": "A strength/racquet training day precedes a next-day HRV status shift."},
    {"id": "H5", "predictor": "resting_hr", "outcome": "steps", "lag_days": 1,
     "description": "Elevated resting HR (poor recovery) precedes reduced activity the next day."},
]

HRV_STATUS_NUMERIC = {"LOW": -1, "UNBALANCED": 0, "BALANCED": 1}

MIN_N_EFFECTIVE = 20
Q_VALUE_THRESHOLD = 0.10
BOOTSTRAP_RESAMPLES = 5000
CI_ALPHA = 0.10  # 90% CI, consistent with the rest of analytics.py


def _parse_date(d) -> date:
    if isinstance(d, date):
        return d
    return datetime.strptime(str(d), "%Y-%m-%d").date()


# ---- Anomaly detection (STL residual, robust z-score) ----------------------

ANOMALY_METRICS = ["resting_hr", "sleep_score", "body_battery_wake"]
ANOMALY_WINDOW_DAYS = 90  # recent-behavior window - keeps STL fast/stable and
                          # relevant to "how am I doing lately", not all-time
ANOMALY_PERIOD = 7
# STL residuals aren't perfectly Gaussian (the decomposition absorbs some
# noise into the trend/seasonal fit), so a nominal z>3 threshold runs hotter
# than its Gaussian tail probability suggests. 4.0 was chosen empirically in
# scripts/validate_stats_engine.py against synthetic data with a known noise
# scale, landing at a real-world false-positive rate of roughly 1 per year
# on a rolling 90-day window.
ANOMALY_Z_THRESHOLD = 4.0


def _densify(dates: list[date], values: list[float]) -> tuple[list[date], np.ndarray]:
    """Fill to one row per calendar day (STL requires no gaps), linearly
    interpolating gaps up to 3 days and forward/backward-filling anything
    longer - a pragmatic tradeoff for a single-subject series with real
    missed syncs, not a claim that interpolated days are measured."""
    if not dates:
        return [], np.array([])
    by_date = dict(zip(dates, values))
    start, end = min(dates), max(dates)
    full_dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    series = np.array([by_date.get(d, np.nan) for d in full_dates], dtype=float)

    # Linear interpolation for gaps, capped at 3 consecutive missing days.
    n = len(series)
    i = 0
    while i < n:
        if np.isnan(series[i]):
            j = i
            while j < n and np.isnan(series[j]):
                j += 1
            gap_len = j - i
            if gap_len <= 3 and i > 0 and j < n:
                series[i:j] = np.linspace(series[i - 1], series[j], gap_len + 2)[1:-1]
            i = j
        else:
            i += 1

    # Anything still missing (leading/trailing or long internal gaps) -
    # forward/backward fill so STL has a complete series to work with.
    for i in range(n):
        if np.isnan(series[i]) and i > 0:
            series[i] = series[i - 1]
    for i in range(n - 1, -1, -1):
        if np.isnan(series[i]) and i < n - 1:
            series[i] = series[i + 1]

    return full_dates, series


def stl_anomalies(dates: list[date], values: list[float],
                   period: int = ANOMALY_PERIOD, z_thresh: float = ANOMALY_Z_THRESHOLD) -> dict:
    """Flags anomalies on the STL *residual* component via a robust
    (median/MAD) z-score - never on raw values, so every Monday doesn't read
    as an anomaly just because Mondays are structurally different."""
    full_dates, series = _densify(dates, values)
    if len(series) < period * 2 or np.all(series == series[0]):
        return {"anomalies": [], "insufficient_data": True}

    stl = STL(series, period=period, robust=True).fit()
    resid = stl.resid
    median = np.median(resid)
    # Plain std, not MAD: confirmed empirically against synthetic data with a
    # known noise scale that STL's robust=True iterative reweighting leaves
    # residuals with a sharp central peak + heavier tail, which makes
    # MAD/IQR-based "robust" scale estimators systematically UNDERESTIMATE
    # the true noise scale (by ~35-45% in testing) and over-flag. Plain std
    # tracked the true injected noise scale correctly across sample sizes.
    sigma = np.std(resid, ddof=1)
    if sigma == 0:
        return {"anomalies": [], "insufficient_data": False}
    z = (resid - median) / sigma

    anomalies = [
        {"date": d.isoformat(), "value": round(float(v), 2), "z": round(float(zz), 2)}
        for d, v, zz in zip(full_dates, series, z) if abs(zz) > z_thresh
    ]
    return {"anomalies": anomalies, "insufficient_data": False}


def rest_recommendation_gate(daily_rows: list[dict], baseline_resting_hr: Optional[float]) -> dict:
    """Hard-flags "recommend rest" independent of the general anomaly
    threshold: resting HR >=5bpm above the 30-day baseline AND HRV status
    below normal AND a falling sleep score, sustained across the 2 most
    recent days. `daily_rows` must be ordered oldest-to-newest and contain
    at least the last 3 days (2 to check + 1 to establish the sleep-score
    trend for the earlier of the 2)."""
    if baseline_resting_hr is None or len(daily_rows) < 3:
        return {"flagged": False, "reason": "insufficient data"}

    last_three = daily_rows[-3:]
    flagged_days = []
    for i in (1, 2):  # the 2 most recent days, each needs a prior day for trend
        today_row, prev_row = last_three[i], last_three[i - 1]
        resting_hr = today_row.get("resting_hr")
        hrv_status = today_row.get("hrv_status")
        sleep_score = today_row.get("sleep_score")
        prev_sleep_score = prev_row.get("sleep_score")

        if resting_hr is None or sleep_score is None or prev_sleep_score is None:
            continue

        hr_high = (resting_hr - baseline_resting_hr) >= 5
        hrv_low = hrv_status in ("LOW", "UNBALANCED")
        sleep_falling = sleep_score < prev_sleep_score
        flagged_days.append(hr_high and hrv_low and sleep_falling)

    flagged = len(flagged_days) == 2 and all(flagged_days)
    return {
        "flagged": flagged,
        "reason": "resting HR elevated + HRV low + sleep falling, 2 consecutive days" if flagged else None,
    }


# ---- Pre-registered lagged hypotheses --------------------------------------

def _partial_out_weekday(dates: list[date], values: np.ndarray) -> np.ndarray:
    """Regress out day-of-week (6 dummy columns + intercept) via OLS, return
    residuals - so a lagged relationship isn't just two series that both
    happen to have weekly structure."""
    n = len(values)
    weekday = np.array([d.weekday() for d in dates])
    X = np.ones((n, 7))
    for i in range(1, 7):
        X[:, i] = (weekday == i).astype(float)
    coeffs, *_ = np.linalg.lstsq(X, values, rcond=None)
    fitted = X @ coeffs
    return values - fitted


def _block_length(residuals: np.ndarray, max_lag: int = 20) -> int:
    """First lag where the autocorrelation function drops below 0.2 -
    the moving-block bootstrap's block length, so resampling respects
    however much day-to-day dependence the series actually has."""
    if len(residuals) < 8:
        return 1
    acf_vals = acf(residuals, nlags=min(max_lag, len(residuals) // 2 - 1), fft=True)
    for lag in range(1, len(acf_vals)):
        if abs(acf_vals[lag]) < 0.2:
            return max(lag, 1)
    return max(len(acf_vals) - 1, 1)


def _moving_block_bootstrap_corr(x: np.ndarray, y: np.ndarray, block_len: int,
                                  n_resamples: int, rng: np.random.Generator) -> np.ndarray:
    """Resamples (x, y) jointly in blocks (preserving their pairing and local
    dependence structure) to build a bootstrap distribution of the Pearson
    correlation coefficient."""
    n = len(x)
    n_blocks = int(np.ceil(n / block_len))
    starts = np.arange(0, n - block_len + 1) if n > block_len else np.array([0])

    boot_corrs = np.empty(n_resamples)
    for b in range(n_resamples):
        chosen = rng.choice(starts, size=n_blocks, replace=True)
        idx = np.concatenate([np.arange(s, min(s + block_len, n)) for s in chosen])[:n]
        xs, ys = x[idx], y[idx]
        if np.std(xs) == 0 or np.std(ys) == 0:
            boot_corrs[b] = 0.0
        else:
            boot_corrs[b] = np.corrcoef(xs, ys)[0, 1]
    return boot_corrs


def test_lagged_hypothesis(dates: list[date], predictor: np.ndarray, outcome: np.ndarray,
                            rng: Optional[np.random.Generator] = None) -> dict:
    """Runs one pre-registered hypothesis: weekday-partial both series, block-
    bootstrap the correlation between the residuals, and return the effect
    size, CI, an approximate p-value, and n_effective. Does not apply the BH
    correction itself - that's applied once across the family of 5 by the
    caller, since it needs all 5 p-values together."""
    rng = rng or np.random.default_rng(RNG_SEED)
    n = len(predictor)
    if n < MIN_N_EFFECTIVE:
        return {"effect_size": None, "ci_low": None, "ci_high": None,
                "p_value": None, "n_effective": n, "n_raw": n,
                "insufficient_n": True}

    x_resid = _partial_out_weekday(dates, predictor)
    y_resid = _partial_out_weekday(dates, outcome)

    block_len = _block_length(y_resid)
    n_effective = max(int(n / block_len), 1)

    observed_r = float(np.corrcoef(x_resid, y_resid)[0, 1]) if np.std(x_resid) and np.std(y_resid) else 0.0
    boot_corrs = _moving_block_bootstrap_corr(x_resid, y_resid, block_len, BOOTSTRAP_RESAMPLES, rng)

    ci_low, ci_high = np.percentile(boot_corrs, [100 * CI_ALPHA / 2, 100 * (1 - CI_ALPHA / 2)])
    # Two-sided percentile-bootstrap test of r=0: under the null the
    # bootstrap distribution splits ~50/50 around zero.
    p_value = 2 * min(np.mean(boot_corrs >= 0), np.mean(boot_corrs <= 0))
    p_value = min(p_value, 1.0)

    return {
        "effect_size": round(observed_r, 4),
        "ci_low": round(float(ci_low), 4),
        "ci_high": round(float(ci_high), 4),
        "p_value": round(float(p_value), 4),
        "n_effective": n_effective,
        "n_raw": n,
        "block_len": block_len,
        "insufficient_n": False,
    }


def run_lagged_hypotheses(series_by_hypothesis: dict) -> list[dict]:
    """Runs all 5 pre-registered hypotheses and applies Benjamini-Hochberg
    across the family. `series_by_hypothesis` maps hypothesis id -> (dates,
    predictor_array, outcome_array) already aligned by date + lag."""
    results = []
    for spec in HYPOTHESES:
        hid = spec["id"]
        if hid not in series_by_hypothesis:
            results.append({**spec, "effect_size": None, "ci_low": None, "ci_high": None,
                            "q_value": None, "n_effective": 0, "status": "suppressed",
                            "detail": {"reason": "no aligned data available"}})
            continue
        dates, predictor, outcome = series_by_hypothesis[hid]
        test = test_lagged_hypothesis(dates, predictor, outcome)
        results.append({**spec, **test})

    # BH correction across exactly the 5 pre-registered tests - never a
    # larger or smaller family, which would change what "q<0.10" means.
    testable = [r for r in results if r.get("p_value") is not None]
    if testable:
        pvals = [r["p_value"] for r in testable]
        _, qvals, _, _ = multipletests(pvals, alpha=Q_VALUE_THRESHOLD, method="fdr_bh")
        for r, q in zip(testable, qvals):
            r["q_value"] = round(float(q), 4)

    for r in results:
        if r.get("q_value") is None:
            r["status"] = r.get("status", "suppressed")
            continue
        ci_excludes_zero = not (r["ci_low"] <= 0 <= r["ci_high"])
        surfaced = (
            r["q_value"] < Q_VALUE_THRESHOLD
            and r["n_effective"] >= MIN_N_EFFECTIVE
            and ci_excludes_zero
        )
        r["status"] = "surfaced" if surfaced else "suppressed"

    return results


# ---- Training-day vs. rest-day comparison (Hedges' g) ----------------------

MIN_GROUP_N = 10


def hedges_g(training: np.ndarray, rest: np.ndarray) -> float:
    """Bias-corrected Cohen's d (Hedges' g) - training vs. rest-day values."""
    nx, ny = len(training), len(rest)
    dof = nx + ny - 2
    pooled_sd = np.sqrt(((nx - 1) * np.var(training, ddof=1) + (ny - 1) * np.var(rest, ddof=1)) / dof)
    if pooled_sd == 0:
        return 0.0
    d = (np.mean(training) - np.mean(rest)) / pooled_sd
    correction = 1 - (3 / (4 * dof - 1))
    return d * correction


def compare_training_vs_rest(training: np.ndarray, rest: np.ndarray,
                              rng: Optional[np.random.Generator] = None,
                              n_resamples: int = BOOTSTRAP_RESAMPLES) -> dict:
    if len(training) < MIN_GROUP_N or len(rest) < MIN_GROUP_N:
        return {"effect_size": None, "ci_low": None, "ci_high": None,
                "n_effective": min(len(training), len(rest)), "status": "suppressed",
                "detail": {"reason": f"needs n>={MIN_GROUP_N} per group"}}

    rng = rng or np.random.default_rng(RNG_SEED)
    observed_g = hedges_g(training, rest)

    boot_g = np.empty(n_resamples)
    for b in range(n_resamples):
        t_sample = rng.choice(training, size=len(training), replace=True)
        r_sample = rng.choice(rest, size=len(rest), replace=True)
        boot_g[b] = hedges_g(t_sample, r_sample)

    ci_low, ci_high = np.percentile(boot_g, [100 * CI_ALPHA / 2, 100 * (1 - CI_ALPHA / 2)])
    ci_excludes_zero = not (ci_low <= 0 <= ci_high)

    return {
        "effect_size": round(float(observed_g), 4),
        "ci_low": round(float(ci_low), 4),
        "ci_high": round(float(ci_high), 4),
        "n_effective": min(len(training), len(rest)),
        "status": "surfaced" if ci_excludes_zero else "suppressed",
    }


# ---- DB orchestration -------------------------------------------------------

def _daily_series(conn, start: date, end: date) -> dict:
    """One dict per calendar day in [start, end] with daily_metrics columns
    plus a same-day training_load sum and training_day flag from activities."""
    metrics_rows = db.fetch_all_dicts(
        conn,
        "SELECT date, steps, resting_hr, hrv_status, body_battery_wake, sleep_score, stress_avg "
        "FROM daily_metrics WHERE date >= ? AND date <= ?",
        (start.isoformat(), end.isoformat()),
    )
    by_date = {r["date"]: dict(r) for r in metrics_rows}

    activity_rows = db.fetch_all_dicts(
        conn,
        "SELECT date, bucket, training_load FROM activities WHERE date >= ? AND date <= ?",
        (start.isoformat(), end.isoformat()),
    )
    for r in activity_rows:
        d = r["date"]
        row = by_date.setdefault(d, {"date": d})
        row["training_load"] = (row.get("training_load") or 0) + (r["training_load"] or 0)
        if r["bucket"] in ("strength", "racquet"):
            row["training_day"] = True

    for row in by_date.values():
        row.setdefault("training_load", None)
        row.setdefault("training_day", False)
        row["hrv_status_numeric"] = HRV_STATUS_NUMERIC.get(row.get("hrv_status"))

    return by_date


def _aligned_pairs(by_date: dict, predictor_key: str, outcome_key: str, lag_days: int):
    """Returns (dates, predictor_array, outcome_array) for every day D where
    both predictor[D] and outcome[D+lag_days] are non-null - the alignment
    step for one lagged hypothesis."""
    dates_sorted = sorted(_parse_date(d) for d in by_date)
    out_dates, xs, ys = [], [], []
    for d in dates_sorted:
        outcome_date = d + timedelta(days=lag_days)
        pred_row = by_date.get(d.isoformat())
        outcome_row = by_date.get(outcome_date.isoformat())
        if not pred_row or not outcome_row:
            continue
        x = pred_row.get(predictor_key)
        y = outcome_row.get(outcome_key)
        if predictor_key == "training_day":
            x = 1.0 if x else 0.0
        if x is None or y is None:
            continue
        out_dates.append(d)
        xs.append(float(x))
        ys.append(float(y))
    return out_dates, np.array(xs), np.array(ys)


def _store_findings(conn, kind: str, rows: list[dict]) -> None:
    conn.execute("DELETE FROM findings WHERE kind = ?", (kind,))
    now = datetime.now().isoformat()
    for r in rows:
        conn.execute(
            "INSERT INTO findings (computed_at, kind, predictor, outcome, lag_days, effect_size, "
            "ci_low, ci_high, q_value, n_effective, status, detail_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, kind, r.get("predictor"), r.get("outcome"), r.get("lag_days"),
             r.get("effect_size"), r.get("ci_low"), r.get("ci_high"), r.get("q_value"),
             r.get("n_effective"), r.get("status"), json.dumps(r, default=str)),
        )
    conn.commit()


def run_all(conn, today: Optional[date] = None, history_days: int = 365) -> dict:
    """Runs anomaly detection, the 5 lagged hypotheses, and the training-vs-
    rest comparison against real data, and stores every result (surfaced or
    suppressed) in the findings table. Do NOT call this before
    scripts/validate_stats_engine.py has passed on synthetic data."""
    today = today or date.today()
    start = today - timedelta(days=history_days)
    by_date = _daily_series(conn, start, today)

    # Anomaly detection - trailing window only.
    anomaly_start = today - timedelta(days=ANOMALY_WINDOW_DAYS)
    anomaly_rows = []
    for metric in ANOMALY_METRICS:
        dates_vals = sorted(
            (d, by_date[d][metric]) for d in by_date
            if _parse_date(d) >= anomaly_start and by_date[d].get(metric) is not None
        )
        if not dates_vals:
            continue
        dates = [_parse_date(d) for d, _ in dates_vals]
        values = [v for _, v in dates_vals]
        result = stl_anomalies(dates, values)
        for a in result["anomalies"]:
            anomaly_rows.append({
                "predictor": metric, "outcome": metric, "lag_days": 0,
                "effect_size": a["z"], "ci_low": None, "ci_high": None, "q_value": None,
                "n_effective": len(values), "status": "surfaced",
                "date": a["date"], "value": a["value"],
            })
    _store_findings(conn, "anomaly", anomaly_rows)

    # Hard rest-recommendation gate.
    daily_rows_sorted = [by_date[d] for d in sorted(by_date)]
    baseline_rows = [
        r["resting_hr"] for d, r in by_date.items()
        if _parse_date(d) >= today - timedelta(days=30) and r.get("resting_hr") is not None
    ]
    baseline_resting_hr = sum(baseline_rows) / len(baseline_rows) if baseline_rows else None
    gate = rest_recommendation_gate(daily_rows_sorted, baseline_resting_hr)

    # Five pre-registered lagged hypotheses.
    series_by_hypothesis = {}
    for spec in HYPOTHESES:
        dates, x, y = _aligned_pairs(by_date, spec["predictor"], spec["outcome"], spec["lag_days"])
        if len(dates) >= MIN_N_EFFECTIVE:
            series_by_hypothesis[spec["id"]] = (dates, x, y)
    hypothesis_results = run_lagged_hypotheses(series_by_hypothesis)
    _store_findings(conn, "lagged_hypothesis", hypothesis_results)

    # Training-day vs. rest-day comparison.
    comparison_rows = []
    for metric in ("sleep_score", "resting_hr", "hrv_status_numeric"):
        training_vals = np.array([
            r[metric] for r in by_date.values() if r.get("training_day") and r.get(metric) is not None
        ], dtype=float)
        rest_vals = np.array([
            r[metric] for r in by_date.values() if not r.get("training_day") and r.get(metric) is not None
        ], dtype=float)
        result = compare_training_vs_rest(training_vals, rest_vals)
        comparison_rows.append({"predictor": "training_day", "outcome": metric, "lag_days": 0, **result})
    _store_findings(conn, "training_rest_comparison", comparison_rows)

    return {
        "anomalies": anomaly_rows,
        "rest_recommendation_gate": gate,
        "lagged_hypotheses": hypothesis_results,
        "training_rest_comparison": comparison_rows,
    }


def main():
    with db.connect() as conn:
        result = run_all(conn)
    surfaced_h = sum(1 for r in result["lagged_hypotheses"] if r["status"] == "surfaced")
    surfaced_c = sum(1 for r in result["training_rest_comparison"] if r["status"] == "surfaced")
    print(f"Anomalies flagged: {len(result['anomalies'])}")
    print(f"Rest-recommendation gate: {result['rest_recommendation_gate']}")
    print(f"Lagged hypotheses surfaced: {surfaced_h}/5")
    print(f"Training-vs-rest comparisons surfaced: {surfaced_c}/3")


if __name__ == "__main__":
    main()
