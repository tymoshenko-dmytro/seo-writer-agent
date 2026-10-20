"""Interactive editor agents used by the web UI.

AIEditor — rewrites a selected article fragment per a user instruction (brand-voice
and no-new-facts guardrails come from the injected system prompt).

VisualProducerService — creates a visual at a point in the article (user prompt
optional: it infers from context which producer fits) and edits existing visuals from
feedback. Every generated asset gets a sidecar `<asset>.json` with {producer, spec} so
later edits re-render from a known spec instead of guessing.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from .base import BaseAgent
from .fix_routing import same_page
from .visual_director import REFRAMES, ShotLook, _focus_terms, next_free, page_title
from .. import research_sources
from ..producers import charts, infographic, screenshot, image_gen
from ..tools import scrapfly
from ..store import atomic_write_text, safe_words


def cover_headline(d: Path) -> str:
    """The words the article's cover carries: the headline its run recorded when it drew the
    cover, else what the run drew it from (the primary keyword, the brief's title, the topic),
    else the folder's name. A recompose read two files nothing writes and set the slug's words
    over every redrawn cover ("Best Video Remote…" for "Video Remote…", 2026-10-05)."""
    try:
        run = json.loads((d / "_run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        run = {}
    run = run if isinstance(run, dict) else {}
    drawn = next((v.get("headline") for v in run.get("visuals") or []
                  if isinstance(v, dict) and v.get("type") == "cover" and v.get("headline")), None)
    kr, brief = run.get("keyword_research"), run.get("brief")
    for s in (drawn, (kr if isinstance(kr, dict) else {}).get("primary_keyword"),
              (brief if isinstance(brief, dict) else {}).get("title"), run.get("topic")):
        if isinstance(s, str) and s.strip():
            return s.strip()
    return d.name.replace("-", " ")


def _bytes_sha(p: Path) -> str | None:
    import hashlib

    try:
        return hashlib.sha1(p.read_bytes()).hexdigest()
    except OSError:
        return None


MADE_KEPT = 12      # earlier pictures whose maker a sidecar remembers


def _written_words(words: dict, look: dict, diag: dict, url: str | None) -> dict:
    """The words of a kept shot that came with none: the look's, written from what it saw; when
    the look gave none, the page's own title, marked `needs_caption` so the Pictures tab asks the
    person for a caption (the operator, 2026-10-06: keep a good shot, ask for the words)."""
    alt, caption = (look.get("alt") or "") if look.get("looked") else "", (look.get("caption") or "") if look.get("looked") else ""
    if alt or caption:
        return {**words, "alt": alt or caption, "caption": caption or alt}
    from urllib.parse import urlparse

    title = (safe_words(page_title(diag.get("title"), url))
             or f"A page of {urlparse(url or '').hostname or 'the site'}")
    return {**words, "alt": title, "caption": title, "needs_caption": True}


def _kept_words(data: dict, words: dict) -> dict:
    """The producer's answer with the alt and the caption of the place whose shot was kept: its
    own words, one standing in for the other when it gave only one, never the words of a place
    that was refused, nor of the picture it replaces (RE99-02, RCEE-01, RFBC-01). A first place
    with no words at all leaves the picture's own alt and caption as they are."""
    alt, caption = str(words.get("alt") or ""), str(words.get("caption") or "")
    if not (alt or caption):
        return data if words is data else {**data, "alt": "", "caption": ""}
    return {**data, "alt": alt or caption, "caption": caption or alt, "needs_caption": bool(words.get("needs_caption"))}


def _origin(rec: dict) -> dict:
    """What a sidecar keeps under `made` for earlier bytes: what made them, and whether their
    caption was the page's title standing in for words (a Restore brings the request back, R98D-02)."""
    return {k: rec[k] for k in ("producer", "spec", "caption_from", "page_title") if k in rec}


def stand_ins(side: dict | None, sha: str | None = None, title: str | None = None) -> list[list[str]]:
    """The shots here whose caption was the page's title standing in for words, as [bytes, title]
    pairs, with this one added: kept through every later Re-capture, even one that gives the same
    bytes, so a Restore of such a shot asks for a caption again (R518-01); the newest few."""
    pairs = [p for p in (side or {}).get("stand_ins") or []
             if isinstance(p, list) and len(p) == 2 and all(isinstance(x, str) and x for x in p)]
    if sha and title:
        pairs = [p for p in pairs if p != [sha, title]] + [[sha, title]]
    return pairs[-MADE_KEPT:]


# the kinds a picture can be made as, by their producer, and how a prompt names each
KINDS = ("screenshot", "infographic", "chart", "image")
KIND_WORDS = {"screenshot": "screenshot", "infographic": "infographic", "chart": "chart", "image": "illustration"}


def made_record(side: dict | None, sha: str | None, d=None, rel: str | None = None) -> dict | None:
    """What made the bytes `sha` ({producer, spec}), by the sidecar: its own record when it
    describes them (or names no bytes: a sidecar from before `for`), else the one it kept for them
    under `made` when they were made here before (a Restore brings old bytes back, R597-02); else,
    given the article's folder and the picture, what a Revert noted of them (`remember_made`)."""
    if isinstance(side, dict):
        if not side.get("for") or side["for"] == sha:
            return side
        old = (side.get("made") or {}).get(sha or "")
        if isinstance(old, dict):
            return old
    if d is not None and rel and sha:
        rec = (_noted_for(d, rel).get("made") or {}).get(sha)
        return rec if isinstance(rec, dict) else None
    return None


# what made pictures whose sidecar a Revert put back as it was: kept beside the sidecars, by
# picture and bytes (R9F0-02)
MADE_FILE = "_made.json"


def _note_key(rel: str) -> str:
    return str(Path(rel).with_suffix(""))         # a picture and its sidecar: assets/chart_1


def _noted(d) -> dict:
    try:
        data = json.loads((Path(d) / MADE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _noted_for(d, rel: str) -> dict:
    """What a Revert noted of one picture: {"made": {bytes: origin}, "stand_ins": [[bytes, title]]}."""
    mine = _noted(d).get(_note_key(rel))
    return mine if isinstance(mine, dict) else {}


def noted_stand_ins(d, rel: str) -> list[list[str]]:
    """The shots here whose caption was their page's title, as a Revert noted them (RA3C-03)."""
    return stand_ins(_noted_for(d, rel))


def remember_made(d, rel: str, newer: dict) -> None:
    """What made the bytes a sidecar knew of (its own record and the ones it kept), noted before a
    Revert puts back the sidecar it replaced, as it was: those bytes stay in History, and a Restore
    of them is made again the way they were made, not by the file's name (R9F0-02). The newest few
    per picture."""
    later = [(k, v) for k, v in (newer.get("made") or {}).items() if isinstance(v, dict)]
    if newer.get("for") and newer.get("producer"):
        later.append((newer["for"], _origin(newer)))
    # the shots whose caption was their page's title: the same bytes may have had their own words
    # since, and only these pairs remember the title (RA3C-03)
    pairs = stand_ins(newer)
    if not later and not pairs:
        return
    noted = _noted(d)
    old = _noted_for(d, rel)
    made = {k: v for k, v in (old.get("made") or {}).items() if isinstance(v, dict)}
    for sha, rec in later:
        made.pop(sha, None)
        made[sha] = rec
    kept = stand_ins(old)
    kept = [p for p in kept if p not in pairs] + pairs
    noted[_note_key(rel)] = {"made": dict(list(made.items())[-MADE_KEPT:]), "stand_ins": kept[-MADE_KEPT:]}
    atomic_write_text(Path(d) / MADE_FILE, json.dumps(noted, ensure_ascii=False, indent=2))


def picture_kind(d: Path, rel: str) -> str:
    """What makes the picture at `rel` now: "cover"; "illustration" (drawn from a prompt by the
    image model: a picture its sidecar says so of, an image_ file the producer made or a screenshot
    or a chart the person had drawn as an illustration, while the file is still that drawing);
    "photo" (an image_ file without one: an edit by the image model); or its name's kind
    ("chart", "infographic", "screenshot"). The sidecar records the bytes it describes (`for`) and
    what made the earlier bytes it made (`made`): after a Restore the record of the restored bytes
    decides; bytes it never made (an upload) go by the name (RD45-03, R597-02)."""
    p = Path(d) / rel
    name = p.name
    if name == "cover.png":
        return "cover"
    by_name = "photo" if name.startswith("image_") else name.split("_")[0]
    try:
        side = json.loads(p.with_suffix(".json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        side = None
    rec = made_record(side, _bytes_sha(p), d, rel)
    # what made these bytes, while the file is what that record describes: a picture made again as
    # another kind keeps its file's name (the operator, 2026-10-07: Redo as an infographic)
    maker = (rec or {}).get("producer")
    if maker == "image":
        return "illustration"
    return maker if maker in ("chart", "infographic", "screenshot") else by_name


def cover_subject(d: Path) -> str:
    """What the cover's photo is about: the run's topic, else the brief's title, else the headline."""
    try:
        run = json.loads((d / "_run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        run = {}
    run = run if isinstance(run, dict) else {}
    brief = run.get("brief") if isinstance(run.get("brief"), dict) else {}
    for s in (run.get("topic"), brief.get("title")):
        if isinstance(s, str) and s.strip():
            return s.strip()
    return cover_headline(d)


class AIEditor(BaseAgent):
    name = "ai_editor"

    def edit(self, selection: str, instruction: str, before: str = "", after: str = "") -> str:
        user = f"""INSTRUCTION FROM THE EDITOR:
{instruction}

CONTEXT BEFORE THE SELECTION (do not rewrite):
…{before[-700:]}

SELECTED FRAGMENT (rewrite THIS, return only its replacement):
{selection}

CONTEXT AFTER THE SELECTION (do not rewrite):
{after[:700]}…"""
        out = self.call_claude(user, max_tokens=4000).strip()
        m = re.match(r"^```(?:markdown|md)?\s*(.*?)```\s*$", out, re.DOTALL)
        return m.group(1).strip() if m else out


class VisualProducerService(BaseAgent):
    name = "visual_producer"
    model_role = "visuals"      # models.visuals when the profile sets it, else the worker's

    # ---------- helpers ----------
    def _research(self, d: Path) -> dict:
        if (d / "_research.json").exists():
            return json.loads((d / "_research.json").read_text())
        if (d / "_run.json").exists():
            return json.loads((d / "_run.json").read_text()).get("research") or {}
        return {}

    def _facts_block(self, d: Path) -> str:
        r = self._research(d)
        L = []
        for s in (r.get("key_statistics") or [])[:15]:
            L.append(f"- {s.get('stat')}: {s.get('value','')}")
        for f in (r.get("findings") or []):
            for fact in (f.get("facts") or [])[:2]:
                v = f" = {fact['value']}" if fact.get("value") else ""
                L.append(f"- {fact.get('claim','')}{v}")
        # what the project's own research sources give this article, and the pages they leave out
        # of it (core/research_sources.py): a picture made or made again here reads what the
        # article's first charts read
        own = research_sources.corpus_blocks(d, self.project)
        srcs = [f"- {s.get('title') or ''} | {s.get('url')}" for s in (r.get("sources") or [])
                if not research_sources.leaves_out(d, (s or {}).get("url"), self.project)][:15]
        if not (L or srcs or own):
            return "(no research facts)"
        return ("FACTS (numbers may only come from here):\n" + "\n".join(L[:40])
                + (f"\n\n{own}" if own else "")
                + "\n\nSOURCES (screenshot-able URLs):\n" + "\n".join(srcs))

    def _render(self, d: Path, producer: str, spec: dict, out_png: Path, focus_text: str = "",
                diag: dict | None = None) -> str | None:
        if producer == "chart":
            # the project's chart style when it names one that is a file: without one, the project's
            # folder itself was passed as the style and every chart made here failed to render
            style = self.project.root / self.project.chart_style if self.project.chart_style else None
            return charts.build(spec, str(out_png), style_path=str(style) if style and style.is_file() else None)
        if producer == "infographic":
            logo = self.project.brand.logo_file()   # None: the project has no logo
            return infographic.render(spec, str(out_png), logo_png=str(logo) if logo else None,
                                      palette=self.project.brand.palette)
        if producer == "screenshot":
            url = (spec or {}).get("url", "")
            if not url.startswith("http"):
                return None
            if research_sources.leaves_out(d, url, self.project):
                # a page the project's own research source leaves out of this article is never
                # captured, whichever place offered it
                if diag is not None:
                    diag["reason"] = "left out of this article by a research source"
                return None
            # never the whole page: a support article captured whole is several screens of text
            # in the post (the Zoom article). The capture is one screen, scrolled to the block
            # the caption talks about (the visual director's focus); without the focus it was the
            # top of the page, a header and a nav bar under a caption about something else
            return screenshot.capture(url, str(out_png), selector=spec.get("selector") or None,
                                      focus=_focus_terms(str(spec.get("focus") or focus_text or "")) or None,
                                      remote=scrapfly.remote(self.config, (self.project.seo or {}).get("country")),
                                      diag=diag, heading=str(spec.get("heading") or "") or None)
        if producer == "image":
            return image_gen.generate((spec or {}).get("prompt", ""), str(out_png))
        return None

    _PREFIX = {"chart": "chart", "infographic": "infographic", "screenshot": "screenshot", "image": "image"}

    def _recapture(self, d: Path, p: Path, spec: dict, data: dict, alt: str, feedback: str, diag: dict,
                   refused: list[str], avoid: list[str] | None = None,
                   old_url: str | None = None) -> tuple[dict, dict] | None:
        """Each place in turn (the producer's spec, then up to two candidates) until a shot shows
        what was asked. The producer looks at its own shot (visual_director.look_at_shot, with the
        page's sections and the person's instruction) and keeps it, takes it again on the section
        it names, or goes to the next place, saying why (`refused`). Without a usable look, the
        probe finding the named heading decides, and the last shot taken is kept, as before
        (RD45). Each shot is taken beside the picture and put in its place only when kept: one the
        look refused never replaces it. Returns (spec, words) of the shot kept, or None; `diag`
        holds the last capture's account. A place that shows a page in `avoid` (a page the
        article shows already) is never shot."""
        tries = [(spec, data)] + [
            ({**spec, "url": c["url"], "heading": c.get("heading") or "", "focus": c.get("focus") or c.get("heading") or ""}, c)
            for c in (data.get("candidates") or [])[:2]
            if isinstance(c, dict) and str(c.get("url") or "").startswith("http")]
        # a page the article shows already is never shot again, by a candidate no more than the
        # first place (RE99-03)
        shown = [t for t in tries if any(same_page(t[0].get("url"), u) for u in avoid or [])]
        tries = [t for t in tries if t not in shown]
        if shown and not tries:
            refused.append(f"the article already shows {shown[0][0].get('url')}")
        looks = bool(((self.config.get("pipeline") or {}).get("visuals") or {}).get("vision_check"))
        trial = p.with_name(p.stem + ".next" + p.suffix)
        took = None
        try:
            for t_spec, t_words in tries:
                heading, settled = t_spec.get("heading") or "", False
                for attempt in range(1 + REFRAMES):
                    diag.clear()
                    t = {**t_spec, "heading": heading}
                    # the place's own words; the picture's current ones only for the first place on
                    # the picture's own page (RCEE-01). A place with no words of its own anywhere
                    # else gets them from the look that sees its shot, never from another page
                    own = t_words.get("caption") or t_words.get("alt") or ""
                    same = t_words is data and bool(alt) and (not old_url or same_page(t.get("url"), old_url))
                    need_words = not own and not same
                    words = own or (alt if same else "") or feedback
                    if not self._render(d, "screenshot", t, trial, diag=diag, focus_text=t.get("focus") or words):
                        break
                    # a shot of our own site is kept only where it shows the product (MARKETING),
                    # on every place it takes: the first, a candidate, a new frame (Codex RA5D-02)
                    look = (ShotLook(self.project, self.config).look(trial, None if need_words else words,
                                                                     diag.get("outline"), want=feedback or None,
                                                                     words=need_words,
                                                                     own=self.project.brand.is_own(t.get("url")))
                            if looks else {"looked": False})
                    if look.get("looked") and look["label"] == "REFRAME" and attempt < REFRAMES and look["heading"] != heading:
                        heading = look["heading"]       # the section it named: shot again there
                        continue
                    if look.get("looked") and look["label"] != "OK":
                        refused.append(look.get("why") or look["label"].lower())
                        break
                    if trial.exists():      # a capture that says it took the shot wrote it there
                        os.replace(trial, p)
                    took = (t, _written_words(t_words, look, diag, t.get("url")) if need_words else t_words)
                    settled = look.get("looked") or not heading or bool((diag.get("focus") or {}).get("heading"))
                    break
                if settled:
                    break
        finally:
            trial.unlink(missing_ok=True)
        return took

    # the producer's budget: on Opus 5.5 its thinking spends output tokens, and 2500 (5000 on the
    # retry) cut its JSON mid-spec, read as "no spec" (the operator's live article, 2026-10-07)
    PRODUCER_TOKENS = 8000

    def _producer_json(self, user: str) -> tuple[dict | None, str]:
        """The producer's JSON answer: asked once more when the first does not parse, and the
        whole answer logged when the second does not either (the card says it plainly)."""
        data, raw = self.call_claude_json(user, max_tokens=self.PRODUCER_TOKENS)
        if not isinstance(data, dict):
            data, raw = self.call_claude_json(
                user + "\n\nYour last answer was not one complete JSON object. Return ONLY the JSON.",
                max_tokens=self.PRODUCER_TOKENS)
        if not isinstance(data, dict):
            logging.getLogger(__name__).warning("picture producer: no usable JSON in: %s", str(raw))
            return None, str(raw or "")
        return data, str(raw or "")

    def _pages_block(self, url: str | None, d: Path | None = None) -> str:
        """What a re-capture reads before it names a section: the current page's headings, each
        with the start of its text, and the project's own pages. It guessed a focus phrase without
        seeing the page, and three re-captures in a row came back as the page's header (2026-10-06).
        A page the article's research sources leave out is neither read nor offered (`d`, the
        article's folder)."""
        def left_out(u):
            return d is not None and research_sources.leaves_out(d, u, self.project)

        L = []
        if url and url.startswith("http") and not left_out(url):
            heads = screenshot.outline(url)
            if heads:
                L.append(f"PAGE OUTLINE of {url} (its headings, each with the start of the text under it):")
                L += [f"- {h['heading']}: {h.get('text') or ''}" for h in heads[:30]]
            else:
                L.append(f"PAGE OUTLINE of {url}: (could not be read)")
        try:
            from ..tools.link_index import LinkIndex

            idx = LinkIndex.load(self.project.root)
            own = [t for t in (idx.targets if idx else [])
                   if getattr(t, "kind", "page") != "post" and not left_out(t.url)][:40]
        except Exception:  # noqa: BLE001 — the pages are a hint
            own = []
        if own:
            L.append("OUR OWN PAGES (url | title), for a section this page does not have:")
            L += [f"- {t.url} | {t.title}" for t in own]
        return "\n".join(L)

    # ---------- create ----------
    def create(self, article_dir: str | Path, instruction: str, before: str, after: str,
               avoid_urls: list[str] | None = None) -> dict:
        """A new visual for the spot between `before` and `after`. A screenshot of a page in
        `avoid_urls` (the pages the article already shows) is refused before it is taken."""
        d = Path(article_dir)
        # the project's own research sources decide for this article before anything here reads
        # them (an article written or imported before they did)
        research_sources.decide(d, self.project)
        assets = d / "assets"
        assets.mkdir(parents=True, exist_ok=True)

        user = f"""ARTICLE CONTEXT — the visual goes between these two passages:

BEFORE:
…{before[-900:]}

AFTER:
{after[:900]}…

USER REQUEST: {instruction or "(none — decide yourself what best serves this spot)"}

{self._facts_block(d)}
{self._pages_block(None, d)}

Pick the producer, build the spec per your instructions. Return ONLY the JSON."""
        data, _ = self._producer_json(user)
        if not data or data.get("producer") not in self._PREFIX:
            return {"error": "the picture producer gave no usable answer"}

        producer, spec = data["producer"], data.get("spec") or {}
        if producer == "screenshot" and not data.get("candidates") and \
                any(same_page(spec.get("url"), u) for u in avoid_urls or []):
            return {"error": f"the article already shows {spec.get('url')}"}       # nothing else to try
        prefix = self._PREFIX[producer]
        n = next_free(assets, prefix)
        png = assets / f"{prefix}_{n}.png"
        if producer == "screenshot":
            # the producer looks at its own shot as in a Re-capture, and frames the section asked
            # for; the words are those of the place it kept (RE99-02)
            diag, refused = {}, []
            took = self._recapture(d, png, spec, data, data.get("alt") or "", instruction, diag, refused,
                                   avoid=avoid_urls)
            if not took:
                why = refused[-1] if refused else " ".join(str(x) for x in (diag.get("reason"), diag.get("detail")) if x)
                return {"error": f"no shot showed what was asked ({(why or 'no answer from the page')[:200]})"}
            spec, data = took[0], _kept_words(data, took[1])
        elif not self._render(d, producer, spec, png,
                              focus_text=spec.get("focus") or data.get("caption") or data.get("alt") or instruction):
            return {"error": f"{producer} render failed"}

        sha, title = _bytes_sha(png), str(data.get("caption") or "") if data.get("needs_caption") else ""
        atomic_write_text(assets / f"{prefix}_{n}.json", json.dumps(
            {"producer": producer, "spec": spec, "for": sha,
             **({"caption_from": "page_title", "page_title": title, "stand_ins": stand_ins(None, sha, title)}
                if title else {})}, ensure_ascii=False, indent=2))
        alt = safe_words(data.get("alt") or data.get("caption") or spec.get("title") or "") or producer
        caption = safe_words(data.get("caption") or "")
        if producer == "screenshot":
            caption = self.project.brand.caption_for(caption or alt, str(spec.get("url") or ""))
        md = f"![{alt}](assets/{prefix}_{n}.png)" + (f"\n*{caption}*" if caption else "")
        return {"markdown": md, "path": f"assets/{prefix}_{n}.png", "producer": producer}

    # ---------- edit ----------
    def edit_visual(self, article_dir: str | Path, rel_path: str, feedback: str,
                    alt: str = "", as_image: bool = False, kind: str | None = None) -> dict:
        """The picture at `rel_path` made again from `feedback`, in place (the same file): a chart or
        an infographic redrawn from its spec, a screenshot re-captured, the cover drawn afresh, a
        photo edited. With `kind` it is made as that kind instead ("screenshot", "infographic",
        "chart", "image": an illustration, as `as_image`), or "auto": the producer chooses the kind
        that shows what is asked (the operator, 2026-10-07: one Redo, the producer decides; a menu
        to pick). The file keeps its name; the sidecar says what made it. {"ok", "path", "alt",
        "caption"} or {"error"}."""
        d = Path(article_dir)
        research_sources.decide(d, self.project)       # as in create(), before any read of them
        p = (d / rel_path).resolve()
        if not p.exists() or (d / "assets").resolve() not in p.parents:
            return {"error": "asset not found"}
        name = p.name

        # the cover: drawn again, as the first draw made it (a new background from the article's
        # subject, the run's headline and the logo set over it). An edit of the old background
        # keeps what it had: the live run's feedback named the headline, the image model painted
        # it into the background, and the next edit kept it under the new headline (2026-10-05/06)
        if name == "cover.png":
            from .visual_director import build_cover

            built, why = build_cover(self.project, self.config, d, cover_headline(d), cover_subject(d),
                                     attempts=1, fresh=True)
            return {"ok": True, "path": rel_path} if built else {"error": f"the cover could not be drawn: {why}"}
        sidecar = p.with_suffix(".json")
        try:
            side = json.loads(sidecar.read_text()) if sidecar.exists() else None
        except ValueError:
            side = None
        side = side if isinstance(side, dict) else None
        old_sha = _bytes_sha(p)
        made_by = picture_kind(d, rel_path)
        want = "image" if as_image else (kind or None)
        if want not in (None, "auto", *KINDS):
            return {"error": f"cannot make a picture as '{want}'"}
        if made_by == "photo" and want is None:            # a photo: the image model edits it
            if not image_gen.edit(str(p), feedback):
                return {"error": "image edit failed"}
            return {"ok": True, "path": rel_path}
        now = "image" if made_by in ("illustration", "photo") else made_by
        kind = now if want in (None, "auto") else want
        if kind not in KINDS:
            return {"error": f"cannot edit '{name}'"}
        # the spec of what it is now: the record of these bytes (the sidecar's own, or the one it kept
        # for them, R597-02), or the screenshot's or the chart's an illustration replaced (kept
        # under "was" when it was drawn, so a Restore gets its page back)
        cur = made_record(side, old_sha, d, rel_path)
        if cur and cur.get("producer") == kind:
            old_spec = cur
        elif side and (side.get("was") or {}).get("producer") == kind:
            old_spec = side["was"]
        else:
            old_spec = None

        draw = bool(os.getenv("OPENAI_API_KEY"))
        if want == "auto":
            ask = ("The user wants this picture MADE AGAIN, and its kind is yours to choose: whatever "
                   "shows what is asked best. A screenshot of a real page (producer \"screenshot\"), an "
                   "infographic (\"infographic\"), a chart of numbers from the facts (\"chart\")"
                   + (", or an illustration drawn by the image model (\"image\")" if draw else
                      "; not an illustration: no image model is set up")
                   + f". It is a {KIND_WORDS[now]} now; keep that kind when it still serves.")
        elif kind == "image":
            ask = (f"The user wants this {KIND_WORDS[now]} DRAWN AS AN ILLUSTRATION instead (producer "
                   f"\"image\"): what it must show is below; the caption says it is an illustration.")
        elif kind != now:
            ask = (f"The user wants this {KIND_WORDS[now]} MADE AS {'AN' if kind == 'infographic' else 'A'} "
                   f"{kind.upper()} instead (producer \"{kind}\"): what it must show is below.")
        else:
            ask = f"The user wants to CHANGE an existing {KIND_WORDS[kind]} in the article."
        shot = ""
        if kind == "screenshot" or want == "auto":
            shot = self._pages_block(str(((old_spec or {}).get("spec") or {}).get("url") or ""), d) + f"""

{"If you choose a screenshot" if want == "auto" else "For this screenshot"}, read the outline: `heading` is the exact heading of the section that shows
what is asked (copied from an outline); `focus` the words that section shows. If this page has no
such section, take the page that has it from OUR OWN PAGES or the SOURCES. Add "candidates": up to
two more {{"url", "heading", "caption", "alt"}} to try in order when the first does not show it."""
        user = f"""{ask}

USER FEEDBACK: {feedback}

CURRENT VISUAL: alt text = "{alt or name}"
{"CURRENT SPEC: " + json.dumps(old_spec["spec"], ensure_ascii=False) if old_spec else "(no stored spec — rebuild it from the alt text, feedback and facts)"}

{self._facts_block(d)}
{shot}

{"Return ONLY the JSON with the producer you chose and its spec." if want == "auto" else
 f'Return ONLY the JSON with producer "{kind}" and the UPDATED spec applying the feedback.'}"""
        data, _ = self._producer_json(user)
        spec = (data or {}).get("spec") or {}
        if not spec:
            return {"error": "the picture producer gave no usable answer"}
        if want == "auto":
            chosen = (data or {}).get("producer")
            if chosen not in KINDS or (chosen == "image" and not draw):
                return {"error": "the picture producer chose no kind it can make"}
            if chosen != kind:          # another kind: its own spec, not the current one's
                old_spec = (cur if cur and cur.get("producer") == chosen
                            else side["was"] if side and (side.get("was") or {}).get("producer") == chosen else None)
            kind = chosen
        # made as another kind: the old picture's words must not stay under it (a screenshot's
        # caption links the page it showed, R058-04). An infographic, a chart or an illustration
        # takes the producer's words, one standing in for the other; none, and nothing is made. A
        # screenshot gets its words from the page it shows, never from the picture it replaces
        changing = kind != now
        if changing and kind != "screenshot":
            words = [safe_words(str((data or {}).get(k) or "")) for k in ("alt", "caption")]
            if not any(words):
                return {"error": f"the new {KIND_WORDS[kind]} came without a caption: nothing changed, "
                                 f"try again"}
            data = {**data, "alt": words[0] or words[1], "caption": words[1] or words[0]}
        if kind == "chart" and not charts.plotted_values(spec):
            # the producer's way of saying "take it out" was an empty frame titled "(Removed)";
            # the old chart stays, and removing it is a text edit a person or the fixer makes
            return {"error": "the new chart has nothing to plot; if the chart should go, remove it "
                             "from the text instead"}
        diag: dict = {}
        refused: list[str] = []
        if kind == "screenshot":
            took = self._recapture(d, p, spec, data or {}, "" if changing else alt, feedback, diag, refused,
                                   old_url=str(((old_spec or {}).get("spec") or {}).get("url") or "") or None)
            if took:
                spec, data = took[0], _kept_words(data or {}, took[1])
        if (kind == "screenshot" and not took) or (kind != "screenshot" and not self._render(
                d, kind, spec, p, diag=diag, focus_text=spec.get("focus") or (data or {}).get("caption") or alt or feedback)):
            # why, in the person's words: "screenshot re-render failed" said nothing (2026-10-06)
            if kind == "screenshot" and refused:
                return {"error": f"no shot showed what was asked ({refused[-1][:200]}): give another "
                                 f"instruction, or draw it as an illustration"}
            if kind == "screenshot":
                why = " ".join(str(x) for x in (diag.get("reason"), diag.get("detail")) if x) or "no answer from the page"
                return {"error": f"the page could not be captured ({why[:200]}): try again, "
                                 f"give an instruction, or draw it as an illustration"}
            return {"error": f"the {kind} could not be made ({'the image model gave no picture' if kind == 'image' else 'the render failed'})"}
        record = {"producer": kind, "spec": spec, "for": _bytes_sha(p)}
        title = str((data or {}).get("caption") or "") if (data or {}).get("needs_caption") else ""
        if title:
            # no words came for this shot: the Pictures tab asks, while its caption is still this title
            record["caption_from"], record["page_title"] = "page_title", title
        if pairs := stand_ins(side, record["for"], title):
            record["stand_ins"] = pairs
        # made as another kind: what it was (its page, its numbers) is kept, so a Redo back to that
        # kind starts from it
        if cur and cur.get("producer") and cur.get("producer") != kind:
            record["was"] = {"producer": cur.get("producer"), "spec": cur.get("spec")}
        elif side and side.get("was") and (side.get("was") or {}).get("producer") != kind:
            record["was"] = side["was"]
        # what made each earlier picture here, by its bytes: a Restore of any of them is made
        # again the way it was made (R597-02); the newest few are kept
        made = dict((side or {}).get("made") or {})
        if side and side.get("for"):    # the bytes it described, which a Restore may have replaced
            made[side["for"]] = _origin(side)
        if cur and old_sha:
            made[old_sha] = _origin(cur)
        made.pop(record["for"] or "", None)
        if made:
            record["made"] = dict(list(made.items())[-MADE_KEPT:])
        atomic_write_text(sidecar, json.dumps(record, ensure_ascii=False, indent=2))
        # the new picture's own words: the article's alt text and caption must describe it, not
        # the chart it replaced
        caption = safe_words((data or {}).get("caption") or "")
        if kind == "screenshot" and caption:
            caption = self.project.brand.caption_for(caption, str(spec.get("url") or ""))
        return {"ok": True, "path": rel_path, "alt": safe_words((data or {}).get("alt") or ""),
                "caption": caption, **({"needs_caption": True} if (data or {}).get("needs_caption") else {}),
                **({"changed_kind": True} if changing else {})}
