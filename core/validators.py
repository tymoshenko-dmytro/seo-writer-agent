"""Structural article validation.

Cheap, product-agnostic structural checks only. The deep, opinionated quality bar
(brand voice, factual accuracy vs product guardrails) lives in the Editor agent.

`analyze_structure()` measures the things an LLM counts badly — words per section, thin
H3s, link density, invented internal URLs — so the Editor can judge against real numbers
instead of estimating. `validate_brief()` does the same for the outline BEFORE anything
is written, which is where article length is actually decided.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

_HEADING = re.compile(r"^(#{1,4})\s+(.+?)\s*$", re.M)
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MARKER = re.compile(r"<!--.*?-->", re.DOTALL)
from .store import FRONTMATTER as _FM  # noqa: E402  (one frontmatter definition)

THIN_H3_WORDS = 150      # below this an H3 does not earn its heading
H2_INTRO_WORDS = 25      # an H2 must frame itself before its first H3
WALL_WORDS = 300         # a run of prose this long with no subheading in it reads as a wall


def _words(text: str) -> int:
    text = _IMG.sub(" ", _MARKER.sub(" ", text or ""))
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'’%$.\-]*", text))


_TABLE_ROW = re.compile(r"^[ \t]*\|.*$", re.M)


def _prose_words(text: str) -> int:
    """Words the reader actually scrolls through as prose.

    Comparison tables are excluded: a section that is mostly a table is not a wall of text,
    and asking for it to be split or trimmed would attack the most skimmable block on the
    page. Yoast counts table text in the same measure, so its panel can still flag a
    table-heavy section that we deliberately leave alone.
    """
    return _words(_TABLE_ROW.sub(" ", text or ""))


def _is_internal(url: str, hosts) -> bool:
    host = urlparse(url).netloc.lower()
    return any(host == h or host.endswith("." + h) for h in hosts)


def _url_key(url: str) -> str:
    """Compare URLs ignoring scheme and the www. prefix — all four spellings are one page.

    The scheme used to count, and it cost a run: the writer emitted
    `http://example.com/pricing-api` for a page the index holds as
    `https://www.example.com/pricing-api`. The keys differed, so the URL read as
    invented, the Editor demanded its removal, the fixer failed twice and the finding ended
    stuck — over a real page with a sloppy spelling.
    """
    return re.sub(r"^https?://(www\.)?", "", url.rstrip("/").lower())


_PICTURE_FILE = re.compile(r"\]\((assets/[^)\s]+)\)$")




def caption_lines(markdown: str) -> list[tuple[int, int, str]]:
    """The caption lines under pictures: (start, end, the picture). A picture on a line of its own
    and the caption line under it are read as the page's parser reads them (store.picture_alone,
    store.caption_of): a pattern for the alt dropped the protection of a caption under "Setup
    guide [2026]" (R898-03)."""
    from .store import caption_of, picture_alone

    out, at = [], 0
    lines = (markdown or "").split("\n")
    for i, line in enumerate(lines):
        image = line.rstrip(" \t")
        at += len(line) + 1                     # where the next line, the caption's, starts
        m = _PICTURE_FILE.search(image) if image.startswith("![") and i + 1 < len(lines) else None
        if m and picture_alone(image, m.group(1)) and caption_of(image, lines[i + 1]):
            out.append((at, at + len(lines[i + 1]), m.group(1)))
    return out


def in_caption(spans: list[tuple[int, int, str]], pos: int) -> bool:
    return any(a <= pos < b for a, b, _ in spans)


def caption_link_spans(markdown: str) -> list[tuple[int, int, str, str]]:
    """The links in the captions under pictures: (start, end, the picture, the url). The caption
    under a screenshot of our own page links that page so the reader can open what the picture
    shows (operator, 2026-09-28): it is not a repeated link of the prose, which the one-link
    rule unlinked at publish and the readers asked to remove (2026-10-06). The links are the ones
    the page's parser shows, where it found them (markdown_render.link_spans): never text behind an
    escaped bracket or in a code span, whatever the address holds (R83D-02 … R11D-02). (The
    one-link rule leaves every caption line as it is: caption_lines.)"""
    from .tools.markdown_render import link_spans

    out = []
    for start, end, asset in caption_lines(markdown):
        for a, b, url in link_spans((markdown or "")[start:end]):
            out.append((start + a, start + b, asset, url))
    return out


def dedupe_links(markdown: str) -> tuple[str, list[str]]:
    """Link every URL at most once (SEO rule): keep the first, unlink later mentions.

    Applied right after the writer so `article.md` is the canonical text — otherwise the
    editor reviews and the Studio previews a version with duplicate links that the publisher
    silently strips, and QA burns rounds on a defect that fixes itself at publish time.
    Idempotent, so the publisher can still run it as a last-resort net.
    """
    seen: set[str] = set()
    removed: list[str] = []
    captions = caption_lines(markdown)

    def sub(m):
        if in_caption(captions, m.start()):
            # a picture's caption keeps its links, and they do not count; nothing in its line
            # is touched (unlinking a text match there broke the real link around it)
            return m.group(0)
        key = _url_key(m.group(2))
        if key in seen:
            removed.append(m.group(2))
            return m.group(1)  # keep the words, drop the repeated link
        seen.add(key)
        return m.group(0)

    return _LINK.sub(sub, markdown or ""), removed



def canonicalise_internal_links(markdown: str, known_urls) -> tuple[str, list[tuple[str, str]]]:
    """Rewrite internal links that name a REAL page with the wrong scheme or host spelling.

    Sits with `dedupe_links` and `strip_horizontal_rules`: deterministic, so it belongs in code
    rather than in a required_fix. Only URLs that match a sitemap target once scheme and `www.`
    are ignored get rewritten, and they are rewritten to that target verbatim — a URL with no
    match is left exactly as written, so a genuinely invented link still reaches the Editor.
    """
    canonical = {}
    for u in known_urls or []:
        canonical.setdefault(_url_key(u), u)
    if not canonical:
        return markdown, []
    changed: list[tuple[str, str]] = []

    def sub(m):
        url = m.group(2)
        good = canonical.get(_url_key(url))
        if not good or good == url:
            return m.group(0)
        changed.append((url, good))
        return f"[{m.group(1)}]({good})"

    return _LINK.sub(sub, markdown or ""), changed


def selling_surface(url: str | None, hosts=(), doc_paths=()) -> bool:
    """Is this page a competitor's SELLING SURFACE? One definition for the structure report, which
    lists such links, and the claim verifier, which must never cite one.

    A document on a competitor's domain is a legitimate citation — their docs, an industry
    survey they host, a PDF (a docs/support/help subdomain, or a path in `doc_paths`). Their
    product, pricing, solutions, integration or "X vs Y" page is not: it is the page we are
    competing against, and linking it is the one thing an article can do that actively helps them.
    """
    if not hosts or not url:
        return False
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower().split(":")[0].removeprefix("www.")
    if not any(host == h or host.endswith("." + h) for h in (x.lower().lstrip(".") for x in hosts if x)):
        return False
    # the PATH decides, not the query string: "?from=/research/" must not turn a pricing page into
    # a document
    target = (parsed.path or "/").lower()
    # a knowledge base is their help centre: knowledge.interprefy.com was listed as a selling
    # surface, so two editors asked to remove the link while the third asked to add it (trial 1)
    return not (any(d.lower() in target for d in doc_paths or ())
                or host.split(".")[0] in ("docs", "support", "help", "knowledge", "kb", "developer",
                                          "developers"))


def competitor_links(markdown: str, hosts=(), doc_paths=()) -> list[tuple[str, str]]:
    """External links that hand weight to a competitor's selling surface. Returns [(anchor, url)] to fix."""
    return [(a, u) for a, u in _LINK.findall(markdown or "") if selling_surface(u, hosts, doc_paths)]


_EM_SPACED = re.compile(r"\s+—\s+")
_EM_TIGHT = re.compile(r"(?<=[A-Za-z0-9])—(?=[A-Za-z0-9])")


def normalize_em_dashes(markdown: str) -> tuple[str, int]:
    """Replace em dashes used as sentence punctuation with a comma. Deterministic, so it
    belongs in code: the ban is absolute (SEO hard rule, stated in every prompt), the writer
    still emits them (33 in one measured article), and "replace every em dash" cannot be
    expressed as one anchored string swap — it stuck as an unfixable Critical in the field
    test. EN dashes in ranges ("$0.05–$0.10") and hyphens are untouched; the frontmatter block
    is protected like strip_horizontal_rules does.

    TABLE LINES ARE EXEMPT: a lone em dash in a cell is the standard "no data" placeholder,
    not sentence punctuation — the first version rewrote `| — |` into `|, |`, manufacturing
    the broken-table Critical the QA loop then correctly refused to ship (run 3). The rule
    bans em dashes AS PUNCTUATION; a placeholder is not punctuation.
    """
    fm = _FM.match(markdown or "")
    head, body = (fm.group(0), markdown[fm.end():]) if fm else ("", markdown or "")
    out_lines, n = [], 0
    for line in body.split("\n"):
        if line.lstrip().startswith("|"):
            # table row: a LONE em dash in a cell is the "no data" placeholder and stays;
            # an em dash inside a cell's prose is punctuation and is normalized like anywhere
            # else (the editor flagged exactly that boundary case in the field)
            cells = line.split("|")
            fixed_cells = []
            for cell in cells:
                if cell.strip() in ("—", ""):
                    fixed_cells.append(cell)
                    continue
                cell, n1 = _EM_SPACED.subn(", ", cell)
                cell, n2 = _EM_TIGHT.subn(", ", cell)
                n += n1 + n2
                fixed_cells.append(cell)
            out_lines.append("|".join(fixed_cells))
            continue
        line, n1 = _EM_SPACED.subn(", ", line)
        line, n2 = _EM_TIGHT.subn(", ", line)
        n += n1 + n2
        out_lines.append(line)
    return head + "\n".join(out_lines), n


_HR = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$\n?", re.M)


def strip_horizontal_rules(markdown: str) -> tuple[str, int]:
    """Remove `---` rules. Banned in every prompt and still emitted; the publisher strips them,
    but not before the Editor spends a required_fix asking for it. Deterministic, so it belongs
    in code — applied right after the writer, like dedupe_links, so every consumer sees the same
    text. The frontmatter block is protected by being matched only outside it.
    """
    fm = _FM.match(markdown or "")
    head, body = (fm.group(0), markdown[fm.end():]) if fm else ("", markdown or "")
    cleaned, n = _HR.subn("", body)
    return head + cleaned, n



# Sentences that talk about how the article was MADE. They read as candid, which is why the
# Editor waves them through — one shipped inside a customer-facing FAQ answer: "The researched
# facts used to write this article do not include a sourced per-word rate, so a specific figure
# cannot be responsibly stated here." A reader has no idea what "the researched facts" are, and
# saying a number cannot be given is worse than omitting the question. Detected in code because
# `no_internal_meta` passed this article: the phrasing is polite and locally coherent, and a
# judge reading for tone does not catch it.
_META_COMMENTARY = re.compile(r"""(?ix)
      (the \s+)? research(ed)? \s+ (facts?|corpus|material|sources?) \b
    | \b(this|the) \s+ article \s+ (does \s+ not|cannot|can't|will \s+ not) \b
    | \bcannot \s+ be \s+ (responsibly \s+)? (stated|given|provided|confirmed) \s+ here \b
    | \bno \s+ sourced \s+ (figure|number|rate|data) \b
    | \b(our|the) \s+ (word \s+ budget|target \s+ length) \b
    | \bas \s+ an? \s+ (AI|language \s+ model)\b
    | \b(per|according \s+ to) \s+ (the|our) \s+ (brief|prompt)\b
""")
# This blocks publication, so it must prefer a miss to a false alarm. Two patterns were dropped
# after they fired on a correct article within one run: a bare `language model` matched "a
# language model generates the spoken script" — ordinary subject matter for a publication that
# writes about AI — and `the brief` matched "The Brief", the name of TIME's newsletter. Only
# self-referential phrasing counts now.

_SENT = re.compile(r"[^.!?\n]{15,600}[.!?]")


def meta_commentary(markdown: str) -> list[str]:
    """Sentences that describe the writing process instead of the subject."""
    out = []
    for m in _SENT.finditer(markdown or ""):
        sent = " ".join(m.group(0).split())
        # The FAQ ships as JSON in the frontmatter, so a sentence lifted from it carries the
        # surrounding punctuation and key name. Strip those so the report quotes prose.
        sent = re.sub(r'^[\s",:\[\{]*(?:"?\w+"?\s*:\s*")?', "", sent).strip()
        if sent and _META_COMMENTARY.search(sent):
            out.append(sent[:180])
    return list(dict.fromkeys(out))


def _shows_page(article_dir, asset: str, url: str) -> bool:
    """Whether the picture at `asset` is a screenshot of the page `url`: its kind and its page by
    what made its bytes, when the article's folder is known (R058-06, R9F0-05: a link edited under
    a shot of another page is not proved by it); a shot whose page nothing records, and without
    the folder, by its file's name, as before."""
    if not article_dir:
        return asset.split("/")[-1].startswith("screenshot")
    from .agents.fix_routing import same_page
    from .agents.interactive import picture_kind
    from .cleanup import screenshot_urls

    if picture_kind(article_dir, asset) != "screenshot":
        return False
    shown = screenshot_urls(Path(article_dir), f"]({asset})").get(asset)      # this one picture's page
    return not shown or same_page(url, shown)


def analyze_structure(markdown: str, target_words: int = 0, allowed_urls=None,
                      internal_hosts=None, words_per_link: int | None = None,
                      sources_available: int = 0, words_per_citation: int = 250,
                      competitor_hosts=(), competitor_doc_paths=(), article_dir=None,
                      own_names=()) -> dict:
    """Measure everything the SEO rules are stated in terms of. Pure counting, no judgement.
    `internal_hosts` defaults to the project's own domains (core/brand.py); `article_dir` is where
    the pictures are, so a picture's kind is read from its sidecar (else from its file's name)."""
    from .brand import default as _brand

    brand = _brand()
    if internal_hosts is None:
        internal_hosts = brand.domains
    if words_per_link is None:
        words_per_link = brand.words_per_link
    body = _FM.sub("", markdown or "")
    heads = list(_HEADING.finditer(body))
    sections = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
        chunk = body[m.end():end]
        sections.append({"level": len(m.group(1)), "title": m.group(2).strip(),
                         "words": _words(chunk), "prose_words": _prose_words(chunk)})

    # Which H3s sit inside a roundup section. One H3 per reviewed product is correct there —
    # that exemption already exists for the H3 COUNT budget, and it has to apply to the
    # thickness rule too, or the system contradicts itself: a 120-word product entry gets
    # demanded AND flagged. The specs live in the comparison table, not in the prose, so a short
    # entry is normal. Observed: the Editor asked to add 90-150 words to three product H3s while
    # also asking to cut 20 to get under the length ceiling — unsatisfiable, and the fix loop
    # oscillated 74 -> 79 -> 76 without converging.
    roundup_h3 = set()
    roundups: list[tuple[str, list[int]]] = []
    cur_h2, kids = None, []
    for i, sec in enumerate(sections + [{"level": 2, "title": "", "words": 0}]):
        if sec["level"] == 2:
            if cur_h2 and _ROUNDUP.search(cur_h2) and len(kids) >= 3:
                roundup_h3.update(kids)
                roundups.append((cur_h2, list(kids)))
            cur_h2, kids = sec["title"], []
        elif sec["level"] == 3 and cur_h2:
            kids.append(i)

    thin_h3, h2_no_intro, h2_with_h3, question_h3 = [], [], 0, []
    for i, s in enumerate(sections):
        if s["level"] == 3 and s["words"] < THIN_H3_WORDS and i not in roundup_h3:
            thin_h3.append((s["title"], s["words"]))
        # a question-shaped heading is how an FAQ leaks into the body, which the FAQ already
        # ships as structured data
        if s["level"] in (2, 3) and s["title"].rstrip().endswith("?"):
            question_h3.append(s["title"])
        if s["level"] == 2:
            nxt = sections[i + 1] if i + 1 < len(sections) else None
            if nxt and nxt["level"] == 3:
                h2_with_h3 += 1
                if s["words"] < H2_INTRO_WORDS:
                    h2_no_intro.append(s["title"])

    # Walls of text: a stretch of prose longer than WALL_WORDS with no subheading inside it.
    # A section's word count already runs to the NEXT heading of any level, so `prose_words`
    # over the threshold IS "no subheading in N words" — the same thing Yoast measures as
    # subheading distribution. Counted for every level, because a 560-word product entry
    # under an H3 is as much of a wall as one under an H2.
    long_sections = [(s["title"], s["prose_words"]) for s in sections
                     if s["prose_words"] > WALL_WORDS]
    if heads and _prose_words(body[:heads[0].start()]) > WALL_WORDS:
        long_sections.insert(0, ("(text before the first heading)",
                                 _prose_words(body[:heads[0].start()])))

    links = _LINK.findall(body)
    internal = [(a, u) for a, u in links if _is_internal(u, internal_hosts)]
    external = [(a, u) for a, u in links if not _is_internal(u, internal_hosts)]
    captions = caption_lines(body)
    seen, dupes = set(), []
    for _, u in [(m.group(1), m.group(2)) for m in _LINK.finditer(body) if not in_caption(captions, m.start())]:
        key = _url_key(u)
        (dupes.append(u) if key in seen else seen.add(key))

    allowed = {_url_key(u) for u in (allowed_urls or [])}
    # our app/docs/API hosts are always legitimate targets, index or not
    # the page a screenshot shows is a page that exists: its caption's link is not invented (the
    # docs quickstart a new screenshot showed is not in the sitemap's index, 2026-10-06). Only a
    # screenshot's, and only the page it shows: a link under another kind of picture, or edited to
    # another page under a shot, is a link like any other (R058-06, R9F0-05); what the picture is
    # comes from its sidecar, since a picture made again as another kind keeps its name
    shot_pages = {_url_key(u) for _, _, asset, u in caption_link_spans(body)
                  if _shows_page(article_dir, asset, u)}
    invented = ([u for _, u in internal if _url_key(u) not in allowed and _url_key(u) not in shot_pages
                 and not brand.is_app_page(u)] if allowed else [])

    # where our own product's entry stands in each roundup: the first or the second is the rule
    # (testers, 2026-10-20: "our product should be first or second")
    names = [n.strip().lower() for n in own_names if n and n.strip()]
    own_places = []
    for h2, idxs in roundups if names else ():
        place = next((k for k, i in enumerate(idxs, 1)
                      if any(re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", sections[i]["title"].lower())
                             for n in names)), None)
        own_places.append({"section": h2, "place": place, "of": len(idxs)})

    total = _words(body)
    want_internal = max(3, round((target_words or total) / max(words_per_link, 1)))
    return {
        "words": total,
        "target_words": target_words,
        "overshoot_pct": round((total - target_words) / target_words * 100, 1) if target_words else 0.0,
        "h2": sum(1 for s in sections if s["level"] == 2),
        "h3": sum(1 for s in sections if s["level"] == 3),
        "h2_with_h3": h2_with_h3,
        "thin_h3": thin_h3,
        "roundup_h3": len(roundup_h3),
        "own_places": own_places,
        "question_headings": question_h3,
        "long_sections": long_sections,
        "h2_without_intro": h2_no_intro,
        "internal_links": internal,
        "external_links": external,
        "internal_expected": want_internal,
        "citations_expected": (min(sources_available, max(4, round((target_words or total) / words_per_citation)))
                               if sources_available else 0),
        "sources_available": sources_available,
        "product_page_links": [u for _, u in internal if brand.is_product_page(u)],
        "duplicate_urls": dupes,
        "invented_internal": invented,
        "meta_commentary": meta_commentary(markdown),
        "competitor_links": competitor_links(body, competitor_hosts, competitor_doc_paths),
        "sections": sections,
    }


def format_structure_report(r: dict) -> str:
    """The measured numbers, phrased for a prompt."""
    L = [f"- Length: {r['words']} words vs target {r['target_words'] or 'n/a'}"
         + (f" ({r['overshoot_pct']:+.0f}%)" if r["target_words"] else ""),
         f"- Headings: {r['h2']} H2, {r['h3']} H3 — {r['h2_with_h3']} of the H2s carry H3s",
         f"- Internal links: {len(r['internal_links'])} (expected ~{r['internal_expected']}), "
         f"of which {len(r['product_page_links'])} to product pages",
         f"- External links / citations: {len(r['external_links'])}"
         + (f" (expected ~{r['citations_expected']} from the {r['sources_available']} sources the "
            f"research gathered — well under that means the piece is unsupported)"
            if r.get("citations_expected") else "")]
    def _cap(items, n=12):
        extra = len(items) - n
        return "; ".join(items[:n]) + (f" (+{extra} more)" if extra > 0 else "")

    for o in r.get("own_places") or []:
        if o["place"] is None:
            L.append(f"- OUR ENTRY: not among the {o['of']} entries of the roundup '{o['section']}'.")
        else:
            L.append(f"- OUR ENTRY: #{o['place']} of {o['of']} in the roundup '{o['section']}'"
                     + (" — the first or the second place is the rule" if o["place"] > 2 else "") + ".")
    if r.get("roundup_h3"):
        L.append(f"- {r['roundup_h3']} H3(s) are per-product entries in a roundup section — one "
                 f"per reviewed product is correct, and a short entry is normal there because "
                 f"the comparison table carries the specs. They are NOT counted as thin and must "
                 f"not be flagged.")
    if r["thin_h3"]:
        L.append(f"- THIN H3s ({len(r['thin_h3'])} of {r['h3']}, under {THIN_H3_WORDS} words, "
                 f"roundup entries excluded — fold into prose): "
                 + _cap([f"'{t}' {w}w" for t, w in r["thin_h3"]]))
    # Long stretches of prose (`long_sections`, over WALL_WORDS) are still measured but no longer
    # reported as a defect: the operator does not require sections under 300 words (2026-09-28),
    # and the "walls of text" findings were a large share of every trial's fix work.
    if r.get("meta_commentary"):
        L.append(f"- PROCESS COMMENTARY in customer-facing text ({len(r['meta_commentary'])}) — "
                 f"these sentences describe how the article was written, which the reader has no "
                 f"context for. Each one must be rewritten to state the subject or cut: "
                 + _cap([f"'{t}'" for t in r["meta_commentary"]], 4))
    if r.get("question_headings"):
        L.append(f"- QUESTION-SHAPED headings ({len(r['question_headings'])}) — the FAQ ships as "
                 f"structured data, so these duplicate it: " + _cap([f"'{t}'" for t in r["question_headings"]]))
    if r["h2_without_intro"]:
        L.append(f"- H2s that jump straight into an H3 with no framing prose "
                 f"({len(r['h2_without_intro'])}): " + _cap([f"'{t}'" for t in r["h2_without_intro"]]))
    if r.get("competitor_links"):
        L.append(f"- LINKS TO A COMPETITOR'S SELLING SURFACE ({len(r['competitor_links'])}) — "
                 f"their product, pricing, solutions or comparison page, i.e. the page we are "
                 f"trying to outrank. Each one must go: keep the fact and attribute it in prose "
                 f"('<Competitor>'s pricing page lists …'), or cite a document instead (their docs, a "
                 f"PDF, a survey report). Never a bare removal that leaves a number uncited: "
                 + _cap([f"'{a}' -> {u}" for a, u in r["competitor_links"]], 8))
    if r["duplicate_urls"]:
        L.append(f"- URLs linked more than once: {', '.join(sorted(set(r['duplicate_urls']))[:8])}")
    if r["invented_internal"]:
        L.append(f"- INVENTED internal URLs (not in the inventory — Critical): "
                 f"{', '.join(r['invented_internal'][:8])}")
    return "\n".join(L)


_ROUNDUP = re.compile(r"\b(best|top|round-?up|reviewed|compared|comparison|alternatives|"
                      r"tools|apps|providers|vendors|platforms|software)\b", re.I)


def _roundup_sections(sections) -> set:
    """Sections where one H3 per reviewed product is legitimate (a "best X" roundup).

    Identified by an H2 that both reads like a roundup AND carries several H3s — a single
    H3 under a "best practices" heading is not a roundup.
    """
    return {id(s) for s in sections
            if _ROUNDUP.search(str(s.get("h2", ""))) and len(s.get("h3") or []) >= 3}


def _norm_q(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", str(t or "").lower()).strip()


def validate_meta(meta: dict, primary_keyword: str = "", h1: str = "",
                  title_min: int = 50, title_max: int = 70,
                  desc_min: int = 130, desc_max: int = 150) -> list[str]:
    """Check the meta tags against the SEO team's spec, in code.

    A model asked for "50-70 characters" writes 44 or 83 and states that it wrote 60, so the
    lengths are measured here and sent back for repair rather than trusted.
    """
    problems: list[str] = []
    title = (meta.get("meta_title") or "").strip()
    desc = (meta.get("meta_description") or "").strip()
    kw = (primary_keyword or "").strip().lower()

    if not title:
        problems.append("meta_title is missing.")
    else:
        if not (title_min <= len(title) <= title_max):
            problems.append(f"meta_title is {len(title)} characters — must be "
                            f"{title_min}-{title_max}. Rewrite it to fit, do not pad it.")
        if kw and kw not in title.lower():
            problems.append(f"meta_title does not contain the primary keyword '{primary_keyword}'.")
        if h1 and title.strip().lower() == h1.strip().lower():
            problems.append("meta_title is identical to the H1 — it must be worded differently "
                            "while targeting the same keyword.")
    if not desc:
        problems.append("meta_description is missing.")
    else:
        if not (desc_min <= len(desc) <= desc_max):
            problems.append(f"meta_description is {len(desc)} characters — must be "
                            f"{desc_min}-{desc_max}.")
        if kw and kw not in desc.lower():
            problems.append(f"meta_description does not contain the primary keyword "
                            f"'{primary_keyword}'.")
    return problems


_FAQ_HEADING = re.compile(r"^\s*(faqs?\b|frequently asked|common questions|questions\s*&|q\s*&\s*a)", re.I)


def normalize_brief_structure(brief: dict) -> list[str]:
    """Deterministically remove the two FAQ-in-the-body defects. Mutates `brief`.

    Both rules are absolute — the FAQ ships as structured data, so it is never a body
    heading — which makes them a job for code rather than another advisory repair pass. The
    model was asked not to do this and did it anyway, and losing a round to it costs a
    research-and-writing cycle.

    Nothing is thrown away: a removed FAQ heading's questions are promoted into the `faq`
    field, where they were supposed to go, so the coverage survives as structured data.
    """
    sections = brief.get("sections") or []
    if not sections:
        return []
    changes: list[str] = []
    faq = list(brief.get("faq") or [])
    roundup = _roundup_sections(sections)
    have = {_norm_q(q) for q in faq}

    def promote(q: str):
        q = str(q or "").strip()
        if q and _norm_q(q) not in have:
            have.add(_norm_q(q))
            faq.append(q if q.endswith("?") else q + "?")

    # 1. an FAQ section in the body
    keep, freed = [], 0
    for sec in sections:
        if _FAQ_HEADING.search(str(sec.get("h2", ""))):
            for q in sec.get("h3") or []:
                promote(q)
            freed += sec.get("words") if isinstance(sec.get("words"), int) else 0
            changes.append(f"removed body section '{sec.get('h2')}' — the FAQ ships as "
                           f"structured data; its questions moved to `faq`")
            continue
        keep.append(sec)
    if not keep:                       # pathological: the FAQ was the whole outline
        return []

    # 2. question-shaped H3s (outside a roundup, where H3s are product names)
    for sec in keep:
        if id(sec) in roundup:
            continue
        h3s = sec.get("h3") or []
        kept_h3 = [h for h in h3s if not str(h).strip().endswith("?")]
        for h in h3s:
            if str(h).strip().endswith("?"):
                promote(h)
                changes.append(f"removed question H3 '{h}' under '{sec.get('h2')}' — moved to `faq`; "
                               f"the section's prose covers the ground")
        if len(kept_h3) != len(h3s):
            sec["h3"] = kept_h3

    # give the freed budget back so the section budgets still sum to target_words
    if freed and keep:
        budgeted = [sec for sec in keep if isinstance(sec.get("words"), int)]
        if budgeted:
            share, rest = divmod(freed, len(budgeted))
            for i, sec in enumerate(budgeted):
                sec["words"] += share + (1 if i < rest else 0)
            changes.append(f"redistributed {freed} words from the removed FAQ section across "
                           f"{len(budgeted)} remaining sections")

    brief["sections"] = keep
    brief["faq"] = faq
    return changes


def validate_brief(brief: dict, max_h2: int = 8, max_h3_per_h2: int = 3,
                   primary_keyword: str = "", mandatory: list[str] | None = None) -> list[str]:
    """Catch an over-built outline BEFORE research and writing spend money on it."""
    problems: list[str] = []
    sections = brief.get("sections") or []
    target = brief.get("target_words") or 0
    roundup = _roundup_sections(sections)

    # The title becomes the article's H1, and the keyword report blocks publication when the H1
    # lacks the primary keyword. Check it here, with the report's own phrase match, so the brief
    # and the gate can never disagree about what "contains the keyword" means.
    title = str(brief.get("title") or "")
    # every SEO-mandatory keyword needs a home: assigned to a section, or carried by the title
    if mandatory:
        from .keyword_report import count_phrase, tokens
        placed = [tokens(str(k)) for s in (brief.get("sections") or []) for k in (s.get("keywords") or [])]
        for kw in mandatory:
            need = tokens(str(kw))
            if need and not count_phrase(tokens(title), need) and need not in placed:
                problems.append(f"the SEO-mandatory keyword '{kw}' is not assigned to any section — "
                                f"add it to the keywords of the section where it fits.")
    if primary_keyword and title:
        from .keyword_report import count_phrase, tokens
        if not count_phrase(tokens(title), tokens(primary_keyword)):
            problems.append(
                f"the title (the article's H1) does not contain the primary keyword "
                f"'{primary_keyword}' as a phrase — the keyword report blocks publication on "
                f"this. Rework the title so the phrase appears intact.")

    if len(sections) > max_h2:
        problems.append(
            f"{len(sections)} H2 sections — the maximum is {max_h2}. Merge the overlapping ones.")

    # H3 budget. The SEO team's rule is that H3s DILUTE the weight the H2s pass to the main
    # keywords, so they are rationed: at most one per H2 across the article, and the roundup
    # section (one H3 per reviewed product) is the single exemption.
    non_roundup = [s for s in sections if id(s) not in roundup]
    h3_outside = sum(len(s.get("h3") or []) for s in non_roundup)
    if non_roundup and h3_outside > len(non_roundup):
        problems.append(
            f"{h3_outside} H3s across {len(non_roundup)} non-roundup sections — the budget is "
            f"at most one per section (most should have none). H3s dilute the weight the H2s "
            f"pass to the primary keyword. Fold the thin ones into their parent's prose.")
    for s in non_roundup:
        if len(s.get("h3") or []) > max_h3_per_h2:
            problems.append(f"section '{s.get('h2','?')}' has {len(s['h3'])} H3s "
                            f"(max {max_h3_per_h2}) — fold the thin ones into its prose.")

    # FAQ questions promoted to H3s: the FAQ ships as structured data, so a question that also
    # appears as a heading spends body budget on ground the FAQ already covers AND adds an H3
    # that earns nothing.
    faq_norm = {_norm_q(q) for q in (brief.get("faq") or []) if q}
    for s in sections:
        for h3 in s.get("h3") or []:
            if _norm_q(h3) in faq_norm:
                problems.append(f"H3 '{h3}' under '{s.get('h2','?')}' repeats an FAQ question — "
                                f"the FAQ ships as structured data. Remove the heading; the "
                                f"section's prose should cover the ground instead.")
            elif str(h3).strip().endswith("?") and id(s) not in roundup:
                problems.append(f"H3 '{h3}' under '{s.get('h2','?')}' is a question heading — "
                                f"question-shaped H3s are how an FAQ leaks into the body. "
                                f"Rewrite it as a statement or fold it into the prose.")

    for s in sections:
        if re.search(r"^\s*(faq|frequently asked)", str(s.get("h2", "")), re.I):
            problems.append(f"section '{s.get('h2')}' is an FAQ — the FAQ ships as structured "
                            f"data from the `faq` field and must never be a body section. "
                            f"Remove it and redistribute its budget.")

    budgets = [s.get("words") for s in sections if isinstance(s.get("words"), int)]
    if len(budgets) < len(sections):
        problems.append("every section needs an integer `words` budget.")
    elif target and abs(sum(budgets) - target) > target * 0.05:
        problems.append(f"section budgets sum to {sum(budgets)} but target_words is {target} — "
                        f"they must match within 5%.")
    for s in sections:
        if isinstance(s.get("words"), int) and s["words"] < 150:
            problems.append(f"section '{s.get('h2','?')}' is budgeted at {s['words']} words — "
                            f"too small to be its own section; merge it.")

    # near-duplicate section titles: the taxonomy-twice failure, caught mechanically.
    # The primary keyword's own words are excluded: headings are SUPPOSED to carry the target
    # keyword, so counting it made "what to look for in a business AI voice translator" and
    # "the best AI voice translator apps for business" look like the same section.
    stop = {"the", "a", "an", "and", "or", "of", "to", "in", "for", "on", "what", "how", "why",
            "when", "your", "you", "is", "are", "it", "its", "with", "from", "by", "at", "as"}
    stop |= {w for w in re.findall(r"[a-z]+", (primary_keyword or "").lower()) if len(w) > 2}

    def toks(t):
        return {w for w in re.findall(r"[a-z]+", (t or "").lower()) if w not in stop and len(w) > 2}

    for i, a in enumerate(sections):
        for b in sections[i + 1:]:
            ta, tb = toks(a.get("h2")), toks(b.get("h2"))
            if ta and tb and len(ta & tb) / min(len(ta), len(tb)) >= 0.6:
                problems.append(f"sections '{a.get('h2')}' and '{b.get('h2')}' overlap heavily — "
                                f"merge them into one.")
    return problems


# ---------------------------------------------------------------------------
# The review's attention routers: pure functions, no model calls.
# ---------------------------------------------------------------------------

_ENTITY = re.compile(r"\b((?:[A-Z][A-Za-z0-9]*|[a-z]+\.[a-z]{2,3})(?:\s+[A-Z][A-Za-z0-9]*)*)\b")
_INTERROGATIVE = {"what", "which", "who", "whom", "whose", "when", "where", "why", "how", "are",
                  "is", "do", "does", "can", "could", "will", "would", "should", "the", "a"}


def gap_entities(research: dict | None, article: str) -> list[str]:
    """Entities the research explicitly could NOT verify, that the article still talks about.

    Gap-side text = the `gaps` list plus every finding's `question` (an unanswered research
    question is a gap even when the finding carries facts about something else — the TIME
    lesson). Subtraction is by EXACT SURFACE FORM, deliberately case-sensitive: case-folding
    removed "TIME" because "time" appears in the facts as a common noun eight times, while
    "ElevenLabs" is attested verbatim in facts and is correctly excluded. Verified by
    simulation against the audio-ai-news corpus; keep that check green (§7 of the design).
    """
    r = research or {}
    gap_text = "\n".join(str(g) for g in (r.get("gaps") or []))
    fact_text_parts: list[str] = []
    for f in (r.get("findings") or []):
        gap_text += "\n" + str(f.get("question") or "")
        for fact in (f.get("facts") or []):
            fact_text_parts.append(str(fact.get("claim") or "") + " " + str(fact.get("value") or ""))
    for s_ in (r.get("key_statistics") or []):
        fact_text_parts.append(str(s_.get("stat") or "") + " " + str(s_.get("value") or ""))
    fact_text = "\n".join(fact_text_parts)

    attested = set(_ENTITY.findall(fact_text))
    out: list[str] = []
    for ent in _ENTITY.findall(gap_text):
        e = ent.strip()
        if not e or e.lower() in _INTERROGATIVE or len(e) < 3:
            continue
        if e in attested:                      # exact surface form — see docstring
            continue
        # a fragment of an attested entity ("Eleven" ⊂ "ElevenLabs") is that entity, not a gap
        if any(e in a for a in attested if a != e):
            continue
        if not re.search(r"(?<![A-Za-z0-9])" + re.escape(e) + r"(?![A-Za-z0-9])", article or ""):
            continue
        if e not in out:
            out.append(e)
    return out[:10]


_MD_LINK_MASK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_NUMERAL = re.compile(r"\b\d[\d,.]*%?\b")
_YEAR_ONLY = re.compile(r"^(19|20)\d\d$")


def uncited_numeral_paragraphs(markdown: str, attested_numbers: set[str] | None = None,
                               cap: int = 10) -> list[str]:
    """Paragraphs whose numerals carry no inline link — candidates, never verdicts.

    Links are masked BEFORE matching (the raw text of a markdown URL contains dots that
    fragment naive sentence splits — the `_SENT` trap), and numerals attested in the product
    facts or the benchmark block are masked too: those need no external citation by the
    editor's own rules, and listing them would hand the reads a view that contradicts the
    checklist (the "two views of one fact" bug shape). Paragraphs whose numbers are NOT in the
    supplied attested set sort first; capped so the hint block stays a hint.
    """
    attested = attested_numbers or set()
    body = _FM.sub("", markdown or "")
    body = _IMG.sub(" ", body)
    out: list[tuple[int, str]] = []
    for para in re.split(r"\n\s*\n", body):
        p = para.strip()
        if not p or p.startswith("#"):
            continue
        if "](http" in p:                     # paragraph carries an inline citation — not a candidate
            continue
        text = _MD_LINK_MASK.sub(r"\1", p)    # unwrap any non-http links so URLs never count as numbers
        nums = [n.strip(",.").rstrip("%") for n in _NUMERAL.findall(text)]
        nums = [n for n in nums if not _YEAR_ONLY.match(n) and n not in attested]
        if not nums:
            continue
        out.append((-len(nums), " ".join(text.split())[:180]))
    out.sort()
    return [p for _, p in out[:cap]]


def attested_numbers_from(product_facts: str, sources_text: str = "") -> set[str]:
    """Numbers that need no external citation: our own product facts, and what the project's own
    research sources give this article (core/research_sources.py)."""
    text = (product_facts or "") + "\n" + (sources_text or "")
    return {n.strip(",.").rstrip("%") for n in _NUMERAL.findall(text)}


def validate_article(state) -> tuple[bool, list[str]]:
    problems: list[str] = []

    if not state.draft_markdown.strip():
        problems.append("empty draft")
    if "# " not in state.draft_markdown:
        problems.append("no H1 title")
    if not state.frontmatter.get("title"):
        problems.append("missing frontmatter title")

    # research facts carry source_url by schema; flag any that slipped through uncited
    research = state.research or {}
    for s in research.get("key_statistics", []):
        if not s.get("source_url"):
            problems.append(f"uncited key statistic: {str(s.get('stat', '?'))[:60]}")
    for finding in research.get("findings", []):
        for fact in finding.get("facts", []) or []:
            if not fact.get("source_url"):
                problems.append(f"uncited fact: {str(fact.get('claim', '?'))[:60]}")

    # if we gathered facts but the draft cites nothing, the writer ignored the research
    has_facts = bool(research.get("key_statistics") or research.get("findings"))
    if has_facts and "](http" not in state.draft_markdown:
        problems.append("research facts were available but the draft has no inline citations")

    return (len(problems) == 0, problems)
