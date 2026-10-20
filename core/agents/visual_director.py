"""Visual Director — turns the writer's visual markers into real assets, and adds the visuals
the competitors' format calls for.

Producers: charts (matplotlib, producers/charts), screenshots (Playwright or Scrapfly,
producers/screenshot), infographics (HTML templates, producers/infographic) and the cover
(gpt-image-2 background + headline + the project's logo, producers/cover).

Chart flow:
  1. collect `<!-- visual:chart ... -->` markers from the draft
  2. ONE Claude call: markers + researched facts -> chart specs (numbers strictly from
     real data; a chart without supporting data is skipped, never invented)
  3. producers.charts.build() -> <output_dir>/assets/chart_N.png
  4. replace each marker with the embedded image + italic caption; skipped/failed
     markers are removed (graceful degradation — a visual never fails the article)
"""
from __future__ import annotations

import base64
import io
import json
import math
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from .base import BaseAgent
from ..producers import charts, screenshot, infographic, cover as cover_producer
from .. import research_sources
from ..tools import scrapfly
from ..store import atomic_write_text

_MARKER = re.compile(r"<!--\s*visual:(\w+)\s+(.*?)-->", re.DOTALL)
_ATTR = re.compile(r'(\w+)="([^"]*)"')


def _attrs(spec: str) -> dict:
    return dict(_ATTR.findall(spec or ""))


def insert_after_mention(md: str, needle: str, block: str) -> tuple[str, bool]:
    """Insert `block` after the first PROSE paragraph mentioning `needle` —
    never after headings/images/tables, never adjacent to another image/marker."""
    lines = md.split("\n")
    for idx, ln in enumerate(lines):
        s = ln.strip()
        if not s or s.startswith(("#", "!", "|", "<!--", "*", "-", ">")):
            continue
        if needle.lower() not in s.lower():
            continue
        j = idx
        while j < len(lines) - 1 and lines[j + 1].strip():
            j += 1
        near = lines[max(0, idx - 3):idx] + lines[j + 1:j + 4]
        if any(x.strip().startswith(("!", "<!-- visual")) for x in near):
            continue  # image already nearby — try the next mention
        lines[j + 1:j + 1] = ["", block]
        return "\n".join(lines), True
    return md, False


_HEADING = re.compile(r"^#{2,4}\s+(.+?)\s*$", re.M)


# --- the fallback chain for a refused screenshot (S4) --------------------------------------------
_RETRY_DELAY_S = 10            # a challenge is sometimes transient (Reverso served its real page later)
_TRANSIENT = ("CHALLENGE", "BLANK", "ERROR")
_FOCUS_STOP = {"page", "pages", "screenshot", "interface", "showing", "shows", "show", "product",
               "products", "official", "website", "homepage", "home", "dashboard", "with", "from",
               "that", "this", "their", "your", "and", "the", "for", "into", "view", "overview",
               "section", "listing", "app", "apps"}


def _focus_terms(caption: str) -> list[str]:
    """The caption's distinctive words and figures - what the screenshot is supposed to show."""
    out: list[str] = []
    for t in re.findall(r"[$€£]?\d[\d,.]*\+?%?|[A-Za-z][A-Za-z\-]{3,}", caption or ""):
        low = t.lower().strip(".,")
        if low and low not in _FOCUS_STOP and low not in out:
            out.append(low)
    return out[:8]


def _focus_ok(diag: dict, terms: list[str]) -> bool:
    """Does the capture show what the caption says? Used to accept a SUBSTITUTE page only."""
    if not terms:
        return True
    f = diag.get("focus") or {}
    return int(f.get("after") or 0) >= max(2, math.ceil(len(terms) / 2))


def _registrable(host: str) -> str:
    parts = [p for p in (host or "").lower().split(".") if p and p != "www"]
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "ac", "gov") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _alt_pages(research: dict | None, url: str) -> list[str]:
    """Other pages of the same vendor that the research itself used, most-cited first."""
    r = research or {}
    dom = _registrable(urlparse(url).hostname or "")
    seen: dict[str, int] = {}
    urls = [s.get("source_url") for s in (r.get("key_statistics") or [])]
    urls += [f.get("source_url") for fd in (r.get("findings") or []) for f in (fd.get("facts") or [])]
    urls += [s.get("url") for s in (r.get("sources") or []) if isinstance(s, dict)]
    for u in urls:
        if not u or not str(u).startswith("http") or u.rstrip("/") == url.rstrip("/"):
            continue
        if _registrable(urlparse(u).hostname or "") == dom:
            seen[u] = seen.get(u, 0) + 1
    return sorted(seen, key=lambda u: -seen[u])


def _own_home(brand, url: str) -> bool:
    """Our own site's homepage (any of its hosts, no path): never a substitute screenshot."""
    return brand.is_own(url) and not urlparse(url).path.strip("/")


def _subject(section: str, url: str) -> tuple[str, str]:
    """(name, host) for a vendor's slot: the product a numbered roundup section names ("5. Amazon
    Polly: best for ..."), else the page's own host - never the bare registrable domain."""
    host = (urlparse(url).hostname or "").removeprefix("www.")
    m = re.match(r"^\s*\d+[.)]\s*(.+)$", section or "")
    name = re.split(r"\s*(?::|—|–|\s-\s)\s*", m.group(1))[0].strip()[:40] if m else host
    return name or host, host


_TITLE_SITE = re.compile(r"\s+([|·•]|[-–—]|::)\s+")
_NAME_SMALL = frozenset({"a", "an", "and", "at", "by", "for", "in", "of", "on", "or", "the", "to", "&"})


def page_title(text) -> str:
    """A page's title as a caption: the whole title with only its site's name dropped, never cut
    and never ended with "…" (testers, 2026-10-20: "…American Translators…" under a picture). The
    site's name is the short part after the LAST separator: after " | ", " · " or " :: " always,
    after a dash when it reads as a name ("American Translators Association"), so "Pricing plans -
    annual versus monthly | Acme" keeps its clause (Codex RA54-01). A title whose own part is one
    word ("Home - Acme") is kept whole."""
    t = " ".join(re.sub(r"[\[\]*<>`]+", " ", str(text or "")).split())
    last = None
    for last in _TITLE_SITE.finditer(t):
        pass
    if last:
        head, site = t[:last.start()], t[last.end():].split()
        named = last.group(1) not in "-–—" or all(
            not w[0].isalpha() or w[0].isupper() or w.lower() in _NAME_SMALL for w in site)
        if len(head.split()) >= 2 and 0 < len(site) <= 5 and named:
            t = head
    return t


# --- checking a capture that did succeed (S3) ------------------------------------------------------
# The capture-time checks catch what the DOM shows; the audit's bad shots that got past them were a
# captcha drawn in a canvas, a page in the wrong language, and - most of all - a real page that is
# not what the caption says. A near-duplicate of a screenshot already in the article is caught in
# code; the rest by a look at each shot (pipeline.visuals.vision_check, `look_at_shot`): the
# producer sees its own shot with the page's sections and keeps it, refuses it, or names the
# section to frame instead and takes it again (the operator, 2026-10-06: "the visual producer
# should decide what to crop and check itself"; four re-captures of a pricing table had come back
# as the page's header or a heading alone).
_DUP_BITS = 10                 # of 256: a re-encode or a blinking cursor, not a different page
SHOT_LABELS = ("OK", "REFRAME", "CHALLENGE", "COOKIE_WALL", "OVERLAY", "LOGIN", "BLANK", "PARTIAL",
               "WRONG_LANGUAGE", "MISMATCH", "MARKETING")
REFRAMES = 2                   # times a shot is taken again on the section its look names


def _dhash(path) -> int | None:
    """A 256-bit difference hash: near-identical images differ in a few bits."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            g = im.convert("L").resize((17, 16))
            px = list(g.getdata())
    except Exception:  # noqa: BLE001 — an unreadable file is simply not compared
        return None
    bits = 0
    for r in range(16):
        for c in range(16):
            bits = (bits << 1) | (px[r * 17 + c] > px[r * 17 + c + 1])
    return bits


def _vision_prompt(caption: str | None, outline: list[dict] | None = None, want: str | None = None,
                   words: bool = False, own: bool = False) -> str:
    # Measured on the 62 audited shots, a first wording that asked for "what the caption
    # describes" refused 17 of 50 good ones: the right pricing page, but not the one price the
    # caption named. The judgement is about the page - its subject, readability and rendering -
    # never about whether every caption detail is in the viewport. With the page's sections, the
    # look may move the frame to the section the caption is about (REFRAME) instead of refusing;
    # with a person's instruction (a Re-capture) it must show what they asked for.
    sections = ""
    page_sections = ("\n\nTHE PAGE'S SECTIONS (each heading, then the start of the text under it):\n" + "\n".join(
        f"- {s.get('heading')}" + (f": {s.get('text')}" if s.get("text") else "") for s in outline[:40])) if outline else ""
    if caption:
        expect = f'It will be published under the caption: "{caption}".'
        if want:
            expect += f' The person asked for this shot: "{want}". It must show what they asked for.'
        ok = ("OK - a clean, readable page of the product, company or page the caption names, showing the "
              "part of the page the caption is about. A detail the caption mentions (one price, one mode, "
              "one setting) does not need to be in view")
        extra = "MISMATCH - a readable page of a different product, company or kind of page than the caption names"
        extra += (", or no part of this page shows what the person asked for\n" if want else "\n")
        if outline:
            extra = ("REFRAME - the right page, but the shot shows another part of it (its header, its top "
                     "banner, another section) than the part the caption is about, and one of THE PAGE'S "
                     "SECTIONS below is that part: copy its heading exactly into \"heading\"\n") + extra
            sections = page_sections
    else:
        expect = "It has no caption yet." if words else "It will be published under its own page title."
        if want:
            expect += f' The person asked for this shot: "{want}". It must show what they asked for.'
        ok = "OK - a clean, readable page" + (", showing what the person asked for" if want else "")
        extra = "MISMATCH - no part of this page shows what the person asked for\n" if want else ""
        if want and outline:
            # a shot with no caption still has a subject when the person asked for one: the look
            # may move the frame to the section that shows it (R6FA-01)
            extra = ("REFRAME - the right page, but the shot shows another part of it (its header, its top "
                     "banner, another section) than the part the person asked for, and one of THE PAGE'S "
                     "SECTIONS below is that part: copy its heading exactly into \"heading\"\n") + extra
            sections = page_sections
    if own:
        # our own page is shown only where it shows the product (testers, 2026-10-20: a hero
        # headline, a call to action, a carousel cut midway were "weird"; Codex RA54-02)
        extra += ("MARKETING - this is our own site, and the shot shows a marketing section (a hero "
                  "headline, a slogan, a call to action, a sign-up banner, a carousel, testimonials) "
                  "instead of the product itself: its interface, a pricing table or a feature screen\n")
    # a shot without words of its own: the look that sees it writes them (the operator, 2026-10-06:
    # a good shot is kept, and a missing caption is written, not borrowed from another page)
    own = ('\n\nThe shot has no caption yet. When it is OK, also write "alt" (one plain sentence: what the '
           'picture shows) and "caption" (one short sentence the reader sees under it), from what you see '
           'in the shot. Name no price, number or date you cannot read in it.') if words else ""
    fields = ', "alt": "for OK: what the picture shows", "caption": "for OK: one short sentence"' if words else ""
    return f"""This is a screenshot taken for an article. {expect}
Label it with exactly one of:
{ok}
CHALLENGE - a bot check, captcha or "verify you are human" page
COOKIE_WALL - a cookie or consent banner covers a large part of the page
OVERLAY - a pop-up, modal, newsletter box or chat window covers the content, or the page is dimmed as if behind one
LOGIN - a sign-in page, a paywall or an access-denied page
BLANK - empty, still loading, or an error page
PARTIAL - the page did not finish rendering: a large part of the view is blank or unpainted
{extra}WRONG_LANGUAGE - the page, or a bar across it, is not in English{sections}{own}

Write "why" as one short sentence in plain English: what the shot shows, and what it should show when it is not OK.
Return ONLY JSON: {{"label": "...", "why": "one short sentence", "heading": "for REFRAME only: the heading, copied exactly"{fields}}}"""


def _shot_block(png) -> dict:
    """The shot as an image block, scaled to the model's own working size (fewer tokens)."""
    from PIL import Image

    with Image.open(png) as im:
        im = im.convert("RGB")
        im.thumbnail((1568, 1568))
        buf = io.BytesIO()
        im.save(buf, format="PNG")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                        "data": base64.b64encode(buf.getvalue()).decode()}}


def _named(outline: list[dict] | None, name) -> str:
    """The outline's own heading the look named ("" when it names none of them)."""
    want = " ".join(str(name or "").lower().split())
    if not want:
        return ""
    heads = [str(s.get("heading") or "") for s in outline or []]
    return next((h for h in heads if " ".join(h.lower().split()) == want), "") or \
        next((h for h in heads if want in " ".join(h.lower().split())), "")


def look_at_shot(call, png, caption: str | None, outline: list[dict] | None = None,
                 want: str | None = None, words: bool = False, own: bool = False) -> dict:
    """One look at a shot: {"label", "why", "heading", "looked"}, and with `words` (a shot with no
    caption of its own) the "alt" and "caption" it writes from what it sees. `call` is an agent's
    call_claude_json. "REFRAME" comes with a heading of the page's own outline to frame instead.
    Fails OPEN ("looked": False, label OK): an unusable answer keeps the capture, which already
    passed the capture-time checks, and says so in "why"."""
    def kept(why):
        return {"label": "OK", "why": why, "heading": "", "looked": False}

    try:
        data, _ = call([_shot_block(png), {"type": "text", "text": _vision_prompt(caption, outline, want, words, own)}],
                       max_tokens=2000)
    except Exception as e:  # noqa: BLE001
        return kept(f"vision check unavailable ({str(e)[:80]}); kept on the capture-time checks")
    label = str((data or {}).get("label") or "").strip().upper()
    if label not in SHOT_LABELS or (label == "REFRAME" and not ((caption or want) and outline)):
        return kept("vision check gave no usable label; kept on the capture-time checks")
    heading = _named(outline, (data or {}).get("heading")) if label == "REFRAME" else ""
    if label == "REFRAME" and not heading:
        return kept("the look named no section of the page; kept on the capture-time checks")
    out = {"label": label, "why": " ".join(str((data or {}).get("why") or "").split())[:300],
           "heading": heading, "looked": True}
    if words and label == "OK":
        out.update({k: " ".join(str((data or {}).get(k) or "").split())[:300] for k in ("alt", "caption")})
    return out


class ShotLook(BaseAgent):
    """The producer's look at its own shot (`look_at_shot`), on the model the profile gives the
    visuals (models.visuals, else the worker's)."""
    name = "shot_look"
    model_role = "visuals"
    MAX_RETRIES = 2         # a look that cannot be had keeps the shot: no long wait for it

    def look(self, png, caption: str | None, outline: list[dict] | None = None, want: str | None = None,
             words: bool = False, own: bool = False) -> dict:
        return look_at_shot(lambda content, max_tokens=None: self.call_claude_json(
            content, system="You look at screenshots taken for an article. Answer only with the JSON asked for.",
            max_tokens=max_tokens), png, caption, outline, want, words, own)


def _section_of(md: str, needle: str) -> str:
    """Title of the H2-H4 section that contains `needle` ("" if none precedes it)."""
    pos = md.find(needle)
    if pos < 0:
        return ""
    title = ""
    for h in _HEADING.finditer(md):
        if h.start() >= pos:
            break
        title = h.group(1)
    return title


def next_free(assets_dir: Path, prefix: str) -> int:
    """Next unused index for assets/<prefix>_N.png — never overwrite earlier passes."""
    i = 1
    while (assets_dir / f"{prefix}_{i}.png").exists():
        i += 1
    return i


def _norm_heading(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (t or "").lower()).strip()


def resolve_heading(md: str, wanted: str) -> str | None:
    """Match a PROPOSED heading to a real one, tolerating case and punctuation drift.

    The anchor regex needed the heading verbatim, and a near miss was a silent `continue`. One
    run proposed a full visual budget and landed NONE of it, shipping an article with zero body
    images past three revision cycles — the scorecard caught it at the very end. An exact-match
    requirement on text a model retypes is a coin flip, so compare on letters and digits only.
    """
    want = _norm_heading(wanted)
    if not want:
        return None
    loose = None
    for m in re.finditer(r"^#{2,3}\s+(.+)$", md, flags=re.M):
        head = m.group(1).strip()
        norm = _norm_heading(head)
        if norm == want:
            return head
        if loose is None and len(want) > 12 and (want in norm or norm in want):
            loose = head
    return loose


def build_cover(project, config, article_dir, headline_source: str, prompt: str,
                attempts: int = 2, fresh: bool = False) -> tuple[str | None, str | None]:
    """Make `assets/cover.png`: a gpt-image-2 background, the headline, the project's logo.
    Returns (path, None), or (None, why) after `attempts` tries. An existing cover_bg.png is
    reused, so a retry after a composing error costs nothing; `fresh` asks for a new background,
    and the old cover and background stay until the new pair is whole."""
    from ..tools import openai_images

    assets = Path(article_dir) / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    headline = cover_producer.headline_case(headline_source)
    logo = project.brand.logo_file()        # None: the project has no logo
    q = (config.get("pipeline", {}).get("visuals", {}) or {}).get("cover_quality", "medium")
    for _ in range(max(1, attempts)):
        built = cover_producer.generate(headline, prompt, str(assets / "cover.png"),
                                        logo_png=str(logo) if logo else None, quality=q, fresh=fresh)
        if built:
            return built, None
    return None, openai_images.last_error() or "the cover could not be composed"


class VisualDirector(BaseAgent):
    name = "visual_director"

    def run(self, state):
        state.current_agent = "visual_director"
        out = Path(state.output_dir)
        assets = out / "assets"

        # enrichment pass: the writer under-illustrates — propose additional meaningful
        # visuals (vendor screenshots, extra charts) up to the configured density
        try:
            self._enrich(state)
        except Exception as e:  # noqa: BLE001 — enrichment must never break the pipeline
            state.note(f"[visuals] enrichment skipped: {e}")

        markers = list(_MARKER.finditer(state.draft_markdown))
        chart_markers = [m for m in markers if m.group(1) == "chart"]
        manifest = []

        # --- charts: one Claude call for all markers -> specs -> PNGs -> embed ---
        if chart_markers:
            assets.mkdir(parents=True, exist_ok=True)
            specs = self._chart_specs(state, chart_markers)
            style = None
            if self.project.chart_style:
                style = str(self.project.root / self.project.chart_style)
            md = state.draft_markdown
            for i, m in enumerate(chart_markers, 1):
                spec = specs.get(i)
                repl = ""  # default: drop the marker (skip / no spec / build failure)
                entry = {"type": "chart", "status": "skipped", "path": None, "caption": None}
                if spec and not spec.get("skip"):
                    n = next_free(assets, "chart")
                    png = assets / f"chart_{n}.png"
                    built = charts.build(spec, str(png), style_path=style)
                    if built:
                        caption = spec.get("caption") or spec.get("title", "")
                        alt = spec.get("alt") or caption
                        repl = f"![{alt}](assets/chart_{n}.png)\n*{caption}*"
                        entry = {"type": "chart", "status": "generated",
                                 "path": f"assets/chart_{n}.png", "caption": caption,
                                 "spec": {k: spec.get(k) for k in ("type", "title", "unit")}}
                md = md.replace(m.group(0), repl, 1)
                manifest.append(entry)
            state.draft_markdown = md
            atomic_write_text(out / "04_draft.md", md)

        # --- screenshots: capture url= markers via Playwright, embed or drop ---
        shot_markers = [m for m in markers if m.group(1) == "screenshot"]
        if shot_markers:
            assets.mkdir(parents=True, exist_ok=True)
            md = state.draft_markdown
            seen: list[int] = []        # hashes of the screenshots accepted so far in this article
            for i, m in enumerate(shot_markers, 1):
                a = _attrs(m.group(2))
                url = a.get("url")
                caption = a.get("caption") or url
                if url and research_sources.leaves_out(out, url, self.project):
                    # a project's own research source leaves this page out of the article: the
                    # marker goes and, with no refusal reason recorded, the editor does not ask
                    # for the slot back
                    state.note(f"[visuals] screenshot dropped, left out by a research source: {url}")
                    url = None
                # the section is recorded so a refused capture can be reported against the place
                # it was meant for, and so the report can tell when a substitute has filled it
                section = _section_of(md, m.group(0))
                entry = {"type": "screenshot", "status": "skipped", "path": None, "url": url,
                         "caption": caption, "section": section}
                repl = ""
                if url:
                    n = next_free(assets, "screenshot")
                    png = assets / f"screenshot_{n}.png"
                    terms = _focus_terms(caption)
                    sel = a.get("selector") or None
                    built, diag = self._shoot(url, png, caption, seen, selector=sel, focus=terms)
                    first, used, fallback = dict(diag), url, None
                    # S4 fallback chain: a refusal is not the end of the slot
                    if not built and diag.get("reason") in _TRANSIENT:
                        time.sleep(_RETRY_DELAY_S)
                        built, diag = self._shoot(url, png, caption, seen, selector=sel, focus=terms)
                        fallback = "retry" if built else None
                    if not built:
                        # a page a research source leaves out is never a substitute either, nor
                        # our own homepage: its top is a hero and a call to action, not the
                        # product (Codex RA54-02)
                        alts = [u for u in _alt_pages(state.research, url)
                                if not research_sources.leaves_out(out, u, self.project)
                                and not _own_home(self.project.brand, u)]
                        for alt in alts[:2]:
                            # a substitute is captioned by its own title, so it is judged as a
                            # clean page, not against the writer's caption
                            ok2, d2 = self._shoot(alt, png, None, seen, focus=terms)
                            if ok2:
                                if _focus_ok(d2, terms):
                                    built, used, fallback, diag = str(png), alt, "alt_page", d2
                                    break
                                png.unlink(missing_ok=True)    # a real page, but not what the caption says
                    if built:
                        if diag.get("dhash") is not None:
                            seen.append(diag["dhash"])
                        shown = caption
                        if fallback == "alt_page":
                            # a different page than the one the writer described: say what is in
                            # the image (the page's own title) and keep the writer's words on record
                            name, host = _subject(section, used)
                            title = page_title(diag.get("title"))
                            if title:
                                shown = title if name.lower() in title.lower() else f"{name}: {title}"
                            else:
                                shown = f"{caption}, captured from {host}{urlparse(used).path.rstrip('/')}"
                        # under our own page the caption links to it (brand.caption_for)
                        repl = (f"![{shown}](assets/screenshot_{n}.png)\n"
                                f"*{self.project.brand.caption_for(shown, used)}*")
                        entry = {"type": "screenshot", "status": "generated",
                                 "path": f"assets/screenshot_{n}.png", "caption": shown, "url": used,
                                 "section": section}
                        if shown != caption:
                            entry["caption_original"] = caption
                        if diag.get("dropped_backdrops"):
                            entry["dropped_backdrops"] = diag["dropped_backdrops"]
                        if diag.get("focus"):
                            entry["focus"] = diag["focus"]
                        if diag.get("vision"):
                            entry["vision"] = diag["vision"]
                        if fallback:
                            entry["fallback"] = fallback
                            entry["refused"] = {"url": url, "reason": first.get("reason"),
                                                "detail": first.get("detail")}
                            state.note(f"[visuals] screenshot for '{section}' via {fallback}: {used}")
                    else:
                        # no picture of the vendor's facts in text cards: the facts are the
                        # article's text, and a picture of text has no value of its own (testers,
                        # 2026-10-20: "tables as a picture are odd")
                        entry["reason"] = first.get("reason") or "ERROR"
                        entry["detail"] = first.get("detail") or ""
                        state.note(f"[visuals] screenshot refused for '{section}': {url} — "
                                   f"{entry['reason']} ({entry['detail']})")
                md = md.replace(m.group(0), repl, 1)
                manifest.append(entry)
            state.draft_markdown = md
            atomic_write_text(out / "04_draft.md", md)

        # --- cover: photo bg (gpt-image-2) + headline + logo pill, per the blog template ---
        # Every article gets one: it is the WordPress featured image. The writer's marker only
        # carries the background prompt; without a marker the topic is the prompt. A failed cover
        # is retried once and its reason written down (the Zoom article shipped without one and
        # the record said only "skipped").
        cover_markers = [m for m in markers if m.group(1) == "cover"]
        if cover_markers or not state.frontmatter.get("cover"):
            prompt = (_attrs(cover_markers[0].group(2)).get("prompt") if cover_markers else None) or state.topic
            md = state.draft_markdown
            for cm in cover_markers:  # cover lives in frontmatter, not the body
                md = md.replace(cm.group(0), "", 1)
            state.draft_markdown = md
            atomic_write_text(out / "04_draft.md", md)
            headline = ((state.keyword_research or {}).get("primary_keyword")
                        or (state.brief or {}).get("title") or state.topic)
            built, why = build_cover(self.project, self.config, out, headline, prompt)
            if built:
                state.frontmatter["cover"] = "assets/cover.png"
                manifest.append({"type": "cover", "status": "generated", "path": "assets/cover.png",
                                 "headline": cover_producer.headline_case(headline)})
            else:
                manifest.append({"type": "cover", "status": "skipped", "path": None, "reason": why})
                state.note(f"[visuals] no cover: {why}")

        # --- illustration: keep markers, record as pending (rarely used; wire later) ---
        for m in markers:
            kind = m.group(1)
            if kind in ("chart", "screenshot", "cover"):
                continue
            manifest.append({"type": kind, "status": "pending", "path": None,
                             "spec": m.group(2).strip()[:200]})

        state.visuals = manifest
        done = sum(1 for v in manifest if v["status"] == "generated")
        pend = sum(1 for v in manifest if v["status"] == "pending")
        skip = sum(1 for v in manifest if v["status"] == "skipped")
        state.note(f"[visuals] generated={done} skipped={skip} pending(cover/illustration)={pend}")
        return state

    # ---- LLM: propose additional visuals where the writer left gaps ----
    def _enrich(self, state):
        md = state.draft_markdown
        vis_cfg = self.config.get("pipeline", {}).get("visuals", {})
        max_total = int(vis_cfg.get("max_per_article", 10))
        per_words = int(vis_cfg.get("words_per_visual", 350))

        existing = len(_MARKER.findall(md)) + len(re.findall(r"!\[[^\]]*\]\(assets/", md))
        target = min(max_total, max(3, len(md.split()) // per_words))

        # Prefer what the RANKING PAGES actually carry over our fixed words-per-visual constant.
        # The keyword stage now measures it (`content_benchmark.images_median`), and the constant
        # was quietly losing: one article came out with 2 visuals against a 1 330-word target
        # while its three prototype competitors carried 8, 12 and 22 images. A density rule tuned
        # for 3 000-word articles under-serves a short one, and image count is a format
        # expectation of the SERP, not a function of our word count.
        bench = ((state.keyword_research or {}).get("content_benchmark") or {})
        measured = bench.get("images_median")
        if isinstance(measured, int) and measured > 0:
            was = target
            target = min(max_total, max(target, measured))
            if target != was:
                state.note(f"[visuals] target raised {was} -> {target} to match the "
                           f"{bench.get('sample')} ranking competitors (median {measured} images)")

        budget = target - existing
        if budget <= 0:
            return

        headings = re.findall(r"^(#{2,3})\s+(.+)$", md, flags=re.M)
        heads_block = "\n".join(f"- {h[1].strip()}" for h in headings)
        existing_alts = re.findall(r"!\[([^\]]+)\]\(assets/", md)
        existing_block = "\n".join(f"- {a[:140]}" for a in existing_alts) or "(none)"
        r = state.research or {}
        # a page a project's own research source leaves out of this article is not offered
        srcs = [s for s in (r.get("sources") or [])
                if not research_sources.leaves_out(state.output_dir, (s or {}).get("url"), self.project)]
        sources = "\n".join(
            f"- {s.get('title') or ''} | {s.get('url')}" for s in srcs[:25]
        ) or "(none)"
        facts = "\n".join(
            f"- {s.get('stat')}: {s.get('value','')}" for s in (r.get("key_statistics") or [])[:20]
        ) or "(none)"

        own = self.project.brand.domain if self.project.brand and self.project.brand.domain else "our site"
        prompt = f"""ARTICLE TOPIC: {state.topic}

The article below under-uses visuals. Propose UP TO {budget} additional visuals that add
real information (SEO needs visually rich articles — but no decorative filler).

ARTICLE HEADINGS (insert after one of these, verbatim):
{heads_block}

RESEARCH SOURCES (only these URLs, or official homepages of products explicitly discussed
in a heading's section, may be screenshotted):
{sources}

KEY STATISTICS available for extra charts (numbers must come from here):
{facts}

VISUALS ALREADY IN THE ARTICLE (do NOT propose anything showing the same content — and
never a chart whose MESSAGE overlaps one of these, e.g. a second cost-comparison chart
even with slightly different categories):
{existing_block}

Rules:
- screenshot: ONLY for a product/service/vendor actually discussed under that heading;
  url = its official homepage or product page. One per vendor, no duplicates. A page of our
  own site ({own}) only when it shows the product itself (its interface, a pricing table, a
  feature screen): never our homepage or a landing page's top (a headline, a sign-up banner,
  a carousel), which read as an advert, not a picture.
- chart: only if the statistics above give >=3 real numbers or a clear comparison.
  Every value in ONE chart must share the SAME unit and the SAME period — never mix
  $/day with $/month, or ms with seconds. If units differ, skip the chart.
  Never chart RANKS or positions as bar lengths (rank 1 ≠ smallest quantity) — ranks
  belong in a list or table, not a chart.
- Skip anything that would be decorative. Fewer good visuals beat the budget.

- infographic: a branded explainer graphic that shows a STRUCTURE the reader takes in at a
  glance. Three templates:
  * flow — a process/pipeline the article explains step by step (3-6 steps)
  * decision — a "which option when" guide the article makes in prose/tables (3-5 branches)
  * stats — 2-4 headline numbers FROM THE KEY STATISTICS above (values verbatim)
  Never a picture of sentences: facts, definitions, a list of criteria or a taxonomy stay in
  the article as text or a table (a reader and a search engine both read them there).
  Content MUST be distilled from the article's own text (or the statistics above) — never
  invented. Labels <=6 words, descriptions <=12 words. At most 1-2, each for a major section.

Return ONLY JSON:
{{"insertions": [
  {{"type": "screenshot", "vendor": "vendor name EXACTLY as written in the article prose", "url": "https://...", "caption": "..."}},
  {{"type": "chart", "after_heading": "exact heading text from the list", "data": "label: value, label: value, ...", "caption": "..."}},
  {{"type": "infographic", "after_heading": "exact heading text from the list", "spec": {{"type": "flow|decision|stats", "title": "...", "subtitle": "optional", "source": "optional attribution",
     "steps": [{{"label": "...", "desc": "..."}}], "branches": [{{"if": "...", "then": "...", "detail": "..."}}], "items": [{{"value": "...", "label": "..."}}]}}}}
]}}
For screenshots the placement anchor is the vendor's first substantive mention in prose —
so only propose vendors the article actually discusses in sentences (not just table rows)."""
        data, _ = self.call_claude_json(prompt, max_tokens=2000)
        ins = (data or {}).get("insertions") or []

        # PRIORITY, not arrival order: in a roundup the per-product screenshots are what the
        # brief ordered and what the editor demands ("add one logo or screenshot to each of
        # Synthesia, Descript…"), yet they were competing for the last budget slots against
        # charts the model happened to list first. Reviewed-product screenshots go first.
        reviewed = {str(h).strip().lower()
                    for s in (state.brief or {}).get("sections", []) for h in (s.get("h3") or [])}

        def _priority(item):
            if item.get("type") != "screenshot":
                return 2
            v = (item.get("vendor") or "").strip().lower()
            return 0 if any(v and v in h for h in reviewed) else 1

        ins = sorted(ins, key=_priority)

        added = 0
        dropped: list[str] = []
        # A silent cap reads as "we placed everything worth placing" when it means "we ran out
        # of room" — the run that prompted this reported "proposed 9, placed 2" and never said
        # the other 6 were never even looked at.
        if len(ins) > budget:
            over = [f"{i.get('type')} {i.get('vendor') or i.get('after_heading') or '?'}"
                    for i in ins[budget:]]
            state.note(f"[visuals] budget {budget} (target {target}, {existing} already "
                       f"present) — {len(over)} proposal(s) not considered: "
                       + "; ".join(over[:6]))
        for item in ins[:budget]:
            typ = item.get("type")
            if typ == "screenshot":
                url = (item.get("url") or "").strip()
                vendor = (item.get("vendor") or "").strip()
                if not url.startswith("http") or not vendor or f'url="{url}"' in md:
                    dropped.append(f"screenshot {vendor or url or '?'}: bad url, no vendor, or "
                                   f"already present")
                    continue
                marker = f'<!-- visual:screenshot url="{url}" caption="{item.get("caption", url)}" -->'
                # anchor at the vendor's first prose mention — if the article never
                # discusses it in a sentence, the screenshot has no home: skip it
                md, ok = insert_after_mention(md, vendor, marker)
                if ok:
                    added += 1
                else:
                    dropped.append(f"screenshot {vendor}: the article never mentions it in prose")
            elif typ == "chart":
                head = (item.get("after_heading") or "").strip()
                dat = (item.get("data") or "").strip()
                if not head or not dat:
                    dropped.append(f"chart '{head or '?'}': no heading or no data")
                    continue
                resolved = resolve_heading(md, head)
                if not resolved:
                    dropped.append(f"chart: proposed heading '{head}' matches none in the article")
                    continue
                head = resolved
                marker = f'<!-- visual:chart data="{dat}" caption="{item.get("caption", "")}" -->'
                pat = re.compile(r"^(#{2,3}\s+" + re.escape(head) + r"\s*)\n(?!\n*(?:!\[|<!-- visual))",
                                 flags=re.M)
                new_md, n = pat.subn(r"\1\n\n" + marker.replace("\\", "\\\\") + "\n", md, count=1)
                if n:
                    md = new_md
                    added += 1
                else:
                    dropped.append(f"chart under '{head}': that heading already carries a visual")
            elif typ == "infographic":
                head = (item.get("after_heading") or "").strip()
                spec = item.get("spec") or {}
                # a picture of sentences (cards, a checklist) is the article's text, not a picture
                # (testers, 2026-10-20): only a flow, a decision or numbers are proposed here; a
                # person may still ask for the others in the Studio
                if not head or spec.get("type") not in ("flow", "decision", "stats"):
                    dropped.append(f"infographic '{head or '?'}': missing heading, or a template that is "
                                   f"text in boxes ({spec.get('type')!r})")
                    continue
                resolved = resolve_heading(md, head)
                if not resolved:
                    dropped.append(f"infographic: proposed heading '{head}' matches none in the "
                                   f"article")
                    continue
                head = resolved
                assets = Path(state.output_dir) / "assets"
                assets.mkdir(parents=True, exist_ok=True)
                n_idx = next_free(assets, "infographic")
                png = assets / f"infographic_{n_idx}.png"
                logo = self.project.brand.logo_file()   # None: the project has no logo
                built = infographic.render(spec, str(png), logo_png=str(logo) if logo else None,
                                           palette=self.project.brand.palette)
                if not built:
                    dropped.append(f"infographic under '{head}': the renderer produced nothing")
                    continue
                atomic_write_text(png.with_suffix(".json"), json.dumps({"producer": "infographic", "spec": spec}, ensure_ascii=False, indent=2))
                alt = spec.get("title", "infographic")
                embed = f"![{alt}](assets/infographic_{n_idx}.png)"
                pat = re.compile(r"^(#{2,3}\s+" + re.escape(head) + r"\s*)\n(?!\n*(?:!\[|<!-- visual))",
                                 flags=re.M)
                new_md, n = pat.subn(r"\1\n\n" + embed.replace("\\", "\\\\") + "\n", md, count=1)
                if n:
                    md = new_md
                    added += 1
                else:
                    dropped.append(f"infographic under '{head}': that heading already carries a "
                                   f"visual")
        # Silence here was the whole problem: every rejection above used to be a bare `continue`,
        # so "proposed seven, landed none" and "proposed nothing" printed the same thing (nothing).
        if ins or dropped:
            state.note(f"[visuals] enrichment proposed {len(ins)}, placed {added}"
                       + (f", dropped {len(dropped)}: " + "; ".join(dropped[:6]) if dropped else ""))
        elif budget > 0:
            state.note(f"[visuals] enrichment proposed nothing for a budget of {budget}")
        if added:
            state.draft_markdown = md
            state.note(f"[visuals] enrichment added {added} marker(s) (had {existing}, target {target})")

    # ---- LLM: markers + facts -> chart specs ----
    def _shoot(self, url: str, png: Path, caption: str | None, seen: list[int],
               selector: str | None = None, focus: list[str] | None = None) -> tuple[str | None, dict]:
        """Capture, then look at the capture (S3). A near-duplicate of a screenshot already in this
        article, or a look other than OK, is refused like any capture-time refusal: the file is
        removed and the label is the reason, so the fallback chain runs as usual. A look that
        names the section the caption is about (REFRAME) takes the shot again there, up to
        REFRAMES times; a frame it never accepts is a MISMATCH."""
        heading = None
        for attempt in range(1 + REFRAMES):
            diag: dict = {}
            built = screenshot.capture(url, str(png), selector=selector, diag=diag, focus=focus,
                                       remote=scrapfly.remote(self.config, (self.project.seo or {}).get("country")),
                                       **({"heading": heading} if heading else {}))
            if not built:
                return None, diag
            h = _dhash(png)
            if h is not None and any(bin(h ^ s).count("1") <= _DUP_BITS for s in seen):
                png.unlink(missing_ok=True)
                diag.update(reason="DUPLICATE", detail="near-identical to a screenshot already in this article")
                return None, diag
            diag["dhash"] = h
            if not ((self.config.get("pipeline") or {}).get("visuals") or {}).get("vision_check"):
                return built, diag
            label, why, named = self._vision_label(png, caption, diag.get("outline"),
                                                   own=self.project.brand.is_own(url))
            diag["vision"] = {"label": label, "why": why, **({"heading": named} if named else {})}
            if label == "REFRAME" and not selector and attempt < REFRAMES and named != heading:
                heading = named                 # the section it named: shot again there
                continue
            if label == "REFRAME":
                label = "MISMATCH"              # the frame it asked for never came
            if label != "OK":
                png.unlink(missing_ok=True)
                diag.update(reason=label, detail=f"vision check: {why}")
                return None, diag
            if heading:
                diag["heading"] = heading
            return built, diag
        return None, diag                       # not reached: the last attempt returns

    def _vision_label(self, png: Path, caption: str | None, outline: list[dict] | None = None,
                      own: bool = False) -> tuple[str, str, str]:
        """The look at a shot (ShotLook, the visuals' model): (label, why, heading to frame on).
        `own`: the page is our own site, shown only where it shows the product."""
        got = ShotLook(self.project, self.config).look(png, caption, outline, own=own)
        return got["label"], got["why"], got["heading"]

    def _chart_specs(self, state, chart_markers) -> dict:
        # the shared corpus view: a chart puts numbers straight into the article, so it draws on
        # exactly what the writer cited and the editor checks (it used to see the first 60 facts,
        # without sources). `_enrich` keeps its own short slice on purpose: it only PROPOSES
        # visuals, and every chart it proposes is built here, against the full corpus, with
        # skip=true when the numbers are not there.
        facts_block = self.render_corpus(state.research, state.output_dir)

        markers_block = "\n".join(
            f"MARKER {i}: {m.group(2).strip()[:400]}" for i, m in enumerate(chart_markers, 1)
        )

        user = f"""ARTICLE TOPIC: {state.topic}

CHART MARKERS the writer placed (with the data hints they wrote):
{markers_block}

RESEARCHED FACTS (the only permissible source of numbers):
{facts_block}

Produce a chart spec for each marker per your instructions (skip=true when the real
numbers aren't there). Return ONLY the JSON object."""

        def _parse(payload) -> dict:
            found = {}
            if isinstance(payload, dict):
                for c in payload.get("charts", []) or []:
                    idx = c.get("marker_index")
                    if isinstance(idx, int):
                        found[idx] = c
            return found

        # 3000 tokens stopped being enough for six specs once adaptive thinking began billing as
        # output: the JSON came back truncated, nothing parsed, and every chart in the article
        # was dropped with a one-line note that read like a deliberate skip. Budget for the whole
        # set, keep the raw text when it still fails, and retry — a lost chart is a bug, not a
        # decision, and it should never be indistinguishable from one.
        data, raw = self.call_claude_json(user, max_tokens=8000)
        specs = _parse(data)
        if not specs:
            atomic_write_text(Path(state.output_dir) / "04_visuals.chart-spec-failure.txt", raw or "(empty)")
            state.note("[visuals] chart specs did not parse — retrying once (raw saved)")
            data, raw = self.call_claude_json(user, max_tokens=12000)
            specs = _parse(data)

        if not specs:
            state.note(f"[visuals] chart specs unusable after retry — "
                       f"{len(chart_markers)} chart marker(s) dropped")
        else:
            missing = [i for i in range(1, len(chart_markers) + 1) if i not in specs]
            if missing:
                state.note("[visuals] no spec returned for chart marker(s) "
                           + ", ".join(str(i) for i in missing) + " — dropped")
        return specs
