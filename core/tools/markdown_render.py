"""Markdown -> HTML using markdown-it-py.

One renderer drives BOTH the Studio's pages and the WordPress post body, so what the editor
previews is exactly what gets published.

Raw HTML in the Markdown is escaped, not passed through (RF-03): the text comes from models,
research excerpts and people, and the Studio shows it on the same origin as its settings and
publishing. The articles never needed raw HTML (a report's literal "<details> accordion" was
opening a real disclosure widget). Link and image URLs go through markdown-it's own check,
which drops javascript:, vbscript:, file: and non-image data: targets.
"""
from __future__ import annotations

from markdown_it import MarkdownIt
from markdown_it.rules_inline import StateInline

_md = MarkdownIt("commonmark", {"html": False})
for rule in ("table", "strikethrough"):
    try:
        _md.enable(rule)
    except Exception:  # noqa: BLE001 — rule may not exist in some versions
        pass


def render(markdown_text: str) -> str:
    return _md.render(markdown_text or "")


def parse(markdown_text: str):
    """The tokens of the parser `render` uses, with its settings: what a caller finds in them is
    what the Studio and WordPress show (review R03-01: a default parser read raw HTML where the
    page escapes it, and missed a picture the page showed)."""
    return _md.parse(markdown_text or "")


# the same parser, its link rule recording where each link it shows stands in the text
_md_links = MarkdownIt("commonmark", {"html": False})
for rule in ("table", "strikethrough"):
    try:
        _md_links.enable(rule)
    except Exception:  # noqa: BLE001
        pass
_link_rule = next(r for r in _md_links.inline.ruler.__rules__ if r.name == "link").fn


def _recording_link(state, silent: bool) -> bool:
    start = state.pos
    shown = _link_rule(state, silent)
    if shown and not silent:
        # with the text it was read in: an image's alt is read by a state of its own, and a link
        # there is alt text, not a link of the line (RF09-01)
        state.env.setdefault("_links", []).append((state.src, start, state.pos))
    return shown


_md_links.inline.ruler.at("link", _recording_link)


def link_spans(line: str) -> list[tuple[int, int, str]]:
    """The links the parser shows in one line of Markdown: (start, end, the address as written),
    where each stands in `line`, as the parser itself found them: escapes, code spans, brackets in
    the text and parentheses in the address at any depth it reads (the caption-link reviews
    R4B3-01 … R11D-02: patterns for a link disagreed with the parser one case at a time)."""
    env: dict = {}
    tokens = _md_links.parse(line or "", env)
    inline = next((t for t in tokens if t.type == "inline"), None)
    if inline is None or not env.get("_links"):
        return []
    # the parser reads a NUL as U+FFFD (its normalize step), one character for one
    src, shift = inline.content, (line or "").replace("\x00", "\ufffd").find(inline.content)
    if shift < 0:
        return []
    out = []
    for read_in, start, end in env["_links"]:
        if read_in != src:
            continue
        state = StateInline(src, _md_links, {}, [])
        label_end = _md_links.helpers.parseLinkLabel(state, start, True)
        if label_end < 0:
            continue
        pos = label_end + 2
        while pos < end and src[pos] in " \t\n":
            pos += 1
        dest = _md_links.helpers.parseLinkDestination(src, pos, end)
        out.append((shift + start, shift + end, line[shift + pos:shift + dest.pos] if dest.ok else ""))
    return out


def visible_text(line: str) -> str:
    """The words one line of Markdown shows, as the parser reads them: no link addresses, no
    emphasis marks, entities and escapes resolved ("*[Setup guide](https://…/guide(2026))*" shows
    "Setup guide", R11D-03)."""
    inline = next((t for t in _md.parse(line or "") if t.type == "inline"), None)
    kids = (inline.children or []) if inline is not None else []
    return "".join(" " if k.type in ("softbreak", "hardbreak") else k.content for k in kids
                   if k.type in ("text", "code_inline", "softbreak", "hardbreak"))
