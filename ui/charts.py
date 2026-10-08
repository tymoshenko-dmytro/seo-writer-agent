"""Charts drawn on the server as inline SVG: no chart library, the
colours from the state palette in a fixed order (ui.css, `.chart rect.s0`…`.s4`), a legend with
words beside every chart."""
from __future__ import annotations

from markupsafe import Markup, escape


def _money(v: float) -> str:
    return f"${v:,.0f}" if v >= 10 else f"${v:,.2f}".rstrip("0").rstrip(".")


def stacked_bars(labels: list[str], series: list[str], values: list[list[float]],
                 width: int = 720, height: int = 220, every: int = 7) -> Markup:
    """One bar per label, stacked by series (values[i][j]: label i, series j), with three
    horizontal grid lines and every `every`-th label under the axis. Each segment has a title:
    the label, the series and its amount."""
    left, bottom, top = 40, 22, 8
    plot_w, plot_h = width - left, height - bottom - top
    peak = max((sum(v) for v in values), default=0) or 1
    step = plot_w / max(len(labels), 1)
    bar = max(step * 0.62, 2)
    out = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
           f'aria-label="Spend per day, by part of the article">']
    for k in range(4):
        y = top + plot_h - plot_h * k / 3
        out.append(f'<line class="grid-line" x1="{left}" x2="{width}" y1="{y:.1f}" y2="{y:.1f}"/>')
        out.append(f'<text class="axis" x="{left - 6}" y="{y + 3:.1f}" text-anchor="end">{_money(peak * k / 3)}</text>')
    for i, (label, row) in enumerate(zip(labels, values)):
        x = left + i * step + (step - bar) / 2
        y = top + plot_h
        for j, v in enumerate(row):
            if not v:
                continue
            h = plot_h * v / peak
            y -= h
            out.append(f'<rect class="s{j % 5}" x="{x:.1f}" y="{y:.1f}" width="{bar:.1f}" height="{h:.1f}" rx="1">'
                       f'<title>{escape(label)} · {escape(series[j])}: ${v:,.2f}</title></rect>')
        if i % every == 0:
            out.append(f'<text class="axis" x="{x + bar / 2:.1f}" y="{height - 6}" text-anchor="middle">{escape(label)}</text>')
    out.append("</svg>")
    return Markup("".join(out))
