"""Small text operations on an article's Markdown that a review's changes share
(core/qa_changes): a redrawn picture's alt text and caption, a picture taken off the page, and
whether two addresses are the same page; with the link patterns the fixer's context uses.
Pure functions over strings.
"""
from __future__ import annotations

import html
import re

from ..store import IMAGE_ALT, caption_of, picture_alone

_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


_FACTS_URL = re.compile(r"https?://[^\s)\]>\"'`]+")


def retitle_visual(md: str, asset: str, alt: str, caption: str) -> str:
    """After a redraw, the image's alt text and the caption line under it take the producer's
    words for the new picture. Left alone they described the chart it replaced ("four figures"
    over a two-bar chart), and the edit check rightly raised that as a new defect (interpreter
    replay, 2026-09-28). Missing words from the producer leave the old ones in place."""
    path = "assets/" + str(asset).rsplit("/", 1)[-1]
    alt = " ".join(str(alt or "").replace("]", ")").replace("[", "(").split())
    # no asterisk in a caption line, and no backslash left that escaped one (R83D-02: "New title\\*"
    # left "\\" before the link's "]")
    caption = " ".join(re.sub(r"\\?\*", "", str(caption or "")).split())
    # the picture on a line of its own and the caption line under it, as the page's parser reads
    # them (RC8C-01, R39B-01)
    lines = md.split("\n")
    at = next((i for i, ln in enumerate(lines) if picture_alone(ln, path)), None)
    if at is not None:
        # the caption is the picture's own by the picture's line as it was (its alt may prove it)
        if caption and at + 1 < len(lines) and caption_of(lines[at], lines[at + 1]):
            lines[at + 1] = f"*{caption}*"
        if alt:
            lines[at] = f"![{alt}]({path})"
        return "\n".join(lines)
    pat = re.compile(r"!\[(" + IMAGE_ALT + r")\]\(" + re.escape(path) + r"\)(\n\*[^*\n]+\*)?")

    def sub(m):
        cap = m.group(2) or ""
        if caption and cap:
            cap = f"\n*{caption}*"
        return f"![{alt or m.group(1)}]({path})" + cap

    return pat.sub(sub, md, count=1)


def remove_picture(md: str, asset: str) -> tuple[str, str | None]:
    """`md` without the picture `asset` and its caption, and None; or `md` unchanged and why not.

    It reads the page the way it is shown: the Studio's and WordPress's own parser and renderer
    (core.tools.markdown_render), so a picture inside a code example stays and one the page shows
    inside escaped HTML is counted (reviews R731-02, R03-01). It takes out one shown picture on a
    line of its own; a file shown more than once, or inside a table, a list or a quote, is the
    person's to take out. Nothing else changes: only the blank line the removal leaves doubled is
    closed, never the blank lines of the rest of the text or of its code (R03-02). The result is
    rendered and looked at: a removal that leaves the file on the page is not done."""
    from ..tools.markdown_render import parse, render

    def shown(text):
        toks = parse(text)
        return [i for i, t in enumerate(toks)
                if t.type == "inline" and any(c.type == "image" and (c.attrs or {}).get("src") == asset
                                              for c in t.children or [])], toks

    def on_page(text):
        return bool(shown(text)[0]) or f'src="{html.escape(asset, quote=True)}"' in render(text)

    hits, toks = shown(md)
    if not hits:
        return md, f"{asset} is not on the page any more"
    if len(hits) > 1:
        return md, f"{asset} is shown {len(hits)} times: take out the one you mean in the editor"
    i = hits[0]
    box = toks[i - 1] if i else None
    if box is None or box.type != "paragraph_open" or not box.map or box.level:
        return md, f"{asset} sits inside a table, a list or a quote: take it out in the editor"
    start, end = box.map
    lines = md.split("\n")
    # the picture's own line inside its paragraph (a caption, and text, may follow it without a
    # blank line: the articles are written that way); a picture inside a sentence is not ours
    at = next((n for n in range(start, end) if picture_alone(lines[n].rstrip(), asset)), None)
    if at is None:
        return md, f"{asset} is not on a line of its own: take it out in the editor"
    first = last = at
    if at + 1 < end and caption_of(lines[at], lines[at + 1]):
        last = at + 1
    elif at + 1 == end and i + 2 < len(toks) and toks[i + 2].type == "paragraph_open" and toks[i + 2].map:
        s2, e2 = toks[i + 2].map                  # a caption standing as the next block
        if e2 - s2 == 1 and caption_of(lines[at], lines[s2]) and s2 - end <= 1:
            last = s2
    # one blank line where the picture stood, not two; nothing else in the text is touched
    if 0 < first and last + 1 < len(lines) and not lines[first - 1].strip() and not lines[last + 1].strip():
        last += 1
    out = "\n".join(lines[:first] + lines[last + 1:])
    if on_page(out):
        return md, f"{asset} is still on the page after the removal"
    return out, None


def same_page(url_a: str, url_b: str) -> bool:
    norm = lambda u: str(u or "").split("#")[0].rstrip("/").lower().replace("://www.", "://")  # noqa: E731
    return bool(url_a) and norm(url_a) == norm(url_b)
