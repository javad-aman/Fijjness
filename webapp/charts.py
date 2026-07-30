"""Hand-rolled server-rendered SVG charts - no JS charting library, so the
spec's specific chart rules (direct labels, no legend under 5 series, one
highlighted + grey context series, uncertainty bands, meaningful-only
zero-baselines) are hit exactly rather than fought against a library's
defaults. Palette values are the literal hex codes from static/style.css's
CSS custom properties (SVG can't reference CSS variables across a separate
stylesheet reliably enough to depend on here).
"""
from __future__ import annotations

GROUND = "#0E1116"
LINE = "#262D38"
TEXT = "#E8EAED"
MUTED = "#7C8794"
AHEAD = "#4FD1A5"
BEHIND = "#F2545B"
NEUTRAL = "#6C8EFF"
AMBER = "#F2A93B"

# ahead/behind are reserved exclusively for pace on/off-state (spec §9: "Green
# and red mean on-pace and off-pace, nowhere else") - so categorical charts
# below never use them. Buckets get non-pace hues instead.
BUCKET_COLORS = {
    "strength": NEUTRAL,
    "racquet": AMBER,
    "cardio": "#4A9B8E",  # muted teal - distinct from both ahead-green and neutral-blue
    "other": MUTED,
}

FONT = "font-family='IBM Plex Mono, ui-monospace, monospace'"
LABEL_FONT = "font-family='Inter, sans-serif'"


def _esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;")


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
        # week label (short, every other week if crowded)
        if n <= 8 or i % 2 == 0:
            wk_label = labels[i][5:]  # MM-DD
            parts.append(
                f"<text x='{x + bar_w / 2:.1f}' y='{height - 6}' {LABEL_FONT} font-size='9' fill='{MUTED}' text-anchor='middle'>{_esc(wk_label)}</text>"
            )

    if total_line:
        points = [(bar_centers[i], y0 - (total_line[i] / max_val) * plot_h) for i in range(n)]
        path = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in points)
        parts.append(f"<path d='{path}' fill='none' stroke='{TEXT}' stroke-width='1.5'/>")
        for x, y in points:
            parts.append(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='2' fill='{TEXT}'/>")
        lx, ly = points[-1]
        parts.append(
            f"<text x='{lx:.1f}' y='{ly - 8:.1f}' {LABEL_FONT} font-size='9' fill='{TEXT}' text-anchor='end'>total active cal</text>"
        )

    # direct labels (legend-free, per spec: no legend under 5 series)
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


def steps_30day_svg(steps_data: dict, width: int = 900, height: int = 260) -> str:
    """30 daily bars, goal reference line, 7d moving average overlay. Bars
    ahead-of-goal in `ahead`, behind in `muted` (never `behind` red - a
    single day under goal isn't a failure state, per spec §2.4). Missing
    days render as gaps (no bar), never a zero-height bar. Today's slot
    gets an outlined placeholder if not yet synced, rather than either a
    fabricated bar or an indistinguishable gap."""
    days = steps_data["days"]
    goal = steps_data["goal"]
    ma7 = steps_data["moving_avg_7d"]
    n = len(days)
    if n == 0:
        return f"<svg width='{width}' height='{height}'></svg>"

    values = [d["steps"] for d in days if d["steps"] is not None]
    max_val = max(values + [goal]) * 1.1 if values else goal * 1.1

    pad_left, pad_right, pad_bottom, pad_top = 8, 78, 22, 16
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_bottom - pad_top
    gap = plot_w / n
    bar_w = gap * 0.6
    y0 = pad_top + plot_h

    def y_of(v: float) -> float:
        return pad_top + plot_h - (v / max_val) * plot_h

    parts = [f"<svg viewBox='0 0 {width} {height}' preserveAspectRatio='none' style='width:100%;height:100%;display:block'>"]
    parts.append(f"<rect width='{width}' height='{height}' fill='{GROUND}'/>")
    parts.append(f"<line x1='{pad_left}' y1='{y0:.1f}' x2='{width - pad_right}' y2='{y0:.1f}' stroke='{LINE}' stroke-width='1'/>")

    goal_y = y_of(goal)
    parts.append(
        f"<line x1='{pad_left}' y1='{goal_y:.1f}' x2='{width - pad_right}' y2='{goal_y:.1f}' "
        f"stroke='{MUTED}' stroke-width='1' stroke-dasharray='3,3'/>"
    )
    parts.append(f"<text x='{width - pad_right + 6}' y='{goal_y + 3:.1f}' {LABEL_FONT} font-size='10' fill='{MUTED}'>goal {goal:,}</text>")

    for i, day in enumerate(days):
        x = pad_left + i * gap + (gap - bar_w) / 2
        v = day["steps"]
        is_last = i == n - 1
        if v is None:
            if is_last:
                ph_h = 10
                parts.append(
                    f"<rect x='{x:.1f}' y='{y0 - ph_h:.1f}' width='{bar_w:.1f}' height='{ph_h}' "
                    f"fill='none' stroke='{MUTED}' stroke-width='1' rx='2'/>"
                )
            continue
        bar_h = (v / max_val) * plot_h
        color = AHEAD if v >= goal else MUTED
        parts.append(f"<rect x='{x:.1f}' y='{y0 - bar_h:.1f}' width='{bar_w:.1f}' height='{bar_h:.1f}' fill='{color}' rx='2'/>")

    ma_points = [
        (pad_left + i * gap + gap / 2, y_of(v)) for i, v in enumerate(ma7) if v is not None
    ]
    if len(ma_points) >= 2:
        path = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in ma_points)
        parts.append(f"<path d='{path}' fill='none' stroke='{NEUTRAL}' stroke-width='2'/>")

    first_label = days[0]["date"][5:]
    last_label = days[-1]["date"][5:]
    parts.append(f"<text x='{pad_left}' y='{height - 6}' {LABEL_FONT} font-size='9' fill='{MUTED}'>{_esc(first_label)}</text>")
    parts.append(f"<text x='{width - pad_right}' y='{height - 6}' {LABEL_FONT} font-size='9' fill='{MUTED}' text-anchor='end'>{_esc(last_label)}</text>")

    parts.append("</svg>")
    return "".join(parts)


def training_calendar_weeks_svg(cal: dict, cell: int = 14, gap: int = 3) -> str:
    """GitHub-style: columns = weeks, rows = weekdays (Mon top). Days before
    real coverage began render nothing - not an empty cell. Multiple buckets
    on one day split the cell diagonally."""
    days = cal["days"]
    if not days:
        return "<svg width='100' height='40'></svg>"

    n_cols = -(-len(days) // 7)
    width = n_cols * (cell + gap)
    height = 7 * (cell + gap) + 20

    parts = [f"<svg viewBox='0 0 {width} {height}' style='width:{width}px;height:auto;display:block'>"]
    parts.append(f"<rect width='{width}' height='{height}' fill='{GROUND}'/>")

    for i, day in enumerate(days):
        if day["before_coverage"]:
            continue
        col = i // 7
        row = i % 7
        x = col * (cell + gap)
        y = row * (cell + gap) + 4
        buckets = day["buckets"]
        if not buckets:
            parts.append(f"<rect x='{x}' y='{y}' width='{cell}' height='{cell}' rx='2' fill='{LINE}'/>")
        elif len(buckets) == 1:
            color = BUCKET_COLORS.get(buckets[0], MUTED)
            parts.append(f"<rect x='{x}' y='{y}' width='{cell}' height='{cell}' rx='2' fill='{color}'/>")
        else:
            c1 = BUCKET_COLORS.get(buckets[0], MUTED)
            c2 = BUCKET_COLORS.get(buckets[1], MUTED)
            clip_id = f"cal-{i}"
            parts.append(f"<clipPath id='{clip_id}'><rect x='{x}' y='{y}' width='{cell}' height='{cell}' rx='2'/></clipPath>")
            parts.append(f"<g clip-path='url(#{clip_id})'>")
            parts.append(f"<rect x='{x}' y='{y}' width='{cell}' height='{cell}' fill='{c1}'/>")
            parts.append(f"<polygon points='{x + cell},{y} {x + cell},{y + cell} {x},{y + cell}' fill='{c2}'/>")
            parts.append("</g>")

    ly = height - 6
    lx = 0
    for b, color in BUCKET_COLORS.items():
        parts.append(f"<rect x='{lx}' y='{ly - 8}' width='8' height='8' fill='{color}'/>")
        parts.append(f"<text x='{lx + 11}' y='{ly}' {LABEL_FONT} font-size='9' fill='{MUTED}'>{_esc(b)}</text>")
        lx += 12 + len(b) * 6 + 12

    parts.append("</svg>")
    return "".join(parts)


def sparkline_svg(points: list, width: int = 900, height: int = 90,
                   band_low: float = None, band_high: float = None) -> str:
    """A shape-recognition sparkline, not a precise-reading chart - no axis
    labels beyond context. Points outside the shaded band get a dot; the
    band is either a fixed reference range (sleep) or a mean+/-SD (resting
    HR), whichever the caller passes."""
    if len(points) < 2:
        return f"<svg width='{width}' height='{height}'></svg>"

    values = [p["value"] for p in points]
    lo, hi = min(values), max(values)
    if band_low is not None:
        lo = min(lo, band_low)
    if band_high is not None:
        hi = max(hi, band_high)
    rng = (hi - lo) or 1

    pad_x, pad_y = 6, 8
    plot_w = width - 2 * pad_x
    plot_h = height - 2 * pad_y

    def xy(i, v):
        x = pad_x + (i / (len(points) - 1)) * plot_w
        y = pad_y + plot_h - ((v - lo) / rng) * plot_h
        return x, y

    parts = [f"<svg viewBox='0 0 {width} {height}' preserveAspectRatio='none' style='width:100%;height:100%;display:block'>"]
    parts.append(f"<rect width='{width}' height='{height}' fill='{GROUND}'/>")

    if band_low is not None and band_high is not None:
        _, y_top = xy(0, band_high)
        _, y_bot = xy(0, band_low)
        parts.append(f"<rect x='{pad_x}' y='{y_top:.1f}' width='{plot_w:.1f}' height='{(y_bot - y_top):.1f}' fill='{LINE}'/>")

    line_points = [xy(i, v) for i, v in enumerate(values)]
    path = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in line_points)
    parts.append(f"<path d='{path}' fill='none' stroke='{NEUTRAL}' stroke-width='1.5'/>")

    if band_low is not None and band_high is not None:
        for i, v in enumerate(values):
            if v < band_low or v > band_high:
                x, y = line_points[i]
                parts.append(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='2.5' fill='{AMBER}'/>")

    parts.append("</svg>")
    return "".join(parts)


def weight_trend_svg(chart_series: list, checkpoint: dict, width: int = 900, height: int = 240) -> str:
    """Raw readings as faint dots, EWMA as the emphasized line, a straight
    trajectory to the checkpoint target, and (when available) a Theil-Sen
    projection date range noted as text rather than drawn as false-precise
    geometry. Y-axis never starts at zero - weight per spec §3."""
    if len(chart_series) < 2:
        return f"<svg width='{width}' height='{height}'></svg>"

    dates = [row["date"] for row in chart_series]
    raw = [row["weight_lb"] for row in chart_series]
    ewma = [row["ewma"] for row in chart_series]

    values = raw + ewma
    if checkpoint:
        values = values + [checkpoint["target"]]
    lo, hi = min(values), max(values)
    pad_val = (hi - lo) * 0.1 or 1
    lo, hi = lo - pad_val, hi + pad_val

    pad_x, pad_top, pad_bottom = 8, 16, 22
    plot_w = width - 2 * pad_x
    plot_h = height - pad_top - pad_bottom
    n = len(chart_series)

    def xy(i, v):
        x = pad_x + (i / (n - 1)) * plot_w
        y = pad_top + plot_h - ((v - lo) / (hi - lo)) * plot_h
        return x, y

    parts = [f"<svg viewBox='0 0 {width} {height}' preserveAspectRatio='none' style='width:100%;height:100%;display:block'>"]
    parts.append(f"<rect width='{width}' height='{height}' fill='{GROUND}'/>")

    for i, v in enumerate(raw):
        x, y = xy(i, v)
        parts.append(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='2' fill='{MUTED}' opacity='0.6'/>")

    ewma_points = [xy(i, v) for i, v in enumerate(ewma)]
    path = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in ewma_points)
    parts.append(f"<path d='{path}' fill='none' stroke='{NEUTRAL}' stroke-width='2'/>")

    if checkpoint:
        x0, y0 = xy(n - 1, ewma[-1])
        target_x = width - pad_x
        target_y = pad_top + plot_h - ((checkpoint["target"] - lo) / (hi - lo)) * plot_h
        parts.append(f"<line x1='{x0:.1f}' y1='{y0:.1f}' x2='{target_x:.1f}' y2='{target_y:.1f}' stroke='{MUTED}' stroke-width='1' stroke-dasharray='3,3'/>")
        parts.append(f"<text x='{target_x:.1f}' y='{target_y - 6:.1f}' {LABEL_FONT} font-size='9' fill='{MUTED}' text-anchor='end'>target {checkpoint['target']} · {_esc(checkpoint['date'])}</text>")

    last_x, last_y = xy(n - 1, ewma[-1])
    parts.append(f"<text x='{last_x:.1f}' y='{last_y - 10:.1f}' {FONT} font-size='12' fill='{TEXT}' text-anchor='end'>{ewma[-1]:.1f}</text>")

    parts.append(f"<text x='{pad_x}' y='{height - 6}' {LABEL_FONT} font-size='9' fill='{MUTED}'>{_esc(dates[0])}</text>")
    parts.append(f"<text x='{pad_x + plot_w:.1f}' y='{height - 6}' {LABEL_FONT} font-size='9' fill='{MUTED}' text-anchor='end'>{_esc(dates[-1])}</text>")

    parts.append("</svg>")
    return "".join(parts)
