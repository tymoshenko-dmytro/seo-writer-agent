"""What the article's page shows: the rail's three facts, the Review tab as a queue of decisions,
the article with the review's marks, and History. Read from the engine through the same views an
agent reads (core/agent_door.py); this module only arranges them for the page.

Marks are placed in the Markdown before it is rendered: a private-use character pair stands where
a mark opens and closes, the renderer keeps them as text (it escapes raw HTML), and they become
the mark's tags afterwards. So a quote is marked where it is, whatever Markdown sits around it.
"""
from __future__ import annotations

import html as html_lib
import re
from datetime import datetime, timezone

from markupsafe import Markup

from core import agent_door, publication, store, usage, versions
from core import qa_changes as qc
from core import qa_model as qm
from core.tools.markdown_render import render as md_render
from core.tools.markdown_render import visible_text as md_text

from .diff import _TOKENS, _runs
from .privacy import scrub_errors

# the marks' stand-ins in the Markdown: (open n), close, inserted words, removed words
_OPEN, _NUM_END, _CLOSE = "", "", ""
_INS, _INS_END, _DEL, _DEL_END = "", "", "", ""
_STANDINS = re.compile("[-]")
_IMAGE = r"!\[" + store.IMAGE_ALT + r"\]\(\s*<?{src}(?:[?#][^)\s>]*)?>?(?:\s+\"[^\"]*\")?\s*\)"


def who(name) -> str:
    """A name as the page says it: the person at this Studio is "you"."""
    return "you" if str(name or "") in ("studio-operator", "the editor", "the SEO tab") else str(name or "someone")


def when(iso) -> str:
    """An ISO time as the page writes it: "Oct 2, 10:12" (this year), "Oct 2 2025, 10:12"."""
    if not iso:
        return ""
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return str(iso)[:16].replace("T", " ")
    if t.tzinfo is not None:
        t = t.astimezone()
    year = "" if t.year == datetime.now().year else f" {t.year}"
    return f"{t:%b} {t.day}{year}, {t:%H:%M}"


# ---------------------------------------------------------------------------------------------
# marks in the Markdown
# ---------------------------------------------------------------------------------------------
def _destinations(md: str) -> list[tuple[int, int]]:
    """Where each link's or picture's address sits: (start, end) of what is inside `](…)`, read
    as CommonMark reads it (a `<…>` address, else balanced parentheses and backslash escapes up to
    the first space; review RF9C-04). The title after the address is not part of it."""
    out, i, n = [], 0, len(md)
    while True:
        i = md.find("](", i)
        if i < 0:
            return out
        j = i + 2
        while j < n and md[j] in " \t":
            j += 1
        if j < n and md[j] == "<":
            k = md.find(">", j + 1)
            if k > 0:
                out.append((j + 1, k))
                i = k
                continue
        k, depth = j, 0
        while k < n:
            c = md[k]
            if c == "\\" and k + 1 < n:
                k += 2
                continue
            if c.isspace():
                break
            if c == "(":
                depth += 1
            elif c == ")":
                if depth == 0:
                    break
                depth -= 1
            k += 1
        if k > j:
            out.append((j, k))
        i = max(k, i + 2)


def _outside_links(md: str, at: int) -> int:
    """A mark never opens inside a link's or a picture's address: it moves to the address's end."""
    for a, b in _destinations(md):
        if a <= at < b + 1 and a < at:
            return b + 1 if b < len(md) and md[b] == ")" else b
    return at


def _diff_standins(before: str, after: str) -> str:
    """`after` with each changed run marked: the removed words (struck) before the added ones.
    Inside a link's or a picture's address nothing is marked: the address is the new one as it
    is (the change of an address is shown apart, `links_changed`; review RB32-05)."""
    out = []
    for kind, old, new in _runs(_TOKENS.findall(before), _TOKENS.findall(after)):
        if kind == "equal":
            out.append(new)
            continue
        if old.strip():
            out.append(_DEL + old + _DEL_END)
        if new:
            out.append(_INS + new + _INS_END)

    text = "".join(out)
    for a, b in reversed(_destinations(text)):
        inner = re.sub(_DEL + ".*?" + _DEL_END, "", text[a:b], flags=re.S)
        text = text[:a] + inner.replace(_INS, "").replace(_INS_END, "") + text[b:]
    return text


def _addresses(md: str) -> list[str]:
    return [md[a:b] for a, b in _destinations(md or "")]


def links_changed(before: str, after: str) -> list[tuple[str, str]]:
    """The addresses a change replaced, in order: (old, new) pairs, "" for none."""
    a, b = _addresses(before), _addresses(after)
    if a == b:
        return []
    import difflib

    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op != "equal":
            olds, news = a[i1:i2], b[j1:j2]
            for k in range(max(len(olds), len(news))):
                out.append((olds[k] if k < len(olds) else "", news[k] if k < len(news) else ""))
    return out


def _render(md: str, numbers: dict[int, tuple[str, str]] | None = None, slug: str = "") -> str:
    """The Markdown rendered, its stand-ins made into marks: `numbers` {n: (colour, link)}."""
    out = md_render(md)
    numbers = numbers or {}

    def opener(m):
        n, pic = int(m.group(1)), " hl-pic" if m.group(2) else ""
        colour, link, tip = (tuple(numbers.get(abs(n), ("yellow", ""))) + ("",))[:3]
        if n <= 0:                                    # a second part of item -n (a caption), or a
            return f'<mark class="hl is-{colour}">'   # mark in a review card: no number, no anchor
        # the number says what to fix on hover (the operator, 2026-10-05): the item's own words
        tip = " ".join(str(tip or "").split())
        attrs = f' data-tip="{html_lib.escape(tip[:300], quote=True)}"' if tip else ""
        label = html_lib.escape(f"Item {n}: {tip[:300]}" if tip else f"Item {n} on the Review tab", quote=True)
        num = (f'<a class="hl-n" href="{link}" aria-label="{label}"{attrs}>{n}</a>' if link else "")
        return f'<mark class="hl is-{colour}{pic}" id="mark-{n}">{num}'

    out = re.sub(_OPEN + r"(-?\d+)(p?)" + _NUM_END, opener, out)
    out = (out.replace(_CLOSE, "</mark>").replace(_INS, '<ins class="diff-ins">').replace(_INS_END, "</ins>")
           .replace(_DEL, '<del class="diff-del">').replace(_DEL_END, "</del>"))
    # a stand-in that reached an address anyway (percent-encoded by the renderer) is dropped
    out = re.sub(r"%EE%80%8[0-6]", "", out)
    return _STANDINS.sub("", out)


_BLOCK_LEAD = re.compile(r"[ \t]*(?:#{1,6}[ \t]+|[-*+][ \t]+|\d+[.)][ \t]+|>[ \t]?)*")


def _keep_syntax(md: str, a: int, b: int, inner: str | None) -> tuple[int, int, str | None]:
    """A mark opens after a line's block syntax (a heading's #s, a list's bullet, a quote's >)
    and inside emphasis delimiters, so the Markdown around it reads as it did."""
    if a == 0 or md[a - 1] == "\n":
        lead = _BLOCK_LEAD.match(md, a).end() - a
        if lead and (inner is None or inner.startswith(md[a:a + lead])):
            inner = inner[lead:] if inner is not None else None
            a += lead
    while a < b and md[a] in "*_" and inner is None:
        a += 1
    while b > a and md[b - 1] in "*_" and inner is None:
        b -= 1
    return a, b, inner


def _insert(md: str, spans: list[tuple[int, int, str, str | None]]) -> str:
    """Stand-ins put into `md`: spans (start, end, open text, the replacement of md[start:end] or
    None), applied from the last, overlapping ones dropped."""
    out, last = md, len(md) + 1
    for a, b, opening, inner in sorted(spans, key=lambda s: s[0], reverse=True):
        if b > last:
            continue
        a, b, inner = _keep_syntax(out, a, b, inner)
        if inner is None:                             # a mark around words: never inside an address
            a = _outside_links(out, a)
            b = max(a, _outside_links(out, b))
        middle = out[a:b] if inner is None else inner
        out = out[:a] + opening + middle + _CLOSE + out[b:]
        last = a
    return out


# ---------------------------------------------------------------------------------------------
# where an item sits in the body
# ---------------------------------------------------------------------------------------------
def _image_at(body: str, asset: str) -> tuple[int, int] | None:
    """Where the picture's image is in `body`: its line when it stands alone there, as the page's
    parser reads it (RC8C-01), else the first image of it inside a sentence."""
    i = store.picture_line(body, asset)
    if i is not None:
        start = sum(len(ln) + 1 for ln in body.split("\n")[:i])
        return start, start + len(body.split("\n")[i])
    m = re.search(_IMAGE.format(src=re.escape(asset)), body)
    return (m.start(), m.end()) if m else None


def _caption_at(body: str, at: tuple[int, int]) -> tuple[int, int] | None:
    """The caption line right under the image at `at`, from its line break to its end, when it is
    that picture's caption (store.caption_of, R39B-01, R83D-01)."""
    end = at[1]
    if not body.startswith("\n", end):
        return None
    stop = body.find("\n", end + 1)
    stop = len(body) if stop < 0 else stop
    return (end, stop) if store.caption_of(body[at[0]:end], body[end + 1:stop].strip()) else None


def _image_span(body: str, asset: str) -> tuple[int, int] | None:
    """The picture's Markdown, with the caption line under it."""
    at = _image_at(body, asset)
    if not at:
        return None
    cap = _caption_at(body, at)
    return at[0], (cap[1] if cap else at[1])


def _picture_marks(body: str, asset: str, n: int, inner_colour_of: int | None = None) -> list[tuple]:
    """Marks for a picture: around the image, and inside its caption's asterisks (a mark around
    them would undo the italics)."""
    at = _image_at(body, asset)
    if not at:
        return []
    out = [(at[0], at[1], f"{_OPEN}{n}p{_NUM_END}", None)]
    cap = _caption_at(body, at)
    if cap:
        text = body[cap[0]:cap[1]]
        a = cap[0] + text.index("*") + 1
        b = cap[0] + text.rindex("*")
        if b > a:
            out.append((a, b, f"{_OPEN}-{n}{_NUM_END}", None))
    return out


def _heading_span(body: str, heading: str) -> tuple[int, int] | None:
    """The heading's words (after its #s, so a mark keeps it a heading)."""
    for s in qc.sections(body):
        if s["heading"] == " ".join(str(heading).split()):
            line_end = body.find("\n", s["start"])
            line_end = line_end if line_end >= 0 else len(body)
            lead = re.match(r"[ \t]*#{1,6}[ \t]*", body[s["start"]:line_end])
            return s["start"] + (lead.end() if lead else 0), line_end
    return None


def _target_span(body: str, on: dict | None) -> tuple[int, int] | None:
    if not on:
        return None
    if on.get("quote"):
        at = body.find(on["quote"])
        return (at, at + len(on["quote"])) if at >= 0 else None
    if on.get("heading"):
        return _heading_span(body, on["heading"])
    if on.get("asset"):
        return _image_span(body, on["asset"])
    return None


def section_at(body: str, at: int | None) -> str:
    """The heading of the section `at` sits in ("The introduction" before the first one)."""
    if at is None:
        return ""
    inside = [s for s in qc.sections(body) if s["start"] <= at < s["end"] and s["level"] > 1]
    return inside[-1]["heading"] if inside else "The introduction"


def _block(body: str, span: tuple[int, int]) -> tuple[int, int] | None:
    return qc.paragraph_of(body, span[0], span[1]) or qc.paragraph_of(body, span[0], span[0])


# ---------------------------------------------------------------------------------------------
# the Review tab: a queue of decisions
# ---------------------------------------------------------------------------------------------
KIND_WORD = {"text": "text", "fact": "fact", "link": "link", "keyword": "keyword", "picture": "picture",
             "structure": "structure", "meta": "meta"}


def _will(item: dict) -> str:
    action, kind = item.get("action"), item.get("kind")
    if item["type"] == "outline":
        return "Only you can make it, in the editor: merge, move or reorder the sections there."
    if item["type"] == "left":
        return ""               # the buttons say it (the operator, 2026-10-07: the card was crowded)
    if item.get("choices"):
        return "Pick one: removing it is free; making it again keeps the old picture in the versions."
    if action == "remove_picture":
        return "The picture and its caption leave the article; the file stays in the versions. Free."
    if kind == "fact":
        return "The machine rewrites the claim in this sentence and nothing else."
    if action in ("redraw", "recapture"):
        return "The machine makes the picture again; the old one stays in the versions."
    if action == "add_picture":
        return ("The machine makes a new picture for this place and puts it after the paragraph: it picks "
                "the form (a diagram, a chart, a screenshot it checks, or an illustration).")
    return "The machine makes this change in its place and nothing else."


_CHOICE_LABEL = {"remove_picture": "Remove the picture", "redraw": "Redraw it", "recapture": "Re-capture it"}


def _needs_word(it: dict, folder=None) -> str | None:
    """Which of the page's keys an item's change needs: "ai", "images", "both", or None (free)."""
    from .views.common import finding_needs, needs_word

    return needs_word(finding_needs({"action": it.get("action"), "target": it.get("on")}, folder))


def review_queue(art: store.Article, body: str, rv: dict, costs: dict) -> list[dict]:
    """The latest review's items in the order the page shows them: what needs the person, then
    what changed, then what is closed; the first two in the article's order and numbered."""
    needs, made, closed = [], [], []
    for it in rv.get("needs_you") or []:
        span = _target_span(body, it.get("on"))
        item = {**it, "group": "needs", "at": span[0] if span else None,
                "section": it.get("section") or section_at(body, span[0] if span else None),
                "title": it.get("text") or "", "will": "", "cost": None,
                # why the machine did not make it: a tool's error names no file on the server
                "why": scrub_errors(it.get("why"))}
        item["will"] = _will(item)
        item["needs"] = _needs_word(it, art.dir)
        item["choices"] = [{**c, "label": _CHOICE_LABEL.get(c.get("action"), "Make it again"),
                            "needs": _needs_word({"action": c.get("action"), "on": it.get("on")}, art.dir),
                            "cost": None if c.get("action") == "remove_picture" else costs.get("visual_edit")}
                           for c in it.get("choices") or [] if isinstance(c, dict)]
        on = it.get("on") or {}
        if on.get("asset"):
            item["picture"] = on["asset"]
        elif span and on.get("quote"):
            blk = _block(body, span)
            if blk:
                md = body[blk[0]:blk[1]]
                a, b = span[0] - blk[0], span[1] - blk[0]
                item["context"] = Markup(_pictures_addressed(art, _render(_insert(md, [(a, b, f"{_OPEN}0{_NUM_END}", None)]))))
                item["edit_md"] = md.strip()
        if item["type"] == "proposal" and it.get("action") == "add_picture":
            item["cost"] = costs.get("visual_insert")
        elif item["type"] == "proposal" and it.get("action") != "remove_picture" and not item["choices"]:
            item["cost"] = costs.get("proposals")
        elif item["type"] == "left":
            item["cost"] = costs.get("fix")
        needs.append(item)
    for g in rv.get("changed") or []:
        where = list(g.get("where") or [])
        before, after = list(g.get("before") or []), list(g.get("after") or [])
        first_after = next((a for w, a in zip(where, after) if not str(w).startswith("the frontmatter")), None)
        at = body.find(first_after.strip()) if isinstance(first_after, str) and first_after.strip() else -1
        half = qm.half_done(g)
        item = {"id": g["id"], "group": "needs" if half else "made", "type": "half" if half else "change",
                "at": at if at >= 0 else None, "kept_as_is": g.get("kept_as_is"),
                "kept_by": who((g.get("kept_as_is") or {}).get("who")) if g.get("kept_as_is") else None,
                "adjusted": g.get("adjusted"),
                "adjusted_by": who((g.get("adjusted") or {}).get("who")) if g.get("adjusted") else None,
                "section": section_at(body, at if at >= 0 else None) or (where[0] if where else ""),
                "title": "; ".join(t for t in g.get("texts") or [] if t) or "A change",
                # the check's word as a person reads it (its error stays in the record, R058-08)
                "check": g.get("check"), "check_note": g.get("check_said"), "v": g.get("version_n"),
                "latest": g.get("reverts_now"), "reverts": g.get("reverts"), "places": []}
        for w, b, a in zip(where, before, after):
            w = str(w)
            if w.startswith("assets/"):
                kept = (g.get("kept") or {}).get(w)
                item["picture"] = w
                item["places"].append({"where": w, "picture": w,
                                       # the picture it replaced, from the group's kept copy
                                       "before": f"/a/{art.slug}/{kept}" if kept else None})
                continue
            item["places"].append({"where": w, "html": Markup(_pictures_addressed(art, _render(_diff_standins(str(b or ""), str(a or "")))))
                                   if not w.startswith("the frontmatter") else None,
                                   "before": b, "after": a, "links": links_changed(str(b or ""), str(a or ""))})
        if at >= 0 and isinstance(first_after, str):
            blk = _block(body, (at, at + len(first_after.strip())))
            if blk and body[blk[0]:blk[1]].strip() == first_after.strip():
                item["edit_md"] = first_after.strip()
        (needs if half else made).append(item)
    in_groups = {x for g in rv.get("changed") or [] for x in g.get("findings") or []}
    for it in rv.get("closed") or []:
        if it["id"] in in_groups:                     # shown by its change card
            continue
        if it.get("status") == "changed" and it.get("changed_by"):
            c = it["changed_by"]
            made.append({**it, "group": "made", "type": "yours", "at": None, "title": it.get("text") or "",
                         "section": it.get("section") or "", "by": who(c.get("who")),
                         "when": when(c.get("at")), "places": []})
            continue
        how = ""
        if it.get("status") == "not_important" and (it.get("closed") or {}).get("already"):
            c = it["closed"]
            how = f"Nothing to do: {c.get('why') or ''}"           # the code found it done already
        elif it.get("status") == "not_important":
            c = it.get("closed") or {}
            how = f"Not important · {who(c.get('who'))}, {when(c.get('at'))}" + (f": {c['why']}" if c.get("why") else "")
        elif it.get("changed_by"):
            c = it["changed_by"]
            how = f"Changed by {who(c.get('who'))}, {when(c.get('at'))}"
        else:
            how = "Changed by the machine"
        closed.append({**it, "group": "closed", "title": it.get("text") or "", "how": how})
    key = (lambda x: (x["at"] is None, x["at"] or 0))
    needs.sort(key=key)
    made.sort(key=key)
    for n, it in enumerate(needs + made, 1):
        it["n"] = n
    return needs + made + closed


def _pictures_addressed(art: store.Article, html: str) -> str:
    from . import meta as M

    return M.versioned_assets(html, art.slug, art.dir)


_H1 = re.compile(r"^\s*#\s+.*$", re.M)            # the article's own title: the page shows it above


def _open_marks(body: str, items: list[dict]) -> tuple[list[tuple], dict]:
    """The marks of the open items (spans for `_insert`) and their numbers {n: (colour, link, tip)}.
    A mark on the article's H1 is left out: `article_html` drops that line, and the count promised
    a mark nobody could see (RBC6-02); the item stays on the Review tab."""
    h1 = _H1.search(body)
    spans, numbers = [], {}
    for it in items:
        n = it.get("n")
        if not n or it["group"] != "needs":
            continue
        if it.get("picture"):
            marks = _picture_marks(body, it["picture"], n)
        else:
            span = _target_span(body, it.get("on"))
            marks = [(span[0], span[1], f"{_OPEN}{n}{_NUM_END}", None)] if span else []
        if h1 and any(a < h1.end() and b > h1.start() for a, b, *_ in marks):
            continue
        if marks:
            spans += marks
            numbers[n] = ("yellow", f"?tab=review#item-{n}", _tip(it))
    return spans, numbers


def marked_article(slug: str, body: str, items: list[dict]) -> str:
    """The article rendered with what still needs the person: each open item's words (or its
    picture) on yellow, numbered, linked to its card, and saying what to fix on hover. What the
    machine already changed is not marked: a person who sees the article for the first time
    cannot tell what was there before (the operator, 2026-10-05); its card shows before → after."""
    spans, numbers = _open_marks(body, items)
    return article_html(slug, _insert(body, spans), numbers)


def _tip(it: dict) -> str:
    """What a mark's number says on hover: how much it matters, what kind it is, and the item's
    own words, with the phrase a keyword item is about when its words do not name it ("…include
    this phrase naturally" named none, 2026-10-05)."""
    text = it.get("title") or ""
    phrase = it.get("phrase")
    if phrase and phrase.lower() not in text.lower():
        text += f" The phrase: “{phrase}”."
    head = " · ".join(x for x in (str(it.get("severity") or "").capitalize(), it.get("kind") or "") if x)
    return f"{head}: {text}" if head else text


def marked_count(body: str, items: list[dict]) -> int:
    return len(shown_marks(body, items))


def shown_marks(body: str, items: list[dict]) -> set[int]:
    """The numbers the marked article shows: `_insert` drops a mark that overlaps another (two
    items on one heading), so those are left out the same way; a card offers *Show in the
    article* only for these."""
    spans, _ = _open_marks(body, items)
    shown, last = set(), len(body) + 1
    for a, b, opening, _inner in sorted(spans, key=lambda x: x[0], reverse=True):
        if b > last:
            continue
        m = re.search(r"(-?\d+)", opening)
        if m and int(m.group(1)) > 0:
            shown.add(int(m.group(1)))
        last = a
    return shown


def article_html(slug: str, body: str, numbers: dict | None = None) -> str:
    """The body as the page shows it (its own H1 dropped: the page's title is above), the
    pictures addressed through the article's route."""
    from . import meta as M

    body = re.sub(r"^\s*#\s+.*$", "", body, count=1, flags=re.M)
    body = re.sub(r"<!--\s*visual:(\w+).*?-->", "", body, flags=re.DOTALL)
    art = None
    try:
        from .context import article as get_article

        art = get_article(slug)
    except Exception:  # noqa: BLE001 — a preview without its folder shows the pictures unversioned
        pass
    return M.versioned_assets(_render(body, numbers, slug), slug, art.dir if art else None)


# ---------------------------------------------------------------------------------------------
# the rail: the review, the person's review, WordPress
# ---------------------------------------------------------------------------------------------
def rail(art: store.Article, version: str, rv: dict, project, costs: dict) -> dict:
    acc = publication.accepted_state(art, version)
    post = publication.post_state(art, version)
    rec = publication.published(art)
    action = publication.accept_action(publication.on_accept(project), rec)
    migrated = _migrated(art)
    review = rv.get("review")
    if rv.get("state") == "unreadable":
        r = {"word": "Unreadable", "colour": "red", "time": "the run record does not parse"}
    elif not review:
        r = {"word": "Not reviewed", "colour": "gray", "quiet": True,
             "time": ("Not reviewed in the new system. The old checklist is kept as history." if migrated
                      else "No review has run on this article yet.")}
    elif review.get("valid") is False:
        # no read came back (or too few): nothing it lists means the article is clear (RB32-04)
        r = {"word": "The review did not work", "colour": "red",
             "time": f"{review['id']}, {when(review.get('at'))}: {reads(review)}"}
    else:
        n = rv["counts"]["needs_you"]
        partial = (review.get("reads_requested") or 0) > (review.get("valid_reads") or 0) > 0
        r = {"word": f"{n} need{'s' if n == 1 else ''} you" if n else "Nothing open",
             "colour": "yellow" if n else "green", "quiet": not n,
             "score": rv.get("score"),
             "time": f"{review['id']}, {when(review.get('at'))}" + (f" · {reads(review)}" if partial else "")}
    r["migrated"] = migrated
    a = acc.get("accepted") or {}
    if acc["state"] == "accepted":
        y = {"word": "Accepted", "colour": "green", "time": f"by {who(a.get('who'))}, {when(a.get('at'))}"}
    elif acc["state"] == "changed_since":
        y = {"word": "Changed since you accepted", "colour": "yellow",
             "time": f"Accepted by {who(a.get('who'))}, {when(a.get('at'))}"}
    else:
        y = {"word": "Not accepted", "colour": "gray", "quiet": True, "time": ""}
    if a.get("origin") and a.get("origin") != "person":
        y["note"] = "carried over from the old automatic READY" if "ready" in str(a["origin"]).lower() else a["origin"]
    if post.get("post_id"):
        word, colour, quiet = post_word(post)
        w = {"word": word, "colour": colour, "quiet": quiet}
        bits = []
        if (post.get("last_known") or {}).get("checked_at"):
            bits.append(f"read {when(post['last_known']['checked_at'])}")
        if post.get("changed_since_sent"):
            bits.append("changed since sent")
        elif post.get("sent_version"):
            bits.append("this version")
        w["time"] = " · ".join(bits)
    else:
        w = {"word": "Not sent", "colour": "gray", "quiet": True, "time": ""}
    last = post.get("last_attempt") or {}
    if post.get("unknown"):
        w["alert"] = f"The send of {when(last.get('at')) or 'the last attempt'} was not confirmed: check the post in WordPress."
    elif last and last.get("outcome") not in (None, "sent", "unknown"):
        from .privacy import scrub

        w["alert"] = f"The send of {when(last.get('at')) or 'the last attempt'} failed: {scrub(str(last.get('detail') or 'no reason given'))}"
    return {"review": r, "yours": y, "wp": w, "post": post, "accepted": acc,
            "accept_action": action or "", "accept_label": publication.accept_label(action, rec),
            "accept_note": _accept_note(action), "review_cost": costs.get("review")}


def reads(review: dict) -> str:
    """How many of the review's reads came back: "1 of 3 reads came back"."""
    got, asked = review.get("valid_reads"), review.get("reads_requested")
    if got is None or asked is None:
        return "its reads did not come back"
    return f"{got} of {asked} read{'s' if asked != 1 else ''} came back"


def _accept_note(action: str | None) -> str:
    return {None: "", "": "", "draft": "On Accept: send it to the draft", "publish": "On Accept: publish it on the blog",
            "update_live": "On Accept: update the live post"}.get(action, "")


def _migrated(art: store.Article) -> bool:
    """Reviewed in the old system: its old checklist is kept (`legacy_findings` and the count in
    `review.migration` once migrated, `findings` before)."""
    try:
        run = art.run()
    except store.CorruptFile:
        return False
    moved = ((run.get("review") or {}).get("migration") or {}).get("legacy")
    return bool(moved or run.get("legacy_findings") or run.get("findings"))


# the old ledger's states, in words (core/qa_migration.LEGACY_OPEN are the open ones)
OLD_STATES = {"open": "open", "applied": "changed, not reviewed again", "already_satisfied": "found already done",
              "resolved": "resolved", "stuck": "could not be changed", "contested": "disputed"}


def old_checklist(art: store.Article) -> dict | None:
    """The old checklist as it was when the article moved to the new review, for reading only:
    each finding with its section and its old state; nothing from it runs or is carried."""
    try:
        run = art.run()
    except store.CorruptFile:
        return None
    items = run.get("legacy_findings") if "legacy_findings" in run else run.get("findings")
    items = [it for it in items or [] if isinstance(it, dict)]
    if not items:
        return None
    migration = (run.get("review") or {}).get("migration") or {}
    rows = [{"id": it.get("id"), "text": it.get("latest_text") or it.get("text") or "",
             "section": it.get("section") or "", "severity": it.get("severity") or "",
             "state": "waived by a person" if it.get("waived") else OLD_STATES.get(it.get("state"), it.get("state") or "")}
            for it in items]
    return {"items": rows, "open": migration.get("legacy_open"), "at": when(migration.get("at")) if migration.get("at") else None}


def costs_of(root, prices) -> dict:
    """The page's estimates, each "about $0.60" (or None when it has no price)."""
    out = {}
    for op in ("review", "proposals", "fix", "cover", "visual_edit", "visual_insert"):
        e = agent_door.estimate(root, op, prices)
        out[op] = f"about ${e['usd']:.2f}" if e.get("usd") is not None else None
    return out


# ---------------------------------------------------------------------------------------------
# History: the versions, what it cost, what happened
# ---------------------------------------------------------------------------------------------
def history(art: store.Article, version: str, prices, rail_facts: dict) -> dict:
    grouped, count = versions_list(art, rail_facts["accepted"], rail_facts["post"])
    try:
        cost = usage.article_summary(art.dir, prices)
    except Exception:  # noqa: BLE001 — a ledger that cannot be read leaves the section empty
        cost = {"total": 0.0, "by_category": [], "by_operation": [], "estimated": False}
    return {"versions": grouped, "count": count, "cost": cost, "events": _events(art), "old": old_checklist(art)}


def versions_list(art: store.Article, accepted: dict, post: dict) -> tuple[list[dict], int]:
    """The versions newest first, a review's changes grouped under it, the accepted and the sent
    version marked: History and the editor's Versions panel show the same list."""
    listing = versions.listing(art)
    acc = (accepted.get("accepted") or {}).get("version")
    sent = post.get("sent_version")
    post_id = post.get("post_id")
    rail_facts = {"post": post}
    rows = []
    for v in listing:
        marks = []
        if acc and v["token"] == acc:
            marks.append(("Accepted", "green"))
        if sent and v["token"] == sent:
            marks.append((f"On WordPress #{post_id}", "orange" if rail_facts["post"].get("live") else "green"))
        rows.append({**v, "when": when(v.get("at")), "marks": marks, "who": who(v.get("who"))})
    grouped: list[dict] = []
    for v in rows:
        by = str(v.get("who") or "")
        last = grouped[-1] if grouped else None
        if by.startswith("the review ") and v.get("group"):
            if last and last.get("review") == by:
                last["items"].append(v)
                continue
            grouped.append({"review": by, "items": [v], "when": v["when"]})
        else:
            grouped.append(v)
    for g in grouped:
        if g.get("review"):
            g["title"] = g["review"][0].upper() + g["review"][1:]
            g["detail"] = f"{len(g['items'])} change{'s' if len(g['items']) != 1 else ''}, one version each"
    return grouped, len(listing)


def _events(art: store.Article) -> list[dict]:
    """What happened, newest first: the reviews, the change groups (reverted ones too), the
    acceptances, the sends and the post's reads."""
    out = []
    try:
        run = art.run()
    except store.CorruptFile:
        run = {}
    rv = run.get("review") or {}
    for r in rv.get("reviews") or []:
        mine = [f for f in rv.get("findings") or [] if f.get("review") == r.get("id")]
        groups = [g for g in rv.get("changes") or [] if g.get("review") == r.get("id")]
        out.append({"at": r.get("at"), "colour": "yellow", "what": f"Review {r.get('id')}",
                    "detail": f"{len(mine)} finding{'s' if len(mine) != 1 else ''}, "
                              f"{len(groups)} change{'s' if len(groups) != 1 else ''} made"})
    for g in rv.get("changes") or []:
        if g.get("reverted"):
            out.append({"at": g["reverted"].get("at"), "colour": "gray", "what": f"Change {g['id']} reverted",
                        "detail": ", ".join(str(w) for w in g.get("where") or [])})
    try:
        rec = art.record()
    except store.CorruptFile:
        rec = {}
    acc = rec.get("accepted") or {}
    if acc.get("at"):
        out.append({"at": acc["at"], "colour": "green", "what": "Accepted this version", "who": who(acc.get("who"))})
    labels = {"draft": "Sent as a draft", "publish": "Published on the blog", "update_live": "Updated the live post",
              "unpublish": "Taken off the blog"}
    for s in rec.get("sends") or []:
        out.append({"at": s.get("at"), "colour": "orange" if s.get("outcome") == "sent" else "red",
                    "what": labels.get(s.get("action"), str(s.get("action") or "A send")),
                    "detail": "" if s.get("outcome") == "sent" else f"outcome: {s.get('outcome')}", "who": who(s.get("by"))})
    known = ((rec.get("published") or {}).get("last_known") or {})
    if known.get("checked_at"):
        out.append({"at": known["checked_at"], "colour": "blue", "what": "WordPress post read",
                    "detail": f"status: {known.get('status') or 'unknown'}"})
    for e in out:
        e["when"] = when(e.get("at"))
    return sorted(out, key=lambda e: str(e.get("at") or ""), reverse=True)


# ---------------------------------------------------------------------------------------------
# Pictures
# ---------------------------------------------------------------------------------------------
def pictures(art: store.Article, body: str, rv: dict) -> list[dict]:
    """Every picture the page shows, once, in its order: its caption, what the picture check
    last said of it, and whether it is redrawn (a chart, an infographic) or re-captured."""
    from . import meta as M
    from .views.common import needs_word, picture_needs

    open_on = {(it.get("on") or {}).get("asset"): it for it in rv.get("needs_you") or []
               if (it.get("on") or {}).get("asset")}
    # a problem the check found and the person closed as not important stays a found problem,
    # with their reason; it is not "no problem found" (review RF9C-03)
    waived = {(it.get("on") or {}).get("asset"): it for it in rv.get("closed") or []
              if (it.get("on") or {}).get("asset") and it.get("status") == "not_important"}
    # what the latest change of each picture made of it, by the check's word: "made again" alone
    # read as fixed while the check said the problem was still there (2026-10-05)
    made = {str(w): g for g in rv.get("changed") or [] for w in g.get("where") or [] if str(w).startswith("assets/")}
    # what the person did to a picture after the review (made it again, put their own in): the
    # review's word on the old picture is not shown as the new one's (2026-10-06)
    yours = {(it.get("on") or {}).get("asset"): it["changed_by"] for it in rv.get("closed") or []
             if (it.get("on") or {}).get("asset") and it.get("status") == "changed"
             and isinstance(it.get("changed_by"), dict) and it["changed_by"].get("how")}
    # what the latest review's picture check did: only a picture it looked at can be "no problem
    # found"; off, failed or not recorded is not a pass (review RB32-04)
    check = (rv.get("review") or {}).get("picture_check") or {}
    looked = set(check.get("looked_at") or [])
    could_not = set(check.get("could_not") or [])
    # the bytes each picture had in the version the review read: its word is about those bytes
    # only. A Restore of a picture the person had put in brought the old pass back to bytes no
    # review looked at (R597-03)
    read = next((v.get("files") or {} for v in versions.index(art)
                 if v.get("token") and v.get("token") == (rv.get("review") or {}).get("version")), None)
    # the words each picture was looked at with, and the words it has now: a look's word is about
    # those pixels with those words (RA3C-02: a Restore of old words on the same pixels)
    from core.agents.visual_checker import pictures as looked_pictures, words_of

    looked_words = check.get("words") if isinstance(check.get("words"), dict) else {}
    words_now = {p["path"]: words_of(p) for p in looked_pictures(body, None, art.dir)} if looked_words else {}
    out, seen = [], set()
    for alt, ref in store.shown_image_refs(body):
        if ref in seen:
            continue
        seen.add(ref)
        at = _image_at(body, ref)
        c = _caption_at(body, at) if at else None
        cap = md_text(body[c[0]:c[1]].strip()) if c else ""   # the words it shows (R11D-03: "…/guide(2026))")
        from core.qa_changes import _sha

        left = ((made.get(ref) or {}).get("left") or {}).get(ref)
        mine = (rv.get("by_person") or {}).get(ref)
        mine = mine if (isinstance(mine, dict) and mine.get("sha") and mine["sha"] == _sha(art.path(ref))
                        and _after_review(mine, rv.get("review") or {})) else None
        if mine:
            how = {"made again": "made again by you", "your picture": "your own picture"}.get(mine.get("how"), "changed by you")
            word = (how, "gray", False, "Not checked yet: Review again looks at it.")
        elif ref in yours:
            how = {"made again": "made again by you", "your picture": "your own picture"}.get(yours[ref]["how"], "changed by you")
            word = (how, "gray", False, "Not checked yet: Review again looks at it.")
        elif ref in open_on:
            word = ("the check found a problem", "yellow", False, open_on[ref].get("text"))
        elif (ref in made and left and _sha(art.path(ref)) != left) or (
                ref not in made and read is not None and ref in read and read[ref] != _file_sha256(art.path(ref))) or (
                ref in looked_words and looked_words[ref] != words_now.get(ref)):
            word = ("changed after the review", "gray", False, "Not checked yet: Review again looks at it.")
        elif ref in waived:
            c = waived[ref].get("closed") or {}
            word = ("a problem found, closed as not important", "gray", False,
                    f"{waived[ref].get('text') or ''} Not important · {who(c.get('who'))}"
                    + (f": {c['why']}" if c.get("why") else ""))
        elif ref in made and qm.half_done(made[ref]):
            word = ("made again, the check still sees a problem", "yellow", False, made[ref].get("check_said"))
        elif ref in made and made[ref].get("kept_as_is"):
            # kept as it is on Review (or here): no longer a problem on either tab (2026-10-07)
            word = ("kept as it is", "gray", False,
                    f"The check saw: {made[ref].get('check_said') or 'a problem'} · kept by "
                    f"{who((made[ref].get('kept_as_is') or {}).get('who'))}")
        elif ref in made:
            word = ("made again by the review", "green" if made[ref].get("check") == "fixed" else "gray", False,
                    None if made[ref].get("check") == "fixed" else "Not checked: look at it.")
        elif check.get("error") or ref in could_not:
            word = ("the check could not look at it", "red", False, None)
        elif check.get("off"):
            word = ("not checked: the picture check is off", "gray", True, None)
        elif ref in looked:
            word = ("no problem found", "green", True, None)
        else:
            word = ("not checked", "gray", True, None)
        # Review's decisions on this picture, offered where Pictures shows the problem, the
        # caption request below included (R058-10)
        decide = ({"keep": made[ref]["id"]} if word[0] == "made again, the check still sees a problem"
                  else {"not_important": open_on[ref]["id"]} if word[0] == "the check found a problem" else {})
        if word[0] not in _DECIDED and _caption_from_title(art, ref, alt, cap):
            # a good shot that came with no words: kept, captioned with its page's title, and the
            # person is asked for a caption (the operator, 2026-10-06); not once the person kept
            # the picture as it is or closed its problem as not important (R058-10)
            word = ("no caption of its own yet", "yellow", False,
                    "No words came with this shot: its caption is the page's title. Write a caption in the "
                    "editor, or Redo it.")
        from core.agents.interactive import picture_kind

        made_as = picture_kind(art.dir, ref)        # by its sidecar: a kind change keeps the name
        out.append({"path": ref, "alt": alt, "caption": cap, "check": word, "id": "pic-" + ref.rsplit("/", 1)[-1],
                    "decide": decide,
                    "exists": art.path(ref).is_file(),
                    "kind": "image" if made_as in ("illustration", "photo") else made_as,
                    "needs": needs_word(picture_needs(ref, art.dir)),
                    "url": f"/a/{art.slug}/{ref}?v={M.asset_version(art.dir, ref)}"})
    return out


# ---------------------------------------------------------------------------------------------
# a version, viewed or compared
# ---------------------------------------------------------------------------------------------
def _after_review(act: dict, review: dict) -> bool:
    """Whether the person's act on a picture came after the review: by the versions, the one the
    act made against the one the review read (a review that read the act's own version has its
    say on it, R058-10; new words on the same pixels are a later version, R9F0-04); else, for a
    record from before, by the clock."""
    made, read = act.get("version_n"), review.get("version_n")
    if isinstance(made, int) and isinstance(read, int):
        return made > read
    return str(act.get("at") or "") >= str(review.get("at") or "")


# the Pictures words for a picture whose problem the person decided on Review or Pictures
_DECIDED = ("kept as it is", "a problem found, closed as not important")


def _caption_from_title(art: store.Article, ref: str, alt: str, caption: str) -> bool:
    """Whether the picture is a shot whose caption was taken from its page's title and still
    reads that title: the sidecar's `stand_ins` pair these bytes with that title (or, from before
    them, its record of these bytes says so: its own, or the one kept under `made`, R98D-02). A
    caption the person wrote answers the request, and a Restore of the title brings it back
    (R6FA-04)."""
    import json

    from core.agents.interactive import made_record, noted_stand_ins, stand_ins
    from core.qa_changes import _sha

    try:
        side = json.loads(art.path(ref).with_suffix(".json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        side = None
    sha = _sha(art.path(ref))

    def words(s) -> str:            # as brand.caption_for and retitle_visual write a caption (R898-04)
        return " ".join(re.sub(r"\\?\*", "", str(s or "")).replace("[", "(").replace("]", ")").split())

    def shown(md) -> str:           # a title as its caption line shows it: brand.caption_for's
        # brackets, read by the parser (R11D-03)
        return md_text("*" + str(md or "").replace("[", "(").replace("]", ")") + "*")

    rec = made_record(side, sha, art.dir, ref)
    if rec is None:                 # nothing knows these bytes: nothing to say of them
        return False
    # these bytes with the title that stood in for their caption, while the caption reads it
    # (R518-01: a Re-capture that gave the same bytes keeps the pair)
    # (and what a Revert noted of them, when it put back a sidecar from before them, RA3C-03)
    if any(s == sha and words(shown(t)) == words(caption)
           for s, t in stand_ins(side) + noted_stand_ins(art.dir, ref)):
        return True
    if rec.get("caption_from") != "page_title":
        return False
    # a marker written before `page_title` was kept (R98D-03): the stand-in put the same title in
    # the alt and the caption, so the caption is still it while the two read the same
    return words(caption) == words(shown(rec.get("page_title") or alt))


def _file_sha256(p) -> str | None:
    """A picture's bytes as the versions hash them (core/versions)."""
    import hashlib

    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


def version_view(art: store.Article, n: int) -> dict:
    v = versions.get(art, n)
    text = versions.text_of(art, n)
    _, body = store.split_frontmatter(text)
    return {"v": v, "when": when(v.get("at")), "html": _version_html(art, n, body),
            "compare": versions.compare(art, n)}


def _version_html(art: store.Article, n: int, body: str) -> str:
    """The version's text rendered; its pictures as that version had them, from the kept bytes."""
    html = article_html(art.slug, body)
    files = versions.get(art, n).get("files") or {}
    for ref, sha in files.items():
        if ref.startswith("assets/"):
            html = re.sub(r'src="[^"]*/' + re.escape(ref) + r'(\?[^"]*)?"',
                          f'src="/a/{art.slug}/v/{n}/{html_lib.escape(ref)}"', html)
    return html


# ---------------------------------------------------------------------------------------------
# the article list
# ---------------------------------------------------------------------------------------------
FILTERS = (("all", "All"), ("look", "To look at"), ("accepted", "Accepted"), ("wp", "On WordPress"))


def list_row(art: store.Article, prices, jobs: list[dict]) -> dict:
    """One article in the list: the title, the slug and when it changed, the three facts as a
    dot and a word each (a dash when there is nothing), the cost; a job working on it; a run that
    failed. `look` says whether it waits for the person (the To look at filter)."""
    f = agent_door.facts(art, prices)
    status = f.get("status")
    job = next((j for j in jobs if j.get("slug") == art.slug and j.get("status") in ("queued", "running")), None)
    row = {"slug": art.slug, "title": f.get("title") or art.slug, "updated": when(f.get("updated")),
           "cost": f.get("cost_usd"), "status": status, "updated_at": f.get("updated") or ""}
    if job:
        row["running"] = ("Writing" if job.get("kind") == "article" else str(job.get("topic") or "A job").split(" · ")[0],
                          job.get("step") or "queued")
    if status in ("failed", "aborted") or (status == "running" and not job and not f.get("busy")):
        try:
            err = art.run().get("error")
        except store.CorruptFile:
            err = "the run record does not parse"
        from .privacy import scrub

        row["failed"] = ("Run failed" if status == "failed" else "Run stopped", scrub(str(err or "it did not finish")))
    rv = f.get("review")
    if status != "article":
        row["review"] = None
    elif not rv:
        row["review"] = ("Not reviewed", "gray", True)
    elif rv.get("state") == "unreadable":
        row["review"] = ("Record unreadable", "red", False)
    elif rv.get("valid") is False:
        row["review"] = ("Did not work", "red", False)
    elif rv.get("needs_you"):
        row["review"] = (f"{rv['needs_you']} need{'s' if rv['needs_you'] == 1 else ''} you", "yellow", False)
    else:
        row["review"] = ("Nothing open", "green", True)
    acc = (f.get("accepted") or {}).get("state")
    row["yours"] = {"accepted": ("Accepted", "green", False),
                    "changed_since": ("Changed since accepted", "yellow", False)}.get(acc)
    wp = f.get("wordpress") or {}
    if wp.get("unknown"):
        row["wp"], row["wp_note"] = ("Unknown", "red", False), "check the post"
    elif wp.get("post_id"):
        row["wp"] = post_word(wp)
        row["wp_note"] = "changed since sent" if wp.get("changed_since_sent") else ""
    else:
        row["wp"] = None
    row["look"] = bool(row.get("failed") or (rv and (rv.get("valid") is False or rv.get("needs_you")))
                       or acc == "changed_since" or wp.get("unknown"))
    row["accepted"] = acc == "accepted"
    row["on_wp"] = bool(wp.get("post_id"))
    return row


def post_word(post: dict) -> tuple[str, str, bool]:
    """A WordPress post as the list and the rail name it, from its status as last known: the
    status WordPress gave, never a guess (review R6BD-01)."""
    pid, status = post.get("post_id"), str(post.get("status") or "")
    known = {"publish": ("Live", "orange", False), "draft": ("Draft", "green", True),
             "pending": ("Pending review", "yellow", False), "future": ("Scheduled", "blue", False),
             "private": ("Private", "gray", False), "trash": ("In the trash", "red", False)}
    if status in known:
        word, colour, quiet = known[status]
        return f"{word} #{pid}", colour, quiet
    return (f"Post #{pid} ({status})", "gray", False) if status else (f"Post #{pid}", "gray", True)


def matches(row: dict, q: str) -> bool:
    q = (q or "").strip().lower()
    return not q or q in row["title"].lower() or q in row["slug"]


def list_filter(rows: list[dict], key: str, q: str = "") -> list[dict]:
    keep = {"all": lambda r: True, "look": lambda r: r["look"], "accepted": lambda r: r["accepted"],
            "wp": lambda r: r["on_wp"]}.get(key, lambda r: True)
    return [r for r in rows if keep(r) and matches(r, q)]


# ---------------------------------------------------------------------------------------------
# a run: its stages, what each cost and took
# ---------------------------------------------------------------------------------------------
STAGES = (("Keywords", ("keywords",)), ("Brief", ("brief",)), ("Research", ("research",)),
          ("Writing", ("text",)), ("Pictures", ("charts", "images", "screenshots")),
          ("Review", ("review",)), ("Changes", ("edits",)))


# the pipeline's nodes behind each stage (core/orchestrator.py: build_workflow)
NODES = {"Keywords": ("keyword",), "Brief": ("brief",), "Research": ("deep",), "Writing": ("write",),
         "Pictures": ("visual", "finalize")}


def run_stages(art: store.Article, run: dict, prices, job: dict | None) -> list[dict]:
    """The stages of the run in order: done, running (its live step), failed or stopped where the
    run ended, skipped, or not reached; each with what it cost and how long its calls took. The
    attempt's progress (`_progress.json`, written by each node) says which nodes finished and
    which one was running; a record from before it is read from the run's saved inputs."""
    from core.orchestrator import PROGRESS_FILE

    prog = store.read_json_lenient(art.path(PROGRESS_FILE), {}) or {}
    prog = prog if isinstance(prog, dict) else {}
    finished, at = set(prog.get("done") or []), prog.get("stage")
    current = next((label for label, nodes in NODES.items() if at in nodes), None)
    rv = run.get("review") or {}
    kr = run.get("keyword_research") or {}
    done = {"Keywords": bool(kr), "Brief": bool((run.get("brief") or {}).get("sections")),
            "Research": bool((run.get("research") or {}).get("findings")),
            "Writing": art.path("04_draft.md").is_file() or art.path("article.md").is_file(),
            "Pictures": art.path("article.md").is_file(), "Review": bool(rv.get("reviews")),
            "Changes": bool(rv.get("reviews"))}
    for label, nodes in NODES.items():
        # this attempt's own record decides; without one (a run from before it), what is saved
        by_progress = all(n in finished for n in nodes)
        done[label] = by_progress if prog.get("attempt") else (done[label] or by_progress)
    spent: dict[str, list] = {}
    for ev in usage.events(art.dir):
        if ev.get("op") != "generation" and ev.get("op") != "estimate":
            continue
        cat = usage.category(ev)
        c = usage.cost_of(ev, prices)
        spent.setdefault(cat, []).append((ev.get("ts") or "", c or 0.0))
    status = run.get("status")
    active = bool(job and job.get("status") in ("queued", "running"))
    out, reached_end = [], False
    for label, cats in STAGES:
        evs = [e for c in cats for e in spent.get(c, [])]
        cost = sum(c for _, c in evs)
        stamps = sorted(t for t, _ in evs if t)
        minutes = None
        if len(stamps) > 1:
            try:
                minutes = (datetime.fromisoformat(stamps[-1]) - datetime.fromisoformat(stamps[0])).total_seconds() / 60
            except ValueError:
                minutes = None
        if done[label]:
            state = "done"
        elif label == "Keywords" and (done["Brief"] or current not in (None, "Keywords")):
            state = "skipped"                        # the run went on without it
        elif current == label or (current is None and not reached_end):
            reached_end = True
            state = "running" if active else ("failed" if status == "failed" else "stopped"
                                               if status in ("aborted", "running") else "not reached")
        else:
            state = "not reached"
        out.append({"label": label, "state": state, "cost": cost, "minutes": minutes,
                    "step": (job or {}).get("step") if state == "running" else None})
    return out
