"""Import a manually-exported MyFitnessPal data export (zip) into the DB.

MyFitnessPal has no public API - this is the only way this project gets
nutrition data. Not scheduled, not automated: re-export from MyFitnessPal's
own "Export Data" settings page whenever you want fresher numbers, then
re-run this. Safe to re-run on overlapping exports (every write is an
upsert keyed on date, or date+meal).

Only imports Nutrition-Summary (meal-level macros/micros -> nutrition_daily
+ nutrition_meals) and Measurement-Summary (weight, filled in only where
daily_metrics.weight_lb is still NULL for that date - never overwrites a
real Garmin reading). Exercise-Summary is intentionally skipped: it's
almost entirely a passthrough of the same activities already synced
directly from Garmin, so importing it would add duplicate, lower-quality
rows rather than new signal.

Usage:
    python scripts/import_myfitnesspal.py path/to/export.zip
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from garmin_tracker import db

MEAL_FIELDS = {
    "Calories": "calories",
    "Protein (g)": "protein_g",
    "Carbohydrates (g)": "carbs_g",
    "Fat (g)": "fat_g",
}

DAILY_FIELDS = {
    "Calories": "calories",
    "Protein (g)": "protein_g",
    "Carbohydrates (g)": "carbs_g",
    "Fat (g)": "fat_g",
    "Saturated Fat": "saturated_fat_g",
    "Polyunsaturated Fat": "poly_fat_g",
    "Monounsaturated Fat": "mono_fat_g",
    "Trans Fat": "trans_fat_g",
    "Cholesterol": "cholesterol_mg",
    "Sodium (mg)": "sodium_mg",
    "Sugar": "sugar_g",
    "Fiber": "fiber_g",
    "Potassium": "potassium_mg",
    # MyFitnessPal reports these 4 as %DV per meal, not absolute amounts -
    # summing %DV across meals to get a daily %DV total is valid (same
    # reference denominator throughout), unlike the gram/mg fields above.
    "Vitamin A": "vitamin_a_pct",
    "Vitamin C": "vitamin_c_pct",
    "Calcium": "calcium_pct",
    "Iron": "iron_pct",
}


def _to_float(v: str) -> float:
    v = (v or "").strip()
    return float(v) if v else 0.0


def _find_member(zf: zipfile.ZipFile, prefix: str) -> str | None:
    for name in zf.namelist():
        if name.startswith(prefix) and name.endswith(".csv"):
            return name
    return None


def import_nutrition(zf: zipfile.ZipFile, conn) -> tuple[int, int]:
    member = _find_member(zf, "Nutrition-Summary")
    if not member:
        print("No Nutrition-Summary CSV found in export - skipping nutrition import.")
        return 0, 0

    daily_totals: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    meal_rows = 0

    with zf.open(member) as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            date_str = row["Date"].strip()
            meal = row["Meal"].strip()

            meal_row = {"date": date_str, "meal": meal}
            for csv_col, db_col in MEAL_FIELDS.items():
                meal_row[db_col] = _to_float(row.get(csv_col))
            db.upsert(conn, "nutrition_meals", meal_row)
            meal_rows += 1

            for csv_col, db_col in DAILY_FIELDS.items():
                daily_totals[date_str][db_col] += _to_float(row.get(csv_col))

    for date_str, totals in daily_totals.items():
        day_row = {"date": date_str, **{col: round(v, 1) for col, v in totals.items()}}
        db.upsert(conn, "nutrition_daily", day_row)

    conn.commit()
    return len(daily_totals), meal_rows


def import_weight_fallback(zf: zipfile.ZipFile, conn) -> int:
    member = _find_member(zf, "Measurement-Summary")
    if not member:
        print("No Measurement-Summary CSV found in export - skipping weight fallback.")
        return 0

    filled = 0
    with zf.open(member) as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            if "Weight" not in row or not row["Weight"].strip():
                continue
            date_str = row["Date"].strip()
            weight_lb = _to_float(row["Weight"])

            existing = db.fetch_all_dicts(
                conn, "SELECT weight_lb FROM daily_metrics WHERE date = ?", (date_str,)
            )
            if existing and existing[0]["weight_lb"] is not None:
                continue  # never overwrite a real Garmin reading

            if existing:
                conn.execute(
                    "UPDATE daily_metrics SET weight_lb = ? WHERE date = ?", (weight_lb, date_str)
                )
            else:
                db.upsert(conn, "daily_metrics", {"date": date_str, "weight_lb": weight_lb})
            filled += 1

    conn.commit()
    return filled


def main():
    parser = argparse.ArgumentParser(
        description="Import a MyFitnessPal data export (zip) into the database. "
                     "Re-run this manually whenever you re-export from MyFitnessPal - "
                     "there is no live sync for this, MyFitnessPal has no public API."
    )
    parser.add_argument("zip_path", help="Path to the MyFitnessPal export zip file.")
    args = parser.parse_args()

    with zipfile.ZipFile(args.zip_path) as zf, db.connect() as conn:
        n_days, n_meals = import_nutrition(zf, conn)
        print(f"nutrition: {n_days} day(s), {n_meals} meal row(s)")

        n_weight = import_weight_fallback(zf, conn)
        print(f"weight fallback: filled {n_weight} date(s) with no existing Garmin reading")


if __name__ == "__main__":
    main()
