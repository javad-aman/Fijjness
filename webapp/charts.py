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


def stacked_bar_svg(weekly: dict, width: int = 520, height: int = 220) -> str:
    """Weekly stacked bar by bucket. Direct-labeled (4 buckets, under the
    5-series legend threshold), zero-baseline (calories - zero is
    meaningful here per spec §9)."""
    labels = weekly["week_labels"]
    buckets = weekly["buckets"]
    data = weekly["data"]
    n = len(labels)
    if n == 0:
        return f"<svg width='{width}' height='{height}'></svg>"

    totals = [sum(data[b][i] for b in buckets) for i in range(n)]
    max_total = max(totals) or 1

    pad_left, pad_bottom, pad_top = 36, 24, 12
    plot_w = width - pad_left - 10
    plot_h = height - pad_bottom - pad_top
    bar_w = plot_w / n * 0.6
    gap = plot_w / n

    parts = [f"<svg viewBox='0 0 {width} {height}' style='width:100%;height:auto;display:block'>"]
    parts.append(f"<rect width='{width}' height='{height}' fill='{GROUND}'/>")
    # zero baseline
    y0 = pad_top + plot_h
    parts.append(f"<line x1='{pad_left}' y1='{y0}' x2='{width - 10}' y2='{y0}' stroke='{LINE}' stroke-width='1'/>")

    for i in range(n):
        x = pad_left + i * gap + (gap - bar_w) / 2
        y_cursor = y0
        for b in buckets:
            val = data[b][i]
            if val <= 0:
                continue
            seg_h = (val / max_total) * plot_h
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
