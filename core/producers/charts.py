"""Branded static charts via matplotlib (Agg) — light-mode PNGs for blog embedding.

Palette & mark discipline follow the dataviz method: validated categorical hues in
fixed order, one axis, recessive hairline grid, thin marks, direct value labels,
tabular tick figures, text in ink tokens (not series colors). A "highlight" entity
(the project's own product) is drawn in the strong accent while the rest recede — persuasive but
honest.

spec = {
  "type": "bar" | "hbar" | "grouped_bar" | "line",
  "title": str, "source": str?, "unit": str? (e.g. "$/min", "ms", "%"), "y_label": str?,
  # single series:
  "categories": [str], "values": [num], "highlight": str?,
  # OR multi-series (grouped_bar / line):
  "x": [str], "series": [{"name": str, "values": [num]}],
  "note": str?,   # e.g. "lower is better"
}
"""
from __future__ import annotations

import textwrap
from pathlib import Path

# dataviz reference categorical order (light) + ink/surface tokens
CAT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
ACCENT = "#2a78d6"       # highlight / primary blue
BASE = "#b7d3f6"         # recessive light-blue for non-highlighted bars
INK = "#0b0b0b"
SUB = "#52514e"
MUTED = "#898781"
NOTE_WIDTH = 80          # characters a line of the note holds within the chart's width


def _fmt(v, unit):
    if isinstance(v, (int, float)):
        if float(v).is_integer():
            s = f"{v:,.0f}"
        else:  # keep sub-cent precision ($0.015 must not label as $0.01)
            decimals = 3 if abs(v) < 0.1 else 2
            s = f"{v:,.{decimals}f}".rstrip("0").rstrip(".")
    else:
        s = str(v)
    u = unit or ""
    if u.startswith("$"):
        return f"${s}{u[1:]}"
    return f"{s}{(' ' + u) if u and not u.startswith('%') else u}"


def plotted_values(spec) -> int:
    """How many numbers the spec would plot. A spec with none draws empty axes: a finding that
    asked to take chart_1 out came back from the producer as "Feature Comparison (Removed)" with
    no data, and the empty frame went into the Zoom article."""
    spec = spec or {}
    vals = list(spec.get("values") or []) + [d.get("value") for d in spec.get("data") or [] if isinstance(d, dict)]
    for s in spec.get("series") or []:
        vals += list((s or {}).get("values") or []) if isinstance(s, dict) else []
    return sum(1 for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool))


def build(spec, out_path, style_path=None):
    """Render a chart PNG. Returns out_path, or None on failure (a spec with nothing to plot
    is a failure: the old picture stays and the reason is printed)."""
    if not plotted_values(spec):
        print("[charts] error: the spec has no values to plot")
        return None
    fig = None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager  # noqa: F401

        if style_path and Path(style_path).exists():
            plt.style.use(str(style_path))

        typ = spec.get("type", "bar")
        unit = spec.get("unit", "")
        # a horizontal bar chart needs a row per category: at a fixed 4.3 in, a 23-model latency
        # chart put its wrapped labels on top of each other, so past eight rows the height grows
        n_cats = len(spec.get("categories") or spec.get("data") or [])
        height = 1.3 + 0.32 * n_cats if typ == "hbar" and n_cats > 8 else 4.3
        fig, ax = plt.subplots(figsize=(7.4, height))

        def _wrap(label, width=14):
            """Break a long category label across lines so neighbours cannot collide.

            The prompt asks for short labels and the model obliges most of the time; when it
            does not, "AssemblyAI Universal-2" and "OpenAI Whisper" overprint each other and
            the chart is unreadable. Layout is code's job, not the model's.
            """
            words, lines, cur = str(label).split(), [], ""
            for w in words:
                if cur and len(cur) + 1 + len(w) > width:
                    lines.append(cur)
                    cur = w
                else:
                    cur = f"{cur} {w}".strip()
            if cur:
                lines.append(cur)
            return "\n".join(lines[:3])

        if typ in ("bar", "hbar"):
            raw_cats = spec.get("categories") or [d.get("label") for d in spec.get("data", [])]
            # wrapped for DISPLAY, raw kept for MATCHING: comparing the highlight against a
            # wrapped label would silently lose our own accent bar on any two-word name
            # a horizontal chart's labels sit beside the bars and have room to run wider
            cats = [_wrap(c, 24 if typ == "hbar" else 14) for c in raw_cats]
            vals = spec.get("values") or [d.get("value") for d in spec.get("data", [])]
            hl = spec.get("highlight")
            # color follows the entity: with a highlight -> accent vs recessive base;
            # without -> ONE hue for the whole series (never cycle hues across positions)
            colors = [ACCENT if (hl and str(c) == str(hl)) else (BASE if hl else ACCENT)
                      for c in raw_cats]
            if typ == "hbar":
                y = range(len(cats))
                bars = ax.barh(list(y), vals, color=colors, height=0.62)
                ax.set_yticks(list(y)); ax.set_yticklabels(cats)
                ax.invert_yaxis()
                ax.grid(axis="x"); ax.grid(axis="y", visible=False)
                for b, v, c in zip(bars, vals, raw_cats):
                    strong = hl and str(c) == str(hl)
                    ax.text(b.get_width(), b.get_y() + b.get_height() / 2, "  " + _fmt(v, unit),
                            va="center", ha="left", fontsize=10.5, color=INK if strong else SUB,
                            fontweight="bold" if strong else "normal")
            else:
                x = range(len(cats))
                bars = ax.bar(list(x), vals, color=colors, width=0.62)
                ax.set_xticks(list(x)); ax.set_xticklabels(cats, fontsize=10)
                top = max([v for v in vals if isinstance(v, (int, float))] or [1])
                for b, v, c in zip(bars, vals, raw_cats):
                    strong = hl and str(c) == str(hl)
                    ax.text(b.get_x() + b.get_width() / 2, b.get_height() + top * 0.02, _fmt(v, unit),
                            va="bottom", ha="center", fontsize=10.5, color=INK if strong else SUB,
                            fontweight="bold" if strong else "normal")
                ax.set_ylim(0, top * 1.16)

        elif typ == "grouped_bar":
            import numpy as np
            xs = spec.get("x", [])
            series = spec.get("series", [])
            n = len(series); w = 0.8 / max(n, 1)
            idx = np.arange(len(xs))
            for i, s in enumerate(series):
                off = (i - (n - 1) / 2) * w
                ax.bar(idx + off, s.get("values", []), width=w * 0.92, color=CAT[i % len(CAT)], label=s.get("name", f"S{i+1}"))
            ax.set_xticks(idx); ax.set_xticklabels(xs, fontsize=10)
            allv = [v for s in series for v in s.get("values", []) if isinstance(v, (int, float))]
            if allv:
                ax.set_ylim(0, max(allv) * 1.32)  # headroom so the legend clears the bars
            if n >= 2:
                ax.legend(loc="upper right", ncol=n)

        elif typ == "line":
            xs = spec.get("x", [])
            for i, s in enumerate(spec.get("series", [])):
                ax.plot(xs, s.get("values", []), marker="o", markersize=7, linewidth=3.2,
                        color=CAT[i % len(CAT)], label=s.get("name", f"S{i+1}"))
            if len(spec.get("series", [])) >= 2:
                ax.legend(loc="best")

        # title + chrome
        title = spec.get("title", "")
        # the note wraps: the saved figure grows to hold whatever it draws (savefig.bbox: tight),
        # and a 240-character note on one line made a 4549x1023 picture with the chart in its
        # left third (the live run of 2026-10-05)
        note = textwrap.wrap(str(spec.get("note") or ""), NOTE_WIDTH)
        ax.set_title(title, loc="left", pad=16 + 14 * len(note) if note else 12)
        if note:
            ax.text(0, 1.02, "\n".join(note), transform=ax.transAxes, va="bottom", ha="left",
                    fontsize=10.5, color=MUTED)
        if spec.get("y_label"):
            ax.set_ylabel(spec["y_label"])
        ax.tick_params(length=0)

        if spec.get("source"):
            # sits below the tick labels; offset scales with the tallest (multiline) label
            max_lines = max([lbl.get_text().count("\n") + 1 for lbl in ax.get_xticklabels()] or [1])
            ax.annotate("Source: " + spec["source"], xy=(0, 0), xycoords="axes fraction",
                        xytext=(-10, -(30 + 14 * max_lines)), textcoords="offset points",
                        fontsize=8.5, color=MUTED, ha="left", va="top", annotation_clip=False)

        fig.savefig(out_path)
        return out_path
    except Exception as e:  # noqa: BLE001
        print(f"[charts] error: {e}")
        return None
    finally:
        # a spec that failed halfway left its figure open, and pyplot keeps every open figure
        # in memory for the life of the process (the Studio's one worker)
        if fig is not None:
            import matplotlib.pyplot as plt

            plt.close(fig)
