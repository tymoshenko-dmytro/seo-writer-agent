"""Before → after with word-level marks, for the change card:
removed words struck through on red, added words on green, the rest as it was."""
from __future__ import annotations

import difflib
import re

from markupsafe import Markup, escape

# words (with their apostrophes and hyphens), single punctuation marks, runs of whitespace
_TOKENS = re.compile(r"\s+|[\w'’-]+|[^\w\s]")


def _text(value) -> str:
    """A change group keeps its before and after as lists of blocks: one text, blocks apart."""
    if isinstance(value, (list, tuple)):
        return "\n\n".join(str(v) for v in value)
    return str(value or "")


def _runs(a: list[str], b: list[str]) -> list[tuple[str, str, str]]:
    """(kind, removed, added) runs. Two changes with only whitespace between them read as one
    ("under a second" → "about 800 milliseconds", not three interleaved pairs)."""
    runs: list[list] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        old, new = "".join(a[i1:i2]), "".join(b[j1:j2])
        if op == "equal":
            runs.append(["equal", old, new])
        elif runs and runs[-1][0] == "change":
            runs[-1][1] += old
            runs[-1][2] += new
        elif len(runs) >= 2 and runs[-1][0] == "equal" and not runs[-1][1].strip() and runs[-2][0] == "change":
            space = runs.pop()[1]
            runs[-1][1] += space + old
            runs[-1][2] += space + new
        else:
            runs.append(["change", old, new])
    return [tuple(r) for r in runs]


def word_diff(before, after) -> Markup:
    a, b = _TOKENS.findall(_text(before)), _TOKENS.findall(_text(after))
    out: list[str] = []
    for kind, old, new in _runs(a, b):
        if kind == "equal":
            out.append(str(escape(old)))
            continue
        if old:
            out.append(f'<del class="diff-del">{escape(old)}</del>')
        if new:
            out.append(f'<ins class="diff-ins">{escape(new)}</ins>')
    return Markup("".join(out))
