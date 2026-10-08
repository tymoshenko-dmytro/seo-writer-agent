"""Lucide icons, drawn inline: 16 px, stroke 1.75, always beside a word. The shapes are vendored
in ui/vendor/lucide/icons.json with Lucide's ISC licence; the stroke is set here, on the <svg>, so
every icon draws the same."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from markupsafe import Markup, escape

ICONS_FILE = Path(__file__).resolve().parent / "vendor" / "lucide" / "icons.json"


@lru_cache(maxsize=1)
def shapes() -> dict[str, str]:
    return json.loads(ICONS_FILE.read_text(encoding="utf-8"))["icons"]


def icon(name: str, size: int = 16, cls: str = "") -> Markup:
    """The icon as inline SVG, hidden from screen readers (the word beside it is what they read).
    An unknown name raises: a typo must fail the page's test, not draw nothing."""
    body = shapes()[name]
    klass = f"icon {escape(cls)}".strip()
    return Markup(f'<svg class="{klass}" width="{int(size)}" height="{int(size)}" viewBox="0 0 24 24" '
                  'fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" '
                  f'stroke-linejoin="round" aria-hidden="true" focusable="false">{body}</svg>')
