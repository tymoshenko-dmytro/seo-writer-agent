"""Competitor page audit — what a ranking article is actually MADE of.

The SEO team's prototype workflow is: pick three competitors, then "look at the content and
note the length, how many images and which kinds, how many tables and which kinds, video,
FAQ, the H* structure and the meta tags" — and mark the template junk so we don't copy it.
That is a page teardown, not an outline scrape, so this module replaces the old regex
`_fetch_outline()` in the keyword researcher.

Deliberately dependency-free: stdlib `html.parser` gives a real tokeniser (regex over HTML
mis-attributes nested content), and adding bs4/lxml would mean rebuilding the container image.

Every counted element is attributed to the HEADING IT SITS UNDER, because "which content
types live inside which H2" is the part the brief needs in order to plan our own sections.

Falls back to Playwright when a page renders its body in JavaScript — the old scraper
reported medium.com as a 9-word article and silently skewed the target length.
"""
from __future__ import annotations

import json
import re
from html import unescape
from html.parser import HTMLParser

# Chrome UA: several publisher CDNs serve a challenge page to obvious bots, which used to
# come back as a ~0-word "article" and drag the SERP's average length down.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Content of these never belongs to the article.
_SKIP = {"script", "style", "noscript", "nav", "footer", "header", "aside", "form",
         "svg", "select", "template", "iframe"}
_HEADINGS = {"h1", "h2", "h3", "h4"}
_VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com", "wistia", "loom.com", "dailymotion",
                "brightcove", "jwplayer", "streamable")

_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’%$.\-]*")
_YEAR = re.compile(r"\b(20[1-3][0-9])\b")


def clean_text(t: str) -> str:
    return re.sub(r"\s+", " ", unescape(t or "").replace("‍", "").replace("\xa0", " ")).strip()


def _img_kind(alt: str, src: str) -> str:
    """Best-effort visual taxonomy, so "how many images and WHICH kinds" is answerable."""
    s = f"{alt} {src}".lower()
    if any(k in s for k in ("logo", "brand-", "/brand/", "favicon", "avatar", "icon", "badge")):
        return "logo/icon"
    if any(k in s for k in ("screenshot", "screen-shot", "screen_shot", "dashboard", "interface",
                            "app-store", "appstore", "google-play", "-ui-", "/ui/", "webapp")):
        return "screenshot"
    if any(k in s for k in ("chart", "graph", "plot", "diagram", "infographic", "comparison-table",
                            "flow", "architecture")):
        return "chart/diagram"
    if any(k in s for k in ("author", "headshot", "portrait", "team")):
        return "author/portrait"
    if any(k in s for k in ("cover", "hero", "banner", "featured")):
        return "cover/hero"
    return "photo/illustration"


class _Audit(HTMLParser):
    """Single pass over the document, attributing every element to its current heading."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.title = None
        self.meta_desc = None
        self.robots = None
        self.canonical = None
        self.h1 = None
        self.jsonld: list[dict] = []
        self.sections: list[dict] = []       # kept (article) headings, in order
        self.template_junk: list[str] = []   # headings dropped as template chrome
        self._cur_heading: str | None = None  # tag name while inside a heading
        self._heading_buf: list[str] = []
        self._in_title = False
        self._in_jsonld = False
        self._jsonld_buf: list[str] = []
        self.words = 0
        self.tables: list[dict] = []
        self._table_stack: list[dict] = []
        self._cell_buf: list[str] = []
        self._in_cell = False
        self.videos: list[str] = []
        self.images: list[dict] = []
        self.lists = 0
        self.accordions = 0

    # --- helpers ---
    def _bucket(self) -> dict | None:
        """The section every counted element is attributed to (None = above the first heading)."""
        return self.sections[-1] if self.sections else None

    def _bump(self, key: str, n: int = 1):
        b = self._bucket()
        if b is not None:
            b["has"][key] = b["has"].get(key, 0) + n

    # --- parser events ---
    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if self.skip_depth:
            # still need videos/scripts that live inside skipped containers? no — but a
            # JSON-LD <script> is the one exception we care about.
            if tag == "script" and "ld+json" in a.get("type", "").lower():
                self._in_jsonld, self._jsonld_buf = True, []
            if tag in _SKIP:
                self.skip_depth += 1
            return

        if tag == "script" and "ld+json" in a.get("type", "").lower():
            self._in_jsonld, self._jsonld_buf = True, []
            return
        if tag == "iframe":
            src = a.get("src") or a.get("data-src") or ""
            if any(h in src.lower() for h in _VIDEO_HOSTS):
                self.videos.append(src)
                self._bump("video")
            self.skip_depth += 1
            return
        if tag in _SKIP:
            self.skip_depth += 1
            return

        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = (a.get("name") or a.get("property") or "").lower()
            if name == "description" and not self.meta_desc:
                self.meta_desc = clean_text(a.get("content"))
            elif name == "robots":
                self.robots = clean_text(a.get("content"))
        elif tag == "link" and "canonical" in a.get("rel", "").lower():
            self.canonical = a.get("href")
        elif tag in _HEADINGS:
            self._cur_heading, self._heading_buf = tag, []
        elif tag == "img":
            src = a.get("src") or a.get("data-src") or a.get("data-lazy-src") or ""
            alt = clean_text(a.get("alt"))
            self.images.append({"alt": alt, "src": src, "kind": _img_kind(alt, src)})
            self._bump("img")
        elif tag == "video":
            self.videos.append(a.get("src") or "(inline <video>)")
            self._bump("video")
        elif tag == "table":
            self._table_stack.append({"headers": [], "rows": 0, "section": len(self.sections) - 1})
            self._bump("table")
        elif tag in ("th", "td") and self._table_stack:
            self._in_cell, self._cell_buf = True, []
        elif tag == "tr" and self._table_stack:
            self._table_stack[-1]["rows"] += 1
        elif tag in ("ul", "ol"):
            self.lists += 1
            self._bump("list")
        elif tag == "details":
            self.accordions += 1

    def handle_startendtag(self, tag, attrs):
        if tag in ("img", "meta", "link"):
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in _SKIP or tag == "iframe":
            if self.skip_depth:
                self.skip_depth -= 1
                if tag == "script" and self._in_jsonld:
                    self._close_jsonld()
                return
        if self.skip_depth:
            return
        if tag == "script" and self._in_jsonld:
            self._close_jsonld()
            return
        if tag == "title":
            self._in_title = False
        elif tag in _HEADINGS and self._cur_heading == tag:
            text = clean_text(" ".join(self._heading_buf))[:160]
            self._cur_heading, self._heading_buf = None, []
            if not text:
                return
            if tag == "h1" and not self.h1:
                self.h1 = text
            if tag == "h1":
                return                      # H1 is the title, not a section
            if is_template_heading(text):
                self.template_junk.append(text)
                return
            self.sections.append({"lvl": tag, "text": text, "words": 0, "has": {}})
        elif tag == "table" and self._table_stack:
            t = self._table_stack.pop()
            t["rows"] = max(0, t["rows"] - (1 if t["headers"] else 0))
            self.tables.append(t)
        elif tag in ("th", "td") and self._in_cell:
            cell = clean_text(" ".join(self._cell_buf))[:60]
            self._in_cell, self._cell_buf = False, []
            t = self._table_stack[-1] if self._table_stack else None
            if t is not None and len(t["headers"]) < 8 and t["rows"] <= 1 and cell:
                t["headers"].append(cell)

    def _close_jsonld(self):
        self._in_jsonld = False
        raw = "".join(self._jsonld_buf).strip()
        try:
            data = json.loads(raw)
        except Exception:
            return
        for d in (data if isinstance(data, list) else [data]):
            if isinstance(d, dict):
                self.jsonld.append(d)

    def handle_data(self, data):
        if self._in_jsonld:
            self._jsonld_buf.append(data)
            return
        if self.skip_depth or not data or not data.strip():
            return
        if self._in_title:
            self.title = clean_text((self.title or "") + data)
            return
        if self._cur_heading:
            self._heading_buf.append(data)
            return
        if self._in_cell:
            self._cell_buf.append(data)
        n = len(_WORD.findall(data))
        if n:
            self.words += n
            b = self._bucket()
            if b is not None:
                b["words"] += n


# Where an article stops and the template starts. A page's real sections are followed by chrome
# — sidebar promo cards, "find a translator near you", related-post teasers, comment forms — and
# trimming it keeps the H2 count honest: atanet.org otherwise reports 11 H2s for a 5-section
# article.
#
# Two rules were tried and both destroyed real articles:
#
#   1. A run of short sections. One 85-word promo card in the middle of the tail ends the run and
#      the whole tail survives.
#   2. Everything after the LAST section of 150+ words. This is far worse, and it is what shipped:
#      it assumes every real section is long and every template block is short. In a ROUNDUP —
#      the format the SEO team asks us to write — each per-product H3 is 50-110 words BY DESIGN.
#      Measured on boostlingo.com/blog/interpreter-service-providers-healthcare: 26 sections,
#      6 H2 and 20 H3 extracted correctly, and the trim kept THREE (165 of 1528 words). The last
#      150-word section was a related-posts sidebar card sitting at index 2, so the entire
#      article counted as template tail. The page was then rejected as "only 1 H2 heading — not a
#      structured article", and this is why so few prototypes qualified, why benchmarks came out
#      weak, and ultimately why one article's length target collapsed.
#
# So the tail is identified by WHAT IT SAYS, not by how long it is, and the trim is capped so it
# can never take the bulk of a document no matter what the titles look like.
TAIL_MIN_RUN = 3                # a genuine 1-2 section wrap-up is not a template tail
TAIL_MAX_SECTION_SHARE = 0.40   # never trim more than this share of the sections
TAIL_MAX_WORD_SHARE = 0.30      # ...nor this share of the words
TAIL_STUB_WORDS = 25            # an all-but-empty heading joins a tail already in progress

# Headings that are page furniture wherever they appear. Used at parse time (a match is recorded
# in `template_junk` and kept out of the outline the brief sees), by the tail trim, and by the
# benchmark's H2/H3 counts. Deliberately narrow: every pattern is a navigational, promotional or
# consent block, never a section of an article. A broad word list ("product", "use case", "try")
# was tried and, measured over 484 saved competitor headings, removed real sections such as
# "Best Use Cases" and "Moving to production" - add a pattern here only if it can never be content.
_TEMPLATE_HEADING = re.compile(r"""(?ix) ^\s*(
      related \s* (blogs?|posts?|articles?|reading|content|resources (\s+ for \s+ you)?)
    | further \s+ reading | additional \s+ resources
    | (recent|latest|popular|trending|more|similar|featured) \s+ (blogs?|posts?|articles?|stories|news|from)
    | you \s+ (might|may) \s+ (also \s+)? (like|enjoy)
    | (in \s+ this \s+ article | table \s+ of \s+ contents | contents | on \s+ this \s+ page | jump \s+ to)
    | (about|meet) \s+ the \s+ author | about \s+ (us|the \s+ writer)
    | (share|follow) \s+ (this|us) | share \s+ on
    | (leave \s+ a \s+)? (comments?|reply) (\s+ cancel \s+ reply)? | post \s+ a \s+ comment
    | subscribe \b .* | (sign \s+ up|newsletter|stay \s+ (updated|informed)|get \s+ (started|in \s+ touch))
    | (blog|categories|tags|archives|search)
    | (contact|contact \s+ us|request \s+ a \s+ (demo|quote)|book \s+ a \s+ (demo|call))
    | (privacy|terms|cookies?) \b .*
    | (targeting|functional|performance|analytics|advertising|marketing|strictly \s+ necessary
       |necessary|essential|preference|third[-_]?party) \s+ cookies
    | [\w.&'’-]+ (\s+ [\w.&'’-]+){0,3} \s+ uses \s+ cookies
    | manage \s+ (cookies?|consent) (\s+ (preferences|settings))? | consent \s+ (preferences|settings|manager)
  )\s*[:.]?\s*$""")


def is_template_heading(text: str) -> bool:
    return bool(_TEMPLATE_HEADING.match(text or ""))


def _trim_template_tail(sections: list[dict]) -> tuple[list[dict], list[str]]:
    """Drop the trailing run of page furniture. Never drop the bulk of the document."""
    if not sections:
        return sections, []
    total_words = sum(s["words"] for s in sections)

    # walk back from the end while the section is furniture, or a stub inside a run already going
    cut = len(sections)
    for i in range(len(sections) - 1, -1, -1):
        sec = sections[i]
        if is_template_heading(sec["text"]):
            cut = i
            continue
        if cut < len(sections) and sec["words"] <= TAIL_STUB_WORDS:
            cut = i
            continue
        break

    tail = sections[cut:]
    if len(tail) < TAIL_MIN_RUN:
        return sections, []
    # The caps are the real protection: the patterns above are a guess about someone else's HTML,
    # and a guess must not be able to delete an article.
    if (len(tail) > len(sections) * TAIL_MAX_SECTION_SHARE
            or (total_words and sum(s["words"] for s in tail) > total_words * TAIL_MAX_WORD_SHARE)):
        return sections, []
    return sections[:cut], [sec["text"] for sec in tail]


def _schema_types(blocks: list[dict]) -> list[str]:
    """Flatten @type across JSON-LD blocks, including @graph children."""
    out: list[str] = []

    def walk(d):
        if isinstance(d, dict):
            t = d.get("@type")
            for v in ([t] if isinstance(t, str) else (t or [])):
                if isinstance(v, str):
                    out.append(v)
            for child in (d.get("@graph") or []):
                walk(child)
            for child in (d.get("mainEntity") or []) if isinstance(d.get("mainEntity"), list) else []:
                walk(child)
        elif isinstance(d, list):
            for x in d:
                walk(x)

    for b in blocks:
        walk(b)
    return list(dict.fromkeys(out))


def _fetch_requests(url: str, timeout: int = 15):
    import requests

    r = requests.get(url, timeout=timeout, allow_redirects=True,
                     headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"})
    return r.text, r.status_code, str(r.url)


def _fetch_playwright(url: str, timeout_ms: int = 25000):
    """For JS-rendered articles. Chromium ships in the image (see the screenshot producer)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-sandbox"])
        try:
            pg = b.new_page(user_agent=_UA, viewport={"width": 1440, "height": 900})
            resp = pg.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            try:
                pg.wait_for_load_state("networkidle", timeout=6000)
            except Exception:
                pass
            return pg.content(), (resp.status if resp else None), pg.url
        finally:
            b.close()


# A real article below this word count means we failed to get the body (paywall, bot wall,
# or a JS-rendered page) rather than that the article is short.
MIN_PLAUSIBLE_WORDS = 350


def _fetch_remote(url: str, remote: dict, render_js: bool):
    """The same two reads through Scrapfly, in the market's country (core/tools/scrapfly.py)."""
    from . import scrapfly

    res = scrapfly.scrape(url, remote.get("country"), render_js=render_js)
    err = res.get("error")
    if err and not res.get("content"):
        raise RuntimeError(f"{err.get('code')}: {err.get('message')}")
    return res.get("content") or "", res.get("status"), res.get("url") or url


def audit_page(url: str, deep: bool = True, use_playwright: bool = True,
               remote: dict | None = None) -> dict:
    """Teardown of one competitor page.

    `deep=False` keeps only the cheap outline (used for the SERP results we are NOT taking
    as prototypes); `deep=True` returns the full content inventory. `remote` ({"country": ...})
    reads the page through Scrapfly in that country: a plain fetch first, a rendered one when
    the plain fetch came back thin, as with requests and the local browser.
    """
    rec: dict = {"url": url}
    html = ""
    plain, rendered = ("scrapfly", "scrapfly-js") if remote else ("requests", "playwright")
    try:
        html, status, final = _fetch_remote(url, remote, False) if remote else _fetch_requests(url)
        rec.update(status=status, final_url=final, fetched_with=plain)
    except Exception as e:  # noqa: BLE001
        rec.update(status=None, fetched_with=plain, error=str(e)[:120])

    a = _parse(html) if html else None
    thin = (a is None) or (a.words < MIN_PLAUSIBLE_WORDS) or not a.sections
    if thin and use_playwright:
        try:
            html, status, final = _fetch_remote(url, remote, True) if remote else _fetch_playwright(url)
            a2 = _parse(html)
            # keep whichever render actually produced an article
            if a is None or a2.words > a.words:
                a, rec["status"], rec["final_url"] = a2, status, final
                rec["fetched_with"] = rendered
                rec.pop("error", None)
        except Exception as e:  # noqa: BLE001
            rec.setdefault("error", f"{rendered}: {str(e)[:100]}")

    if a is None:
        rec.update(words=0, outline=[], images={"total": 0, "kinds": {}}, tables=[], videos=0,
                   lists=0, faq={"present": False, "how": None, "count": 0}, schema_types=[],
                   template_junk=[], meta_title=None, meta_description=None, h1=None)
        return rec

    schema = _schema_types(a.jsonld)
    body, tail = _trim_template_tail(a.sections)
    a.template_junk.extend(tail)
    # FAQ detection and question harvesting run over ALL headings, not just the trimmed body:
    # an FAQ block usually sits at the very END of an article, so it lands in the template tail.
    # We must not COPY those headings (they stay in template_junk) but the questions themselves
    # are the best available read on what the competitor thinks searchers ask.
    faq_headings = [s["text"] for s in a.sections
                    if re.search(r"\b(faqs?|frequently asked|common questions|questions & answers)\b",
                                 s["text"], re.I)]
    faq_how = ("schema.org FAQPage" if "FAQPage" in schema
               else "H2/H3 section" if faq_headings
               else "<details> accordion" if a.accordions >= 3 else None)
    # question-shaped headings are how an FAQ usually shows up in the body
    q_headings = [s["text"] for s in a.sections if s["text"].rstrip().endswith("?")]

    kinds: dict[str, int] = {}
    for img in a.images:
        kinds[img["kind"]] = kinds.get(img["kind"], 0) + 1

    mt = a.title or ""
    md = a.meta_desc or ""
    ym = _YEAR.search(mt or a.h1 or "")
    rec.update(
        meta_title=mt or None, meta_title_len=len(mt) or None,
        meta_description=md or None, meta_description_len=len(md) or None,
        h1=a.h1, canonical=a.canonical, robots=a.robots,
        words=a.words,
        # A table-of-contents or "Related posts" heading is furniture wherever it sits, not only
        # in a trailing run — boostlingo's "In this Article" H2 sits at index 1 and would
        # otherwise be counted as a section of the article.
        h2=sum(1 for s in body if s["lvl"] == "h2" and not is_template_heading(s["text"])),
        h3=sum(1 for s in body if s["lvl"] == "h3" and not is_template_heading(s["text"])),
        year_in_title=int(ym.group(1)) if ym else None,
        outline=[{k: s[k] for k in ("lvl", "text", "words", "has")} for s in body][:40],
        images={"total": len(a.images), "kinds": kinds},
        tables=[{"headers": t["headers"], "rows": t["rows"]} for t in a.tables][:8],
        videos=len(a.videos),
        lists=a.lists,
        faq={"present": bool(faq_how), "how": faq_how,
             "count": len(q_headings) or len(faq_headings),
             "question_headings": q_headings[:12]},
        schema_types=schema,
        template_junk=list(dict.fromkeys(a.template_junk))[:12],
    )
    if not deep:
        for k in ("images", "tables", "videos", "lists", "schema_types", "template_junk"):
            rec.pop(k, None)
    return rec


def _parse(html: str) -> _Audit:
    p = _Audit()
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass  # partial parse still carries usable structure
    return p
