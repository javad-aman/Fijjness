"""Hand-rolled server-rendered SVG charts - no JS charting library, so the
spec's specific chart rules (direct labels, no legend under 5 series, one
highlighted + grey context series, uncertainty bands, meaningful-only
zero-baselines) are hit exactly rather than fought against a library's
defaults. Palette values are the literal hex codes from static/style.css's
CSS custom properties (SVG can't reference CSS variables across a separate
stylesheet reliably enough to depend on here).
"""
from __future__ import annotations

from datetime import date as _date

GROUND = "#0E1116"
RAISE = "#1C222C"
LINE = "#2E3644"
LINE_SOFT = "#232A35"
TEXT = "#EDEFF2"
MUTED = "#A3AEBC"
DIM = "#7C8794"
AHEAD = "#4FD1A5"
BEHIND = "#FF6B72"
WARN = "#E0A458"
NEUTRAL = "#7C9BFF"
AMBER = WARN

# ahead/behind are reserved exclusively for pace on/off-state (spec §9: "Green
# and red mean on-pace and off-pace, nowhere else") - so categorical charts
# below never use them for a bucket, even though dashboard-prototype-v3.html's
# own mock reuses --ahead green for "racquet" (--b-racquet == --ahead there).
# Racquet gets a distinct teal instead so pace color stays exclusively
# meaningful; everything else matches the v3 palette exactly.
BUCKET_COLORS = {
    "strength": NEUTRAL,
    "racquet": "#2FB8C6",  # teal - deliberately not --ahead, see above
    "cardio": WARN,
    "other": "#8B7BB8",    # a real logged activity (yoga, golf, ...) - must
                           # read differently from an actual rest day below
    "unlogged": "#49566A",
}

FONT = "font-family='IBM Plex Mono, ui-monospace, monospace'"
LABEL_FONT = "font-family='Inter, sans-serif'"


def _esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;")


def _human_date(iso_str: str) -> str:
    d = _date.fromisoformat(iso_str)
    return f"{d.strftime('%b')} {d.day}"


def stacked_bar_svg(weekly: dict, width: int = 520, height: int = 220,
                     total_line: list = None, stretch: bool = False) -> str:
    """Weekly stacked bar by bucket. Direct-labeled (4 buckets, under the
    5-series legend threshold), zero-baseline (calories - zero is
    meaningful here per spec §9). Optional total_line overlays total active
    calories as a thin line, so the gap between logged activity and total
    movement is visible - usually the interesting part.

    `stretch=True` fills a fixed-height container regardless of the
    viewBox's own aspect ratio (needed on the Today dashboard's 260px chart
    frames - width:100%/height:auto locks the aspect ratio, which overflows
    a fixed-height container once the rendered width differs from `width`).
    Defaults to False to keep the Activity page's existing chart-scroll
    (no fixed height) rendering unchanged."""
    labels = weekly["week_labels"]
    buckets = weekly["buckets"]
    data = weekly["data"]
    n = len(labels)
    if n == 0:
        return f"<svg width='{width}' height='{height}'></svg>"

    totals = [sum(data[b][i] for b in buckets) for i in range(n)]
    max_val = max(totals)
    if total_line:
        max_val = max(max_val, max(total_line))
    max_val = max_val or 1

    pad_left, pad_bottom, pad_top = 36, 24, 20
    plot_w = width - pad_left - 10
    plot_h = height - pad_bottom - pad_top
    bar_w = plot_w / n * 0.6
    gap = plot_w / n

    svg_style = "width:100%;height:100%;display:block" if stretch else "width:100%;height:auto;display:block"
    preserve = " preserveAspectRatio='none'" if stretch else ""
    parts = [f"<svg viewBox='0 0 {width} {height}' style='{svg_style}'{preserve}>"]
    parts.append(f"<rect width='{width}' height='{height}' fill='{GROUND}'/>")
    # zero baseline
    y0 = pad_top + plot_h
    parts.append(f"<line x1='{pad_left}' y1='{y0}' x2='{width - 10}' y2='{y0}' stroke='{LINE}' stroke-width='1'/>")

    bar_centers = []
    for i in range(n):
        x = pad_left + i * gap + (gap - bar_w) / 2
        bar_centers.append(x + bar_w / 2)
        y_cursor = y0
        for b in buckets:
            val = data[b][i]
            if val <= 0:
                continue
            seg_h = (val / max_val) * plot_h
            y_cursor -= seg_h
            parts.append(
                f"<rect x='{x:.1f}' y='{y_cursor:.1f}' width='{bar_w:.1f}' height='{seg_h:.1f}' fill='{BUCKET_COLORS.get(b, MUTED)}'/>"
            )
        # week label: per-bar dates on the Activity page, just two endpoint
        # labels ("12 weeks ago" / "this week") on the Today dashboard - the
        # `stretch` flag already distinguishes the two contexts.
        if not stretch and (n <= 8 or i % 2 == 0):
            wk_label = labels[i][5:]  # MM-DD
            parts.append(
                f"<text x='{x + bar_w / 2:.1f}' y='{height - 6}' {LABEL_FONT} font-size='9' fill='{MUTED}' text-anchor='middle'>{_esc(wk_label)}</text>"
            )

    if stretch:
        parts.append(f"<text x='{pad_left}' y='{height - 6}' {LABEL_FONT} font-size='10' fill='{DIM}'>{n} weeks ago</text>")
        parts.append(f"<text x='{width - 10}' y='{height - 6}' {LABEL_FONT} font-size='10' fill='{DIM}' text-anchor='end'>this week</text>")

    if total_line:
        points = [(bar_centers[i], y0 - (total_line[i] / max_val) * plot_h) for i in range(n)]
        path = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in points)
        dash = " stroke-dasharray='4,3'" if stretch else ""
        opacity = "0.55" if stretch else "1"
        parts.append(f"<path d='{path}' fill='none' stroke='{TEXT}' stroke-width='1.5' opacity='{opacity}'{dash}/>")
        if not stretch:
            for x, y in points:
                parts.append(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='2' fill='{TEXT}'/>")
            lx, ly = points[-1]
            parts.append(
                f"<text x='{lx:.1f}' y='{ly - 8:.1f}' {LABEL_FONT} font-size='9' fill='{TEXT}' text-anchor='end'>total active cal</text>"
            )

    # direct labels (legend-free, per spec: no legend under 5 series) on the
    # Activity page; the Today dashboard renders its own external HTML
    # legend below the chart instead (matching dashboard-prototype.html).
    if not stretch:
        lx = pad_left
        for b in buckets:
            parts.append(f"<rect x='{lx}' y='0' width='9' height='9' fill='{BUCKET_COLORS.get(b, MUTED)}'/>")
            parts.append(f"<text x='{lx + 13}' y='8' {LABEL_FONT} font-size='10' fill='{TEXT}'>{_esc(b)}</text>")
            lx += 16 + len(b) * 6 + 14

    parts.append("</svg>")
    return "".join(parts)


def cycle_plot_svg(weekday: str, panel: dict, width: int = 160, height: int = 90) -> str:
    """One weekday panel: raw points (muted, context) + its own Theil-Sen
    trend line (neutral - "trend lines, projections" per the palette's
    defined semantic for that color)."""
    points = panel["points"]
    parts = [f"<svg viewBox='0 0 {width} {height}' style='width:100%;height:auto;display:block'>"]
    parts.append(f"<rect width='{width}' height='{height}' fill='{GROUND}' rx='6'/>")
    parts.append(f"<text x='8' y='14' {LABEL_FONT} font-size='10' fill='{MUTED}' text-transform='uppercase'>{_esc(weekday[:3])}</text>")

    if len(points) < 2:
        parts.append(f"<text x='8' y='{height/2}' {LABEL_FONT} font-size='9' fill='{MUTED}'>not enough data</text>")
        parts.append("</svg>")
        return "".join(parts)

    steps = [p["steps"] for p in points]
    lo, hi = min(steps), max(steps)
    rng = (hi - lo) or 1
    pad = 18
    plot_w, plot_h = width - 16, height - pad - 8

    def xy(i, v):
        x = 8 + (i / max(len(points) - 1, 1)) * plot_w
        y = pad + plot_h - ((v - lo) / rng) * plot_h
        return x, y

    for i, p in enumerate(points):
        x, y = xy(i, p["steps"])
        parts.append(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='2' fill='{MUTED}'/>")

    trend = panel.get("trend_per_week")
    if trend is not None:
        # simple visual trend: straight line from first to last point's
        # y-position implied by the slope, not a full regression redraw
        x0, y0 = xy(0, steps[0])
        x1, y1 = xy(len(points) - 1, steps[-1])
        parts.append(f"<line x1='{x0:.1f}' y1='{y0:.1f}' x2='{x1:.1f}' y2='{y1:.1f}' stroke='{NEUTRAL}' stroke-width='2'/>")

    parts.append("</svg>")
    return "".join(parts)


def effect_ci_svg(effect_size: float, ci_low: float, ci_high: float,
                   width: int = 280, height: int = 48) -> str:
    """A zero-centered effect-size + CI band - the uncertainty visual for
    /insights findings, so a point estimate is never shown without its
    interval (per spec: no false-precision single numbers)."""
    pad = 14
    plot_w = width - 2 * pad
    max_abs = max(abs(ci_low), abs(ci_high), abs(effect_size), 0.05) * 1.25

    def x_of(v: float) -> float:
        return pad + plot_w / 2 + (v / max_abs) * (plot_w / 2)

    zero_x = x_of(0)
    mid_y = height / 2 + 4
    lo_x, hi_x = x_of(ci_low), x_of(ci_high)
    point_x = x_of(effect_size)

    parts = [f"<svg viewBox='0 0 {width} {height}' style='width:100%;height:auto;display:block'>"]
    parts.append(f"<rect width='{width}' height='{height}' fill='{GROUND}'/>")
    parts.append(f"<line x1='{zero_x:.1f}' y1='8' x2='{zero_x:.1f}' y2='{height - 8}' stroke='{LINE}' stroke-width='1'/>")
    parts.append(f"<line x1='{lo_x:.1f}' y1='{mid_y}' x2='{hi_x:.1f}' y2='{mid_y}' stroke='{NEUTRAL}' stroke-width='3' stroke-linecap='round'/>")
    parts.append(f"<circle cx='{point_x:.1f}' cy='{mid_y}' r='4' fill='{TEXT}'/>")
    parts.append(
        f"<text x='{point_x:.1f}' y='{mid_y - 10}' {FONT} font-size='11' fill='{TEXT}' text-anchor='middle'>{effect_size:+.2f}</text>"
    )
    parts.append("</svg>")
    return "".join(parts)


def heatmap_svg(cal: dict, width: int = 560) -> str:
    """Month calendar heatmap colored by dominant activity bucket. A compact
    caption legend is used here (not per-cell direct labels) since day cells
    are too small to label individually - the one deliberate exception to
    the direct-label rule, for a spatial grid layout."""
    days = cal["days"]
    if not days:
        return f"<svg width='{width}' height='40'></svg>"

    first_date_str = days[0]["date"]
    from datetime import date as _date
    first = _date.fromisoformat(first_date_str)
    lead_blank = first.weekday()  # Monday=0

    cell = 28
    gap = 4
    cols = 7
    rows = (lead_blank + len(days) + cols - 1) // cols
    height = rows * (cell + gap) + 30

    parts = [f"<svg viewBox='0 0 {width} {height}' style='width:100%;height:auto;display:block'>"]
    parts.append(f"<rect width='{width}' height='{height}' fill='{GROUND}'/>")

    for i, day in enumerate(days):
        idx = lead_blank + i
        col = idx % cols
        row = idx // cols
        x = col * (cell + gap)
        y = row * (cell + gap)
        color = BUCKET_COLORS.get(day["bucket"], LINE) if day["bucket"] else LINE
        parts.append(f"<rect x='{x}' y='{y}' width='{cell}' height='{cell}' rx='4' fill='{color}'/>")
        day_num = int(day["date"][-2:])
        parts.append(
            f"<text x='{x + cell/2:.1f}' y='{y + cell/2 + 3:.1f}' {FONT} font-size='9' fill='{TEXT}' text-anchor='middle'>{day_num}</text>"
        )

    ly = rows * (cell + gap) + 12
    lx = 0
    for b, color in BUCKET_COLORS.items():
        parts.append(f"<rect x='{lx}' y='{ly - 9}' width='9' height='9' fill='{color}'/>")
        parts.append(f"<text x='{lx + 13}' y='{ly}' {LABEL_FONT} font-size='10' fill='{MUTED}'>{_esc(b)}</text>")
        lx += 16 + len(b) * 6 + 14

    parts.append("</svg>")
    return "".join(parts)


def weight_trend_svg(chart_series: list, checkpoint: dict, projection: list = None,
                      width: int = 900, height: int = 240) -> str:
    """Raw readings as faint dots, EWMA as "Actual (logged)", and - when a
    checkpoint and a real Theil-Sen rate exist - the projected path plus its
    90% CI band as "Expected path" / "Best / worst case" (see
    analytics.weight_projection_path: same rate already computed for the
    single-arrival-date estimate, just drawn as a full straight-line path
    instead of solved for one date). Y-axis never starts at zero - weight
    per spec §3. Never draws a curve or shape the underlying rate doesn't
    actually support."""
    if len(chart_series) < 2:
        return f"<svg width='{width}' height='{height}'></svg>"

    dates = [row["date"] for row in chart_series]
    raw = [row["weight_lb"] for row in chart_series]
    ewma = [row["ewma"] for row in chart_series]

    values = list(raw) + list(ewma)
    if checkpoint:
        values.append(checkpoint["target"])
    if projection:
        for p in projection:
            values.extend([p["expected"], p["best"], p["worst"]])
    lo, hi = min(values), max(values)
    pad_val = (hi - lo) * 0.1 or 1
    lo, hi = lo - pad_val, hi + pad_val

    legend_h = 22
    pad_x, pad_top, pad_bottom = 8, 16 + legend_h, 22
    plot_w = width - 2 * pad_x
    plot_h = height - pad_top - pad_bottom

    start_d = _date.fromisoformat(dates[0])
    end_d = _date.fromisoformat(projection[-1]["date"]) if projection else _date.fromisoformat(dates[-1])
    total_days = max((end_d - start_d).days, 1)

    def x_of(date_str: str) -> float:
        d = _date.fromisoformat(date_str)
        return pad_x + ((d - start_d).days / total_days) * plot_w

    def y_of(v: float) -> float:
        return pad_top + plot_h - ((v - lo) / (hi - lo)) * plot_h

    parts = [f"<svg viewBox='0 0 {width} {height}' preserveAspectRatio='none' style='width:100%;height:100%;display:block'>"]
    parts.append(f"<rect width='{width}' height='{height}' fill='{GROUND}'/>")

    grid_step = max(round((hi - lo) / 4), 1)
    g = (int(lo / grid_step) + 1) * grid_step
    while g < hi:
        gy = y_of(g)
        parts.append(f"<line x1='{pad_x}' y1='{gy:.1f}' x2='{pad_x + plot_w:.1f}' y2='{gy:.1f}' stroke='{LINE_SOFT}' stroke-width='1'/>")
        parts.append(f"<text x='{pad_x + plot_w + 4:.1f}' y='{gy + 3:.1f}' {FONT} font-size='9.5' fill='{DIM}'>{g}</text>")
        g += grid_step

    # ---- legend ----
    legend_items = [("Actual (logged)", NEUTRAL, False)]
    if projection:
        legend_items.append(("Expected path", WARN, False))
        legend_items.append(("Best / worst case", MUTED, True))
    lx = pad_x
    for label, color, dashed in legend_items:
        parts.append(f"<line x1='{lx}' y1='10' x2='{lx + 16}' y2='10' stroke='{color}' stroke-width='2'" + (" stroke-dasharray='3,2'" if dashed else "") + "/>")
        parts.append(f"<text x='{lx + 21}' y='13' {LABEL_FONT} font-size='10' fill='{MUTED}'>{_esc(label)}</text>")
        lx += 21 + len(label) * 6 + 18

    for row in chart_series:
        x, y = x_of(row["date"]), y_of(row["weight_lb"])
        tip = _tip([_human_date(row["date"]), f"{row['weight_lb']} lb logged"])
        parts.append(f"<circle data-tip='{tip}' cx='{x:.1f}' cy='{y:.1f}' r='2.5' fill='{MUTED}' opacity='0.6'/>")

    ewma_points = [(x_of(row["date"]), y_of(row["ewma"])) for row in chart_series]
    path = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in ewma_points)
    parts.append(f"<path d='{path}' fill='none' stroke='{NEUTRAL}' stroke-width='2'/>")

    if projection:
        for key, color, dashed in (("worst", MUTED, True), ("best", MUTED, True), ("expected", WARN, False)):
            pts = [(x_of(p["date"]), y_of(p[key])) for p in projection]
            path = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in pts)
            dash = " stroke-dasharray='4,3'" if dashed else ""
            parts.append(f"<path d='{path}' fill='none' stroke='{color}' stroke-width='1.5' opacity='0.85'{dash}/>")
            for p in projection:
                x, y = x_of(p["date"]), y_of(p[key])
                tip = _tip([_human_date(p["date"]), f"{key}: {p[key]} lb"])
                parts.append(f"<circle data-tip='{tip}' cx='{x:.1f}' cy='{y:.1f}' r='2' fill='{color}' opacity='0'/>")
        last = projection[-1]
        last_x, last_y, last_expected = x_of(last["date"]), y_of(last["expected"]), last["expected"]
        parts.append(f"<text x='{last_x:.1f}' y='{last_y - 8:.1f}' {FONT} font-size='11' fill='{WARN}' text-anchor='end'>{last_expected}</text>")

    if checkpoint:
        target_x = x_of(checkpoint["date"]) if projection else pad_x + plot_w
        target_y = y_of(checkpoint["target"])
        parts.append(f"<circle cx='{target_x:.1f}' cy='{target_y:.1f}' r='3' fill='none' stroke='{TEXT}' stroke-width='1.5'/>")
        parts.append(f"<text x='{target_x:.1f}' y='{target_y - 8:.1f}' {LABEL_FONT} font-size='9' fill='{MUTED}' text-anchor='end'>target {checkpoint['target']} · {_esc(_human_date(checkpoint['date']))}</text>")

    last_actual = ewma_points[-1]
    parts.append(f"<text x='{last_actual[0]:.1f}' y='{last_actual[1] - 10:.1f}' {FONT} font-size='12' fill='{TEXT}' text-anchor='end'>{ewma[-1]:.1f}</text>")

    parts.append(f"<text x='{pad_x}' y='{height - 6}' {LABEL_FONT} font-size='9' fill='{MUTED}'>{_esc(_human_date(dates[0]))}</text>")
    end_label_date = projection[-1]["date"] if projection else dates[-1]
    parts.append(f"<text x='{pad_x + plot_w:.1f}' y='{height - 6}' {LABEL_FONT} font-size='9' fill='{MUTED}' text-anchor='end'>{_esc(_human_date(end_label_date))}</text>")

    parts.append("</svg>")
    return "".join(parts)


# ---- v3 dashboard charts -------------------------------------------------
# Every bar/point below carries a data-tip attribute; a single delegated
# listener (see base.html) reads it on hover and positions the shared #tip
# element - no per-chart JS, no charting library.

def _tip(lines: list) -> str:
    return _esc("\n".join(str(l) for l in lines))


def steps_month_bars_svg(data: dict, width: int = 900, height: int = 250) -> str:
    """Daily steps for the current month - no moving average (v3 removes
    it). Ahead-of-goal bars in `ahead`, behind in `warn` (never muted grey -
    a day under goal is a real signal, not a footnote). Goal and month-
    average both drawn as labeled reference lines, direct on the chart."""
    days = data["days"]
    goal = data["goal"]
    avg = data["daily_avg"]
    n = len(days)
    if n == 0:
        return f"<svg width='{width}' height='{height}'></svg>"

    values = [d["steps"] for d in days if d["steps"] is not None]
    max_val = max(values + [goal]) * 1.08 if values else goal * 1.08

    pad_left, pad_right, pad_bottom, pad_top = 44, 84, 26, 14
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_bottom - pad_top
    gap = plot_w / n
    bar_w = gap * 0.6
    y0 = pad_top + plot_h

    def y_of(v: float) -> float:
        return pad_top + plot_h - (v / max_val) * plot_h

    parts = [f"<svg viewBox='0 0 {width} {height}' preserveAspectRatio='none' style='width:100%;height:100%;display:block'>"]
    parts.append(f"<rect width='{width}' height='{height}' fill='{GROUND}'/>")

    grid_step = max(round(max_val / 4 / 1000) * 1000, 1000)
    g = grid_step
    while g < max_val:
        gy = y_of(g)
        parts.append(f"<line x1='{pad_left}' y1='{gy:.1f}' x2='{width - pad_right}' y2='{gy:.1f}' stroke='{LINE_SOFT}' stroke-width='1'/>")
        parts.append(f"<text x='{pad_left - 8}' y='{gy + 4:.1f}' {FONT} font-size='10.5' fill='{DIM}' text-anchor='end'>{g/1000:.1f}k</text>")
        g += grid_step

    for i, day in enumerate(days):
        v = day["steps"]
        if v is None:
            continue
        x = pad_left + i * gap + (gap - bar_w) / 2
        bar_h = max(1.0, (v / max_val) * plot_h)
        color = AHEAD if v >= goal else WARN
        d = _human_date(day["date"])
        tip = _tip([d, f"{v:,} steps", f"{'+' if v >= goal else ''}{v - goal:,} vs goal"])
        parts.append(
            f"<rect data-tip='{tip}' x='{x:.1f}' y='{y0 - bar_h:.1f}' width='{bar_w:.1f}' height='{bar_h:.1f}' "
            f"rx='2' fill='{color}' opacity='0.9'/>"
        )

    goal_y = y_of(goal)
    parts.append(
        f"<line x1='{pad_left}' y1='{goal_y:.1f}' x2='{width - pad_right + 8}' y2='{goal_y:.1f}' "
        f"stroke='{TEXT}' stroke-width='1' stroke-dasharray='3,3' opacity='0.6'/>"
    )
    parts.append(f"<text x='{width - pad_right + 12}' y='{goal_y + 4:.1f}' {FONT} font-size='11' fill='{MUTED}'>goal {goal:,}</text>")

    avg_y = y_of(avg)
    parts.append(f"<line x1='{pad_left}' y1='{avg_y:.1f}' x2='{width - pad_right + 8}' y2='{avg_y:.1f}' stroke='{NEUTRAL}' stroke-width='1.5'/>")
    parts.append(f"<text x='{width - pad_right + 12}' y='{avg_y + 4:.1f}' {FONT} font-size='11' fill='{NEUTRAL}'>your avg {avg:,}</text>")

    mid_i = n // 2
    for i, anchor in ((0, "start"), (mid_i, "middle"), (n - 1, "end")):
        x = pad_left + i * gap + bar_w / 2
        parts.append(f"<text x='{x:.1f}' y='{height - 8}' {LABEL_FONT} font-size='10' fill='{DIM}' text-anchor='{anchor}'>{_esc(_human_date(days[i]['date']))}</text>")

    parts.append("</svg>")
    return "".join(parts)


def daily_calories_month_svg(data: dict, width: int = 900, height: int = 220) -> str:
    """Daily active calories for the current month, each bar colored by that
    day's dominant session bucket (or the rest/unlogged color) - "calories
    follow your sessions, not your steps."""
    days = data["days"]
    avg = data["daily_avg"]
    n = len(days)
    if n == 0:
        return f"<svg width='{width}' height='{height}'></svg>"

    values = [d["active_calories"] for d in days]
    max_val = max(values) * 1.1 or 1  # real days with active_calories==0 (not just missing) are possible early in a sync

    pad_left, pad_right, pad_bottom, pad_top = 44, 12, 26, 14
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_bottom - pad_top
    gap = plot_w / n
    bar_w = gap * 0.6
    y0 = pad_top + plot_h

    def y_of(v: float) -> float:
        return pad_top + plot_h - (v / max_val) * plot_h

    parts = [f"<svg viewBox='0 0 {width} {height}' preserveAspectRatio='none' style='width:100%;height:100%;display:block'>"]
    parts.append(f"<rect width='{width}' height='{height}' fill='{GROUND}'/>")

    grid_step = max(round(max_val / 4 / 100) * 100, 100)
    g = grid_step
    while g < max_val:
        gy = y_of(g)
        parts.append(f"<line x1='{pad_left}' y1='{gy:.1f}' x2='{width - pad_right}' y2='{gy:.1f}' stroke='{LINE_SOFT}' stroke-width='1'/>")
        parts.append(f"<text x='{pad_left - 8}' y='{gy + 4:.1f}' {FONT} font-size='10.5' fill='{DIM}' text-anchor='end'>{g:,.0f}</text>")
        g += grid_step

    bucket_names = {"strength": "Strength", "racquet": "Racquet", "cardio": "Cardio", "other": "Other", None: "Rest day"}
    for i, day in enumerate(days):
        v = day["active_calories"]
        x = pad_left + i * gap + (gap - bar_w) / 2
        bar_h = max(1.0, (v / max_val) * plot_h)
        color = BUCKET_COLORS.get(day["bucket"], BUCKET_COLORS["unlogged"]) if day["bucket"] else BUCKET_COLORS["unlogged"]
        d = _human_date(day["date"])
        tip = _tip([d, f"{v:,} active cal", bucket_names.get(day["bucket"], "Rest day")])
        parts.append(
            f"<rect data-tip='{tip}' x='{x:.1f}' y='{y0 - bar_h:.1f}' width='{bar_w:.1f}' height='{bar_h:.1f}' "
            f"rx='2' fill='{color}' opacity='0.9'/>"
        )

    avg_y = y_of(avg)
    parts.append(f"<line x1='{pad_left}' y1='{avg_y:.1f}' x2='{width - pad_right}' y2='{avg_y:.1f}' stroke='{NEUTRAL}' stroke-width='1.5' stroke-dasharray='4,3'/>")
    parts.append(f"<text x='{pad_left + 4}' y='{avg_y - 6:.1f}' {FONT} font-size='11' fill='{NEUTRAL}'>avg {avg:,}</text>")

    for i, anchor in ((0, "start"), (n - 1, "end")):
        x = pad_left + i * gap + bar_w / 2
        parts.append(f"<text x='{x:.1f}' y='{height - 8}' {LABEL_FONT} font-size='10' fill='{DIM}' text-anchor='{anchor}'>{_esc(_human_date(days[i]['date']))}</text>")

    parts.append("</svg>")
    return "".join(parts)


def monthly_steps_bars_svg(rows: list, width: int = 520, height: int = 240) -> str:
    """Total steps by month, trailing 6 months - value label above each bar,
    each bar's own target drawn as a short tick (targets can differ month to
    month per goals.yaml, so one flat line across all six would misstate
    the months that don't share July's target)."""
    n = len(rows)
    if n == 0:
        return f"<svg width='{width}' height='{height}'></svg>"

    max_val = max(max(r["steps"] for r in rows), max(r["target"] for r in rows)) * 1.15
    pad_left, pad_right, pad_bottom, pad_top = 48, 12, 28, 26
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_bottom - pad_top
    gap = plot_w / n
    bar_w = gap * 0.62

    def y_of(v: float) -> float:
        return pad_top + plot_h - (v / max_val) * plot_h

    parts = [f"<svg viewBox='0 0 {width} {height}' preserveAspectRatio='none' style='width:100%;height:100%;display:block'>"]
    parts.append(f"<rect width='{width}' height='{height}' fill='{GROUND}'/>")

    grid_step = max(round(max_val / 3 / 50000) * 50000, 50000)
    g = grid_step
    while g < max_val:
        gy = y_of(g)
        parts.append(f"<line x1='{pad_left}' y1='{gy:.1f}' x2='{width - pad_right}' y2='{gy:.1f}' stroke='{LINE_SOFT}' stroke-width='1'/>")
        parts.append(f"<text x='{pad_left - 8}' y='{gy + 4:.1f}' {FONT} font-size='10.5' fill='{DIM}' text-anchor='end'>{g/1000:.0f}k</text>")
        g += grid_step

    for i, r in enumerate(rows):
        x = pad_left + i * gap + (gap - bar_w) / 2
        bar_h = max(1.0, (r["steps"] / max_val) * plot_h)
        y = pad_top + plot_h - bar_h
        color = AHEAD if r["cleared"] else WARN
        over = r["steps"] - r["target"]
        tip = _tip([r["month_label"], f"{r['steps']:,} steps", f"target {r['target']:,}", f"{'+' if over >= 0 else ''}{over:,} vs target"])
        parts.append(f"<rect data-tip='{tip}' x='{x:.1f}' y='{y:.1f}' width='{bar_w:.1f}' height='{bar_h:.1f}' rx='2' fill='{color}' opacity='0.9'/>")
        parts.append(f"<text x='{x + bar_w/2:.1f}' y='{y - 8:.1f}' {FONT} font-size='11.5' font-weight='600' fill='{TEXT}' text-anchor='middle'>{round(r['steps']/1000)}k</text>")
        target_y = y_of(r["target"])
        parts.append(f"<line x1='{x:.1f}' y1='{target_y:.1f}' x2='{x + bar_w:.1f}' y2='{target_y:.1f}' stroke='{TEXT}' stroke-width='1' stroke-dasharray='2,2' opacity='0.6'/>")
        parts.append(f"<text x='{x + bar_w/2:.1f}' y='{height - 10}' {LABEL_FONT} font-size='11' fill='{MUTED}' text-anchor='middle'>{_esc(r['month_label'])}</text>")

    parts.append("</svg>")
    return "".join(parts)


def sessions_month_stacked_svg(rows: list, width: int = 520, height: int = 240) -> str:
    """Sessions by month, stacked by bucket - count printed inside each
    segment tall enough to hold it, total printed above the stack."""
    n = len(rows)
    if n == 0:
        return f"<svg width='{width}' height='{height}'></svg>"

    max_val = max(r["strength"] + r["racquet"] + r["cardio"] + r["other"] for r in rows) * 1.15 or 1
    pad_left, pad_right, pad_bottom, pad_top = 32, 12, 28, 26
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_bottom - pad_top
    gap = plot_w / n
    bar_w = gap * 0.62

    def y_of(v: float) -> float:
        return pad_top + plot_h - (v / max_val) * plot_h

    parts = [f"<svg viewBox='0 0 {width} {height}' preserveAspectRatio='none' style='width:100%;height:100%;display:block'>"]
    parts.append(f"<rect width='{width}' height='{height}' fill='{GROUND}'/>")

    grid_step = max(round(max_val / 3 / 5) * 5, 5)
    g = grid_step
    while g < max_val:
        gy = y_of(g)
        parts.append(f"<line x1='{pad_left}' y1='{gy:.1f}' x2='{width - pad_right}' y2='{gy:.1f}' stroke='{LINE_SOFT}' stroke-width='1'/>")
        parts.append(f"<text x='{pad_left - 8}' y='{gy + 4:.1f}' {FONT} font-size='10.5' fill='{DIM}' text-anchor='end'>{g:.0f}</text>")
        g += grid_step

    segments = [("strength", "Strength"), ("racquet", "Racquet"), ("cardio", "Cardio"), ("other", "Other")]
    for i, r in enumerate(rows):
        x = pad_left + i * gap + (gap - bar_w) / 2
        acc = 0
        total = r["strength"] + r["racquet"] + r["cardio"] + r["other"]
        for key, name in segments:
            v = r[key]
            if not v:
                continue
            y_top = y_of(acc + v)
            y_bot = y_of(acc)
            seg_h = y_bot - y_top
            tip = _tip([f"{r['month_label']} · {name}", f"{v} sessions"])
            parts.append(f"<rect data-tip='{tip}' x='{x:.1f}' y='{y_top:.1f}' width='{bar_w:.1f}' height='{seg_h:.1f}' fill='{BUCKET_COLORS[key]}' opacity='0.9'/>")
            if seg_h > 15:
                parts.append(f"<text x='{x + bar_w/2:.1f}' y='{y_top + seg_h/2 + 4:.1f}' {FONT} font-size='11.5' font-weight='600' fill='{GROUND}' text-anchor='middle'>{v}</text>")
            acc += v
        top_y = y_of(total)
        parts.append(f"<text x='{x + bar_w/2:.1f}' y='{top_y - 8:.1f}' {FONT} font-size='11.5' font-weight='600' fill='{TEXT}' text-anchor='middle'>{total}</text>")
        parts.append(f"<text x='{x + bar_w/2:.1f}' y='{height - 10}' {LABEL_FONT} font-size='11' fill='{MUTED}' text-anchor='middle'>{_esc(r['month_label'])}</text>")

    parts.append("</svg>")
    return "".join(parts)


def monthly_calories_stacked_svg(rows: list, width: int = 520, height: int = 240) -> str:
    """Monthly active calories stacked by source (racquet/strength/cardio/
    unlogged movement) - where the burn actually comes from."""
    n = len(rows)
    if n == 0:
        return f"<svg width='{width}' height='{height}'></svg>"

    max_val = max(r["total"] for r in rows) * 1.15 or 1
    pad_left, pad_right, pad_bottom, pad_top = 48, 12, 28, 26
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_bottom - pad_top
    gap = plot_w / n
    bar_w = gap * 0.62

    def y_of(v: float) -> float:
        return pad_top + plot_h - (v / max_val) * plot_h

    parts = [f"<svg viewBox='0 0 {width} {height}' preserveAspectRatio='none' style='width:100%;height:100%;display:block'>"]
    parts.append(f"<rect width='{width}' height='{height}' fill='{GROUND}'/>")

    grid_step = max(round(max_val / 3 / 5000) * 5000, 5000)
    g = grid_step
    while g < max_val:
        gy = y_of(g)
        parts.append(f"<line x1='{pad_left}' y1='{gy:.1f}' x2='{width - pad_right}' y2='{gy:.1f}' stroke='{LINE_SOFT}' stroke-width='1'/>")
        parts.append(f"<text x='{pad_left - 8}' y='{gy + 4:.1f}' {FONT} font-size='10.5' fill='{DIM}' text-anchor='end'>{g/1000:.0f}k</text>")
        g += grid_step

    segments = [("racquet", "Racquet"), ("strength", "Strength"), ("cardio", "Cardio"), ("other", "Other"), ("unlogged", "Unlogged movement")]
    for i, r in enumerate(rows):
        x = pad_left + i * gap + (gap - bar_w) / 2
        acc = 0
        for key, name in segments:
            v = r[key]
            if not v:
                continue
            y_top = y_of(acc + v)
            y_bot = y_of(acc)
            pct = round(v / r["total"] * 100) if r["total"] else 0
            tip = _tip([f"{r['month_label']} · {name}", f"{v:,} cal · {pct}%"])
            parts.append(f"<rect data-tip='{tip}' x='{x:.1f}' y='{y_top:.1f}' width='{bar_w:.1f}' height='{(y_bot - y_top):.1f}' fill='{BUCKET_COLORS[key]}' opacity='0.9'/>")
            acc += v
        top_y = y_of(r["total"])
        parts.append(f"<text x='{x + bar_w/2:.1f}' y='{top_y - 8:.1f}' {FONT} font-size='11.5' font-weight='600' fill='{TEXT}' text-anchor='middle'>{round(r['total']/1000)}k</text>")
        parts.append(f"<text x='{x + bar_w/2:.1f}' y='{height - 10}' {LABEL_FONT} font-size='11' fill='{MUTED}' text-anchor='middle'>{_esc(r['month_label'])}</text>")

    parts.append("</svg>")
    return "".join(parts)


def month_calendar_svg(cal: dict, width: int = 420) -> str:
    """One cell per day of the current month through snapshot_date, colored
    by dominant bucket, numbered, weekday-aligned (columns = weeks, rows =
    weekdays Mon-Sun, matching the actual calendar)."""
    days = cal["days"]
    if not days:
        return f"<svg width='{width}' height='40'></svg>"

    from datetime import date as _date
    month_start = _date.fromisoformat(cal["month_start"])
    weekday_offset = month_start.weekday()  # Monday=0

    n_cols = -(-(weekday_offset + len(days)) // 7)
    pad_left = 34
    cell = min(46, max(20, int((width - pad_left - 8) / n_cols) - 6))
    gap = 6
    height = 7 * (cell + gap) + 8

    parts = [f"<svg viewBox='0 0 {width} {height}' style='width:100%;height:auto;display:block'>"]
    parts.append(f"<rect width='{width}' height='{height}' fill='{GROUND}'/>")

    dow = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for i, label in enumerate(dow):
        parts.append(f"<text x='0' y='{i * (cell + gap) + cell/2 + 4:.1f}' {FONT} font-size='11' fill='{DIM}'>{label}</text>")

    bucket_names = {"strength": "Strength", "racquet": "Racquet", "cardio": "Cardio", "other": "Other", None: "Rest day"}
    for i, day in enumerate(days):
        idx = weekday_offset + i
        col = idx // 7
        row = idx % 7
        x = pad_left + col * (cell + gap)
        y = row * (cell + gap)
        bucket = day["bucket"]
        color = BUCKET_COLORS.get(bucket, BUCKET_COLORS["unlogged"]) if bucket else BUCKET_COLORS["unlogged"]
        day_num = int(day["date"][-2:])
        tip = _tip([_human_date(day["date"]), bucket_names.get(bucket, "Rest day")])
        parts.append(f"<rect data-tip='{tip}' x='{x:.1f}' y='{y:.1f}' width='{cell}' height='{cell}' rx='4' fill='{color}' opacity='{0.9 if bucket else 0.55}'/>")
        text_color = GROUND if bucket else MUTED
        parts.append(f"<text x='{x + cell/2:.1f}' y='{y + cell/2 + 4:.1f}' {FONT} font-size='11.5' font-weight='{600 if bucket else 400}' fill='{text_color}' text-anchor='middle'>{day_num}</text>")

    parts.append("</svg>")
    return "".join(parts)


def weight_raw_points_svg(points: list, width: int = 420, height: int = 130) -> str:
    """Raw weight readings as plain dots - no line drawn, per v3, until
    there's enough data for a trend (that threshold is enforced by
    weight_chart_data, not here; this just draws whatever it's given)."""
    if not points:
        return f"<svg width='{width}' height='{height}'></svg>"

    values = [p["weight_lb"] for p in points]
    lo, hi = min(values), max(values)
    pad_val = (hi - lo) * 0.15 or 2
    lo, hi = lo - pad_val, hi + pad_val

    pad_left, pad_right, pad_top, pad_bottom = 8, 44, 16, 22
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    n = len(points)

    def y_of(v: float) -> float:
        return pad_top + plot_h - ((v - lo) / (hi - lo)) * plot_h

    parts = [f"<svg viewBox='0 0 {width} {height}' style='width:100%;height:auto;display:block'>"]
    parts.append(f"<rect width='{width}' height='{height}' fill='{GROUND}'/>")

    grid_step = max(round((hi - lo) / 3), 1)
    g = (int(lo / grid_step) + 1) * grid_step
    while g < hi:
        gy = y_of(g)
        parts.append(f"<line x1='{pad_left}' y1='{gy:.1f}' x2='{width - pad_right}' y2='{gy:.1f}' stroke='{LINE_SOFT}' stroke-width='1'/>")
        parts.append(f"<text x='{width - pad_right + 8}' y='{gy + 4:.1f}' {FONT} font-size='10.5' fill='{DIM}'>{g:.0f}</text>")
        g += grid_step

    for i, p in enumerate(points):
        x = pad_left + (i / max(n - 1, 1)) * plot_w
        y = y_of(p["weight_lb"])
        tip = _tip([_human_date(p["date"]), f"{p['weight_lb']} lb"])
        parts.append(f"<circle data-tip='{tip}' cx='{x:.1f}' cy='{y:.1f}' r='4' fill='{MUTED}'/>")
        if i == 0 or i == n - 1 or n <= 6:
            anchor = "start" if i == 0 else "end" if i == n - 1 else "middle"
            parts.append(f"<text x='{x:.1f}' y='{height - 6}' {LABEL_FONT} font-size='9.5' fill='{DIM}' text-anchor='{anchor}'>{_esc(_human_date(p['date']))}</text>")

    parts.append(f"<text x='{pad_left}' y='11' {LABEL_FONT} font-size='10' fill='{DIM}'>{n} reading{'s' if n != 1 else ''} · no line drawn until 8</text>")

    parts.append("</svg>")
    return "".join(parts)


# ---- Nutrition tab --------------------------------------------------------

def nutrition_daily_bars_svg(data: dict, width: int = 900, height: int = 220) -> str:
    """Daily calories from the MyFitnessPal import, plain bars (no goal
    line - no calorie goal is defined anywhere in this project) with an
    average reference line, tooltip per bar."""
    days = data["days"]
    n = len(days)
    if n == 0:
        return f"<svg width='{width}' height='{height}'></svg>"

    values = [d["calories"] for d in days]
    avg = sum(values) / len(values)
    max_val = max(values) * 1.1 or 1

    pad_left, pad_right, pad_bottom, pad_top = 44, 12, 26, 14
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_bottom - pad_top
    gap = plot_w / n
    bar_w = gap * 0.6
    y0 = pad_top + plot_h

    def y_of(v: float) -> float:
        return pad_top + plot_h - (v / max_val) * plot_h

    parts = [f"<svg viewBox='0 0 {width} {height}' preserveAspectRatio='none' style='width:100%;height:100%;display:block'>"]
    parts.append(f"<rect width='{width}' height='{height}' fill='{GROUND}'/>")

    grid_step = max(round(max_val / 4 / 250) * 250, 250)
    g = grid_step
    while g < max_val:
        gy = y_of(g)
        parts.append(f"<line x1='{pad_left}' y1='{gy:.1f}' x2='{width - pad_right}' y2='{gy:.1f}' stroke='{LINE_SOFT}' stroke-width='1'/>")
        parts.append(f"<text x='{pad_left - 8}' y='{gy + 4:.1f}' {FONT} font-size='10.5' fill='{DIM}' text-anchor='end'>{g:,.0f}</text>")
        g += grid_step

    for i, d in enumerate(days):
        v = d["calories"]
        x = pad_left + i * gap + (gap - bar_w) / 2
        bar_h = max(1.0, (v / max_val) * plot_h)
        tip = _tip([_human_date(d["date"]), f"{v:,.0f} cal", f"{d['protein_g']:.0f}g protein · {d['carbs_g']:.0f}g carbs · {d['fat_g']:.0f}g fat"])
        parts.append(f"<rect data-tip='{tip}' x='{x:.1f}' y='{y0 - bar_h:.1f}' width='{bar_w:.1f}' height='{bar_h:.1f}' rx='2' fill='{NEUTRAL}' opacity='0.85'/>")

    avg_y = y_of(avg)
    parts.append(f"<line x1='{pad_left}' y1='{avg_y:.1f}' x2='{width - pad_right}' y2='{avg_y:.1f}' stroke='{TEXT}' stroke-width='1' stroke-dasharray='3,3' opacity='0.6'/>")
    parts.append(f"<text x='{pad_left + 4}' y='{avg_y - 6:.1f}' {FONT} font-size='11' fill='{MUTED}'>avg {avg:,.0f}</text>")

    for i, anchor in ((0, "start"), (n - 1, "end")):
        x = pad_left + i * gap + bar_w / 2
        parts.append(f"<text x='{x:.1f}' y='{height - 8}' {LABEL_FONT} font-size='10' fill='{DIM}' text-anchor='{anchor}'>{_esc(_human_date(days[i]['date']))}</text>")

    parts.append("</svg>")
    return "".join(parts)


def meal_breakdown_svg(data: dict, width: int = 520, height: int = 90) -> str:
    """One 100%-stacked horizontal bar - Breakfast/Lunch/Dinner/Snacks share
    of total calories, direct-labeled (4 segments, under the no-legend-
    under-5 threshold)."""
    meals = data["meals"]
    total = data["total"]
    if not meals or not total:
        return f"<svg width='{width}' height='{height}'></svg>"

    colors = {"Breakfast": NEUTRAL, "Lunch": WARN, "Dinner": "#2FB8C6", "Snacks": "#8B7BB8"}
    bar_h = 28
    y = height - bar_h - 30

    parts = [f"<svg viewBox='0 0 {width} {height}' style='width:100%;height:auto;display:block'>"]
    parts.append(f"<rect width='{width}' height='{height}' fill='{GROUND}'/>")

    x = 0
    for m in meals:
        seg_w = m["pct"] / 100 * width
        tip = _tip([m["meal"], f"{m['calories']:,} cal · {m['pct']}%"])
        meal_color = colors.get(m["meal"], MUTED)
        parts.append(f"<rect data-tip='{tip}' x='{x:.1f}' y='{y}' width='{max(seg_w - 2, 0):.1f}' height='{bar_h}' rx='2' fill='{meal_color}' opacity='0.9'/>")
        if seg_w > 46:
            parts.append(f"<text x='{x + seg_w/2:.1f}' y='{y + bar_h/2 + 4:.1f}' {FONT} font-size='11' font-weight='600' fill='{GROUND}' text-anchor='middle'>{m['pct']}%</text>")
        label_y = y + bar_h + 16
        parts.append(f"<text x='{x + 4:.1f}' y='{label_y}' {LABEL_FONT} font-size='10.5' fill='{MUTED}'>{_esc(m['meal'])}</text>")
        x += seg_w

    parts.append("</svg>")
    return "".join(parts)


def protein_per_bodyweight_svg(data: dict, width: int = 520, height: int = 130) -> str:
    """Daily protein-per-bodyweight ratio, dots + a connecting line - a
    low-n metric, so kept visually simple like weight_raw_points_svg."""
    points = data["points"]
    if not points:
        return f"<svg width='{width}' height='{height}'></svg>"

    values = [p["ratio"] for p in points]
    lo, hi = min(values), max(values)
    pad_val = (hi - lo) * 0.15 or 0.05
    lo, hi = max(lo - pad_val, 0), hi + pad_val

    pad_left, pad_right, pad_top, pad_bottom = 8, 40, 16, 22
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    n = len(points)

    def y_of(v: float) -> float:
        return pad_top + plot_h - ((v - lo) / (hi - lo)) * plot_h

    parts = [f"<svg viewBox='0 0 {width} {height}' style='width:100%;height:auto;display:block'>"]
    parts.append(f"<rect width='{width}' height='{height}' fill='{GROUND}'/>")

    grid_step = max((hi - lo) / 3, 0.1)
    g = lo + grid_step
    while g < hi:
        gy = y_of(g)
        parts.append(f"<line x1='{pad_left}' y1='{gy:.1f}' x2='{width - pad_right}' y2='{gy:.1f}' stroke='{LINE_SOFT}' stroke-width='1'/>")
        parts.append(f"<text x='{width - pad_right + 8}' y='{gy + 4:.1f}' {FONT} font-size='10.5' fill='{DIM}'>{g:.2f}</text>")
        g += grid_step

    line_points = []
    for i, p in enumerate(points):
        x = pad_left + (i / max(n - 1, 1)) * plot_w
        y = y_of(p["ratio"])
        line_points.append((x, y))

    if len(line_points) >= 2:
        path = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in line_points)
        parts.append(f"<path d='{path}' fill='none' stroke='{NEUTRAL}' stroke-width='1.5' opacity='0.8'/>")

    for i, (p, (x, y)) in enumerate(zip(points, line_points)):
        tip = _tip([_human_date(p["date"]), f"{p['ratio']} g/lb"])
        parts.append(f"<circle data-tip='{tip}' cx='{x:.1f}' cy='{y:.1f}' r='3.5' fill='{MUTED}'/>")
        if i == 0 or i == n - 1:
            anchor = "start" if i == 0 else "end"
            parts.append(f"<text x='{x:.1f}' y='{height - 6}' {LABEL_FONT} font-size='9.5' fill='{DIM}' text-anchor='{anchor}'>{_esc(_human_date(p['date']))}</text>")

    parts.append("</svg>")
    return "".join(parts)


def weight_and_calories_dual_svg(data: dict, width: int = 900, height: int = 260) -> str:
    """Two aligned mini-panels sharing an x-axis: weight trend on top,
    daily calories below - a visual-only comparison, no computed
    relationship drawn between the two (see weight_and_calories_series'
    own docstring)."""
    calorie_days = data["calories"]
    weight_days = data["weight"]
    if not calorie_days:
        return f"<svg width='{width}' height='{height}'></svg>"

    n = len(calorie_days)
    date_index = {d["date"]: i for i, d in enumerate(calorie_days)}
    pad_left, pad_right = 44, 12
    plot_w = width - pad_left - pad_right
    gap = plot_w / n

    weight_h, cal_h, gap_h = 110, 90, 30
    top_pad, bottom_pad = 14, 24

    def x_of(i: float) -> float:
        return pad_left + i * gap + gap / 2

    parts = [f"<svg viewBox='0 0 {width} {height}' preserveAspectRatio='none' style='width:100%;height:100%;display:block'>"]
    parts.append(f"<rect width='{width}' height='{height}' fill='{GROUND}'/>")

    # ---- weight panel (top) ----
    w_top = top_pad
    w_plot_h = weight_h
    weight_pts = [(date_index[d["date"]], d["weight_lb"]) for d in weight_days if d["date"] in date_index]
    if weight_pts:
        w_values = [v for _, v in weight_pts]
        w_lo, w_hi = min(w_values), max(w_values)
        w_pad = (w_hi - w_lo) * 0.2 or 1
        w_lo, w_hi = w_lo - w_pad, w_hi + w_pad

        def wy_of(v: float) -> float:
            return w_top + w_plot_h - ((v - w_lo) / (w_hi - w_lo)) * w_plot_h

        parts.append(f"<text x='{pad_left}' y='{w_top - 2}' {LABEL_FONT} font-size='10' fill='{DIM}'>weight (lb)</text>")
        w_grid_step = max((w_hi - w_lo) / 3, 0.5)
        wg = w_lo + w_grid_step
        while wg < w_hi:
            wgy = wy_of(wg)
            parts.append(f"<line x1='{pad_left}' y1='{wgy:.1f}' x2='{width - pad_right}' y2='{wgy:.1f}' stroke='{LINE_SOFT}' stroke-width='1'/>")
            parts.append(f"<text x='{pad_left - 8}' y='{wgy + 4:.1f}' {FONT} font-size='10' fill='{DIM}' text-anchor='end'>{wg:.0f}</text>")
            wg += w_grid_step
        line_pts = [(x_of(i), wy_of(v)) for i, v in weight_pts]
        if len(line_pts) >= 2:
            path = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in line_pts)
            parts.append(f"<path d='{path}' fill='none' stroke='{NEUTRAL}' stroke-width='1.5' opacity='0.85'/>")
        for (i, v), (x, y) in zip(weight_pts, line_pts):
            tip = _tip([_human_date(calorie_days[i]["date"]), f"{v} lb"])
            parts.append(f"<circle data-tip='{tip}' cx='{x:.1f}' cy='{y:.1f}' r='3' fill='{MUTED}'/>")

    # ---- calories panel (bottom) ----
    c_top = w_top + w_plot_h + gap_h
    c_values = [d["calories"] for d in calorie_days]
    c_max = max(c_values) * 1.1 or 1

    def cy_of(v: float) -> float:
        return c_top + cal_h - (v / c_max) * cal_h

    parts.append(f"<text x='{pad_left}' y='{c_top - 2}' {LABEL_FONT} font-size='10' fill='{DIM}'>calories</text>")
    for i, d in enumerate(calorie_days):
        v = d["calories"]
        x = pad_left + i * gap + gap * 0.2
        bw = gap * 0.6
        bar_h = max(1.0, (v / c_max) * cal_h)
        tip = _tip([_human_date(d["date"]), f"{v:,.0f} cal"])
        parts.append(f"<rect data-tip='{tip}' x='{x:.1f}' y='{c_top + cal_h - bar_h:.1f}' width='{bw:.1f}' height='{bar_h:.1f}' rx='1.5' fill='{WARN}' opacity='0.8'/>")

    for i, anchor in ((0, "start"), (n - 1, "end")):
        x = x_of(i)
        parts.append(f"<text x='{x:.1f}' y='{height - 6}' {LABEL_FONT} font-size='9.5' fill='{DIM}' text-anchor='{anchor}'>{_esc(_human_date(calorie_days[i]['date']))}</text>")

    parts.append("</svg>")
    return "".join(parts)


def vo2max_trend_svg(data: dict, width: int = 900, height: int = 160) -> str:
    """VO2max (running) readings, positioned by real date offset (not
    index) since Garmin only recalculates this every week or two of real
    runs/hard cardio - the gaps between points are real and meaningful,
    not a regular daily cadence like most other charts in this file."""
    points = data["points"]
    n = len(points)
    if n == 0:
        return f"<svg width='{width}' height='{height}'></svg>"

    values = [p["vo2max_running"] for p in points]
    lo, hi = min(values), max(values)
    pad_val = (hi - lo) * 0.2 or 1
    lo, hi = lo - pad_val, hi + pad_val

    pad_left, pad_right, pad_top, pad_bottom = 8, 34, 16, 22
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    start_d = _date.fromisoformat(points[0]["date"])
    end_d = _date.fromisoformat(points[-1]["date"])
    total_days = max((end_d - start_d).days, 1)

    def x_of(date_str: str) -> float:
        d = _date.fromisoformat(date_str)
        return pad_left + ((d - start_d).days / total_days) * plot_w

    def y_of(v: float) -> float:
        return pad_top + plot_h - ((v - lo) / (hi - lo)) * plot_h

    parts = [f"<svg viewBox='0 0 {width} {height}' preserveAspectRatio='none' style='width:100%;height:100%;display:block'>"]
    parts.append(f"<rect width='{width}' height='{height}' fill='{GROUND}'/>")

    grid_step = max(round((hi - lo) / 3), 1)
    g = (int(lo / grid_step) + 1) * grid_step
    while g < hi:
        gy = y_of(g)
        parts.append(f"<line x1='{pad_left}' y1='{gy:.1f}' x2='{width - pad_right}' y2='{gy:.1f}' stroke='{LINE_SOFT}' stroke-width='1'/>")
        parts.append(f"<text x='{width - pad_right + 8}' y='{gy + 4:.1f}' {FONT} font-size='10.5' fill='{DIM}'>{g}</text>")
        g += grid_step

    line_points = [(x_of(p["date"]), y_of(p["vo2max_running"])) for p in points]
    if len(line_points) >= 2:
        path = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in line_points)
        parts.append(f"<path d='{path}' fill='none' stroke='{NEUTRAL}' stroke-width='1.5' opacity='0.8'/>")

    for p, (x, y) in zip(points, line_points):
        tip = _tip([_human_date(p["date"]), f"VO2max {p['vo2max_running']}"])
        parts.append(f"<circle data-tip='{tip}' cx='{x:.1f}' cy='{y:.1f}' r='3' fill='{MUTED}'/>")

    parts.append(f"<text x='{pad_left}' y='{height - 6}' {LABEL_FONT} font-size='9.5' fill='{DIM}'>{_esc(_human_date(points[0]['date']))}</text>")
    parts.append(f"<text x='{pad_left + plot_w:.1f}' y='{height - 6}' {LABEL_FONT} font-size='9.5' fill='{DIM}' text-anchor='end'>{_esc(_human_date(points[-1]['date']))}</text>")

    parts.append("</svg>")
    return "".join(parts)
