"""What the machine changes in the working version, what waits for the person's choice, and how
a change is taken back.

After a review, the machine applies the findings on the safe list (qa_model.route), each held to
its target:

    edit            the paragraph that holds the quoted words; nothing outside it changes
    cite            a link to a researched source at the quoted claim; the claim's words stay
    insert_keyword  the phrase worked into the named section's existing sentences
    redraw/recapture the picture file, its alt text and caption

A change that reaches outside its target is not kept: the finding becomes a proposal
(`needs_choice`). The fixer and its guards (agents/fix_applier) make every text edit, so its two
rules hold here too: the text is built only from the edit that passed the guards, and only the
asked-for phrase is required. The primary keyword's count and density are machine changes on
targets the code picks: the sections that lack the phrase, the paragraphs that repeat it most.

Every change is a group: the findings it was made for, what each place looked like before and
after, and the files it changed with copies of their previous bytes (`_kept/<group>/`, the
article's text among them). The latest group, while nothing has changed after it, is reverted in
one step; a picture group (a redraw or a re-capture) also on its own, whatever came after, while
its picture and its caption are as it left them. The check of a group (agents/edit_verifier, the
picture check) is a hint beside it: fixed / still_there / not_checked; it changes no status and
starts nothing.

Every action carries the version the page showed (and a proposal its review). Under the
article's lock a different version changes nothing and raises Stale (contract 1).
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import math
import re
import shutil
from contextlib import contextmanager
from pathlib import Path

from . import keyword_report as kr
from . import qa_model as qm
from . import store, usage
from .store import Article, schema_of, split_frontmatter

KEPT = "_kept"
CITE_MAX_NEW_WORDS = 12          # "according to [the 2026 survey](url)" and no more
KEYWORD_MAX_NEW_WORDS = 12       # beyond the phrase itself
_PARA = ("paragraph_open", "heading_open", "tr_open", "fence", "code_block", "html_block", "hr")
_LINKED = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")


class Stale(RuntimeError):
    """The page showed another version of the article, or another review: nothing was changed.
    The person loads the current page and decides again."""


# ---------------------------------------------------------------------------------------------
# the records
# ---------------------------------------------------------------------------------------------
def records(art: Article) -> tuple[dict, dict]:
    """(the run record, its `review` part), the run upgraded to the model first when it is older
    (core/qa_migration; under the caller's lock, which is re-entrant)."""
    from .qa_migration import RUN_SCHEMA, migrate_article

    run = art.run()
    if not run or schema_of(run) < RUN_SCHEMA:
        migrate_article(art)
        run = art.run()
    rv = run.setdefault("review", {})
    for k in ("reviews", "findings", "changes"):
        rv.setdefault(k, [])
    return run, rv


_FOLLOWS: contextvars.ContextVar = contextvars.ContextVar("follows", default=None)


@contextmanager
def following(page_version: str | None, left: str | None):
    """A job queued behind others on the same article runs on what they left: inside this, the
    version the page showed (`page_version`) stands for `left`, the version those jobs ended at.
    The runner says which (ui/jobs.JobRunner.submit_on_article); a change made by anything else
    still makes the action stale."""
    token = _FOLLOWS.set((page_version, left) if page_version and left and left != page_version else None)
    try:
        yield
    finally:
        _FOLLOWS.reset(token)


def check_version(art: Article, version: str | None) -> str:
    """The version on disk, when it is the one the page showed (or the one the jobs queued before
    this one left, `following`); Stale otherwise."""
    now = art.version()
    if not version or (version != now and _FOLLOWS.get() != (version, now)):
        raise Stale(f"'{art.slug}' changed after the page was opened: load it again and decide on what is there now")
    return now


def latest_review(rv: dict) -> dict | None:
    return (rv.get("reviews") or [None])[-1]


def finding_by_id(rv: dict, fid: str) -> dict | None:
    return next((f for f in rv.get("findings") or [] if f.get("id") == fid), None)


def _current(rv: dict, fid: str) -> dict:
    """An open finding of the latest review, or Stale: a button of a replaced checklist does
    nothing (review R829-03)."""
    f = finding_by_id(rv, fid)
    if f is None or f.get("review") != (latest_review(rv) or {}).get("id"):
        raise Stale(f"{fid} is not on the current checklist: a later review replaced it; look at the page again")
    if f.get("status") != "open":
        raise Stale(f"{fid} is not open any more: look at the page again")
    return f


def subject_of(art: Article, f: dict) -> str | None:
    """What a finding's target holds now: the picture's bytes, the section's text, the quoted
    words. A decision about a finding holds only while its subject is the same (review R829-04)."""
    t = f.get("target") or {}
    if "asset" in t:
        return art.pictures().get(t["asset"]) or "missing"
    if "heading" in t:
        body = split_frontmatter(art.text())[1]
        sec = next((x for x in sections(body) if x["heading"] == t["heading"]), None)
        return hashlib.sha1(body[sec["start"]:sec["end"]].encode("utf-8")).hexdigest()[:16] if sec else "missing"
    return t.get("quote")


def latest_group(rv: dict) -> dict | None:
    return next((g for g in reversed(rv.get("changes") or []) if not g.get("reverted")), None)


def _next_group_id(rv: dict) -> str:
    n = [int(m.group(1)) for g in rv.get("changes") or [] if (m := re.fullmatch(r"c(\d+)", str(g.get("id"))))]
    return f"c{max(n, default=0) + 1}"


# ---------------------------------------------------------------------------------------------
# where a target sits
# ---------------------------------------------------------------------------------------------
def _line_starts(text: str) -> list[int]:
    out, at = [0], 0
    for line in text.split("\n"):
        at += len(line) + 1
        out.append(min(at, len(text)))
    return out


def _span(starts: list[int], m: list[int], size: int) -> tuple[int, int]:
    a, b = m
    return starts[min(a, len(starts) - 1)], (starts[b] if b < len(starts) else size)


def paragraphs(body: str) -> list[tuple[int, int]]:
    """Every block of the body as the page renders it (a paragraph, a heading, a table row, a code
    block), as (start, end) offsets: the bounds a text change keeps to."""
    from .tools.markdown_render import parse

    starts = _line_starts(body)
    return [_span(starts, t.map, len(body)) for t in parse(body) if t.type in _PARA and t.map]


def paragraph_of(body: str, start: int, end: int) -> tuple[int, int] | None:
    """The smallest block that holds [start, end); None when the span crosses blocks."""
    inside = [(a, b) for a, b in paragraphs(body) if a <= start and end <= b]
    return min(inside, key=lambda s: s[1] - s[0]) if inside else None


def sections(body: str) -> list[dict]:
    """The body's sections: {"heading", "level", "start", "end"}, a section running to the next
    heading of its level or higher, read with the page's parser."""
    from .tools.markdown_render import parse

    toks, starts = parse(body), _line_starts(body)
    heads = []
    for i, t in enumerate(toks):
        if t.type == "heading_open" and t.map and i + 1 < len(toks):
            heads.append((int(t.tag[1:]), _span(starts, t.map, len(body))[0], " ".join(toks[i + 1].content.split())))
    out = []
    for n, (level, start, title) in enumerate(heads):
        end = next((s for lv, s, _ in heads[n + 1:] if lv <= level), len(body))
        out.append({"heading": title, "level": level, "start": start, "end": end})
    return out


def _changed(old: str, new: str) -> tuple[int, int, int]:
    """(start, old_end, new_end): the one stretch where `new` differs from `old`."""
    n = min(len(old), len(new))
    p = 0
    while p < n and old[p] == new[p]:
        p += 1
    s = 0
    while s < n - p and old[-1 - s] == new[-1 - s]:
        s += 1
    return p, len(old) - s, len(new) - s


def _within(old: str, new: str, bound: tuple[int, int]) -> bool:
    p, old_end, _ = _changed(old, new)
    return old == new or (bound[0] <= p and old_end <= bound[1])


def _plain(text: str) -> str:
    return _LINKED.sub(lambda m: m.group(1), text or "")


def _only_added(before: str, after: str, most: int) -> bool:
    """Whether `after` says every word of `before` in its order (a link's markup aside), with at
    most `most` words added: a count of words let a citation swap who pays what (RE99-04)."""
    import difflib

    b, a = _plain(before).split(), _plain(after).split()
    ops = difflib.SequenceMatcher(None, b, a, autojunk=False).get_opcodes()
    return all(op in ("equal", "insert") for op, *_ in ops) and len(a) - len(b) <= most


def _count(text: str, phrase: str) -> int:
    return len(re.findall(r"(?<![\w])" + re.escape(phrase) + r"(?![\w])", text or "", flags=re.I))


def _fm_paths(value, path=()):
    """(path, string) for every string in the frontmatter's fields."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _fm_paths(v, path + (k,))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _fm_paths(v, path + (i,))


def _set_path(value, path, new):
    if not path:
        return new
    head, rest = path[0], path[1:]
    if isinstance(value, dict):
        return {**value, head: _set_path(value[head], rest, new)}
    out = list(value)
    out[head] = _set_path(value[head], rest, new)
    return out


# ---------------------------------------------------------------------------------------------
# one action's working copy
# ---------------------------------------------------------------------------------------------
def seed_state(art: Article, block: str, body: str, run: dict, project):
    """The pipeline's state rebuilt from an article's folder, for a review and its changes: the
    brief, the research and the keyword research its run recorded, the text, the keyword
    coverage measured on it, the visuals manifest. The run record is the only source."""
    from .workflow_state import ArticleState

    brief = run.get("brief") or {}
    research = run.get("research") or {}
    kr = run.get("keyword_research") or {}
    state = ArticleState(topic=run.get("topic") or brief.get("title") or art.slug, slug=art.slug)
    state.output_dir = str(art.dir)
    state.brief, state.research, state.keyword_research = brief, research, kr
    # the project's own research sources decide for an article written before they did (their
    # decision is kept in its folder, so the review reads what the writer read)
    from .research_sources import gate_all

    gate_all(art.dir, state.topic, kr.get("primary_keyword") or "", project)
    state.draft_markdown = body
    try:
        from .keyword_report import analyze, thresholds_from

        state.keywords = analyze(body, kr, brief, thresholds_from(project.seo))
    except Exception as e:  # noqa: BLE001 — the fixer is told "(not measured)" instead
        state.note(f"[review] keyword coverage unavailable: {e}")
    state.target_words = brief.get("target_words") or 0
    # the visuals manifest carries refused screenshots the review keeps raising until filled
    state.visuals = run.get("visuals") or []
    state.frontmatter = store.frontmatter_fields(block)
    return state


class Work:
    """The article as one action changes it: the frontmatter block and the body, the state the
    fixer reads its context from, and the groups this action made."""

    def __init__(self, art: Article, run: dict, rv: dict, config: dict, project):
        from .agents import fix_context
        from .agents.fix_applier import FixApplier

        if not art.path("article.md").is_file():
            raise FileNotFoundError(f"no article.md in {art.dir}")
        self.art, self.run, self.rv, self.config, self.project = art, run, rv, config, project
        self.block, self.body = split_frontmatter(art.text())
        self.state = seed_state(art, self.block, self.body, run, project)
        target = self.state.target_words or 0
        self.allowed = fix_context.allowed_urls(project, self.state, self.body)
        self.exempt = fix_context.sanctioned_names(project, self.state)
        self.context = fix_context.context_block(project, self.state, target)
        self.research_urls = _research_urls(self.state.research)
        self.applier = FixApplier(project, config)
        self.made: list[dict] = []

    @property
    def fields(self) -> dict:
        return store.frontmatter_fields(self.block)

    def text(self) -> str:
        return store.compose_article(self.block, self.body)

    def assets(self) -> set[str]:
        d = self.art.assets
        return {f"assets/{p.name}" for p in d.iterdir()} if d.is_dir() else set()

    def fix(self, markdown: str, instruction: str) -> dict:
        """The fixer on `markdown` (the body, or one frontmatter string): its report. No edit takes
        a picture out or adds one, whatever the instruction names (review R829-01): the pictures
        the page shows, counted, are the same after it, and a picture leaves only by the person's
        choice of `remove_picture`."""
        from collections import Counter

        rep = self.applier.apply_all(markdown, [instruction], context=self.context, allowed_urls=self.allowed,
                                     exempt_entity_text=self.exempt, existing_assets=self.assets(),
                                     keep_images=True)
        if rep.get("applied") and Counter(store.shown_images(markdown)) != Counter(store.shown_images(rep["markdown"])):
            return {**rep, "markdown": markdown, "applied": [], "skipped": [
                {"fix": instruction, "why": "the edit took out or added a picture: a picture is removed only by "
                                            "the person's choice"}]}
        return rep

    def write(self, block: str, body: str) -> None:
        self.block, self.body = block, body
        self.art.write_article(block, body)
        self.state.draft_markdown = body
        self.state.frontmatter = store.frontmatter_fields(block)


def _research_urls(research: dict) -> set[str]:
    r = research or {}
    out = {s.get("source_url") for s in (r.get("key_statistics") or []) if isinstance(s, dict)}
    for f in r.get("findings") or []:
        for fact in (f.get("facts") or []) if isinstance(f, dict) else []:
            if isinstance(fact, dict):
                out.add(fact.get("source_url"))
    return {u for u in out if u}


def _why(rep: dict) -> str:
    from .agents.visual_checker import _clip

    if rep.get("already_satisfied"):
        return "no change: the fixer says the text already does this (" + \
               _clip(str(rep["already_satisfied"][0].get("why") or ""), 300) + ")"
    if rep.get("skipped"):
        return _clip(str(rep["skipped"][0].get("why") or "the fixer could not make it"), 600)
    return "the fixer proposed no edit"


def _em_dashes(text: str) -> str:
    from .validators import normalize_em_dashes

    return normalize_em_dashes(text)[0] if "—" in (text or "") else text


# how a check that could not run begins its note: the card says only this, the record keeps the
# reason for an agent (R058-08)
CHECK_FAILED = "the check could not run"
# the card's sentence for a change the machine tried and did not keep
TRIED = "the machine tried and could not make it"


class Refused(Exception):
    """A change that would leave its target: the finding becomes a proposal, with this reason."""


class NotLanded(Exception):
    """A change the fixer could not make (or that broke a rule): nothing changed. `plain` is the
    sentence a person reads when the reason carries a model's or a tool's own words; without it the
    reason is the code's own and the card shows it (R058-08: "Cannot read naturally:
    JSONDecodeError at token 18" on a card)."""

    def __init__(self, reason: str = "", plain: str | None = None):
        super().__init__(reason)
        self.plain = plain


def _not_made(rep: dict) -> NotLanded:
    """The fixer made no change: its reason (the model's words, or the fixer's own checks in its
    terms) for the record and an agent; the card's plain sentence beside it (R058-08)."""
    skipped = rep.get("skipped") or [{}]
    plain = ("the machine found that the text already does this" if rep.get("already_satisfied")
             else f"{TRIED}: no sentence takes it without a reader noticing" if skipped[0].get("declined")
             else TRIED)
    return NotLanded(_why(rep), plain=plain)


# ---------------------------------------------------------------------------------------------
# the bounded changes: each returns [(where, before, after)] and has written the text
# ---------------------------------------------------------------------------------------------
def _quote_count(w: Work, quote: str) -> int:
    """How many times the words are in the text: the body and the frontmatter's strings."""
    return w.body.count(quote) + sum(s.count(quote) for _, s in _fm_paths(w.fields) if quote in s)


def _quote_place(w: Work, quote: str):
    """("body", (start, end) of its block) or ("fm", path) for the one place the words are."""
    in_body = w.body.count(quote)
    in_fm = [(path, s) for path, s in _fm_paths(w.fields) if quote in s]
    total = in_body + sum(s.count(quote) for _, s in in_fm)
    if total != 1:
        raise Refused(f"{qm.target_label({'quote': quote})} is not found exactly once any more")
    if in_body:
        at = w.body.index(quote)
        par = paragraph_of(w.body, at, at + len(quote))
        if par is None:
            raise Refused("the quoted words run across more than one paragraph")
        return "body", par
    return "fm", in_fm[0][0]


def _edit_place(w: Work, quote: str, instruction: str, check=None) -> list[tuple]:
    """Edit the paragraph (or the frontmatter string) that holds `quote`, and nothing else."""
    kind, where = _quote_place(w, quote)
    if kind == "fm":
        path = where
        old = next(s for p, s in _fm_paths(w.fields) if p == path)
        rep = w.fix(old, instruction)
        if not rep.get("applied"):
            raise _not_made(rep)
        new = _em_dashes(rep["markdown"])
        if check:
            check(old, new)
        top = path[0]
        value = _set_path(w.fields[top], path[1:], new) if len(path) > 1 else new
        w.write(store.set_frontmatter_field(w.block, top, value), w.body)
        return [(f"the frontmatter's {'.'.join(str(x) for x in path)}", old, new)]
    bound = where
    old_body = w.body
    rep = w.fix(old_body, instruction)
    if not rep.get("applied"):
        raise _not_made(rep)
    new_body = rep["markdown"]
    if not _within(old_body, new_body, bound):
        raise Refused("the change reached outside the paragraph that holds its words")
    p, old_end, new_end = _changed(old_body, new_body)
    new_body = new_body[:p] + _em_dashes(new_body[p:new_end]) + new_body[new_end:]
    after = new_body[bound[0]:bound[1] + len(new_body) - len(old_body)]
    if check:
        check(old_body[bound[0]:bound[1]], after)
    w.write(w.block, new_body)
    return [("the paragraph", old_body[bound[0]:bound[1]], after)]


def _edit(w: Work, f: dict) -> list[tuple]:
    quote = f["target"]["quote"]
    ins = (f"{f['text']}\n(Change only the paragraph that holds these words, copied from the article: "
           f"«{quote}». Copy `find` from that paragraph; nothing outside it may change.)")
    return _edit_place(w, quote, ins)


def _cite(w: Work, f: dict) -> list[tuple]:
    quote = f["target"]["quote"]
    if quote not in w.body:
        # the FAQ and the meta fields ship as plain structured text: no link can sit there
        raise Refused("a citation goes in the body; the FAQ and the meta fields carry no links")
    ins = (f"{f['text']}\n(Add a link to one of the SOURCES AN EDIT MAY LINK listed above, at this claim: "
           f"«{quote}». Keep every word of the claim as it is; only add the link, and at most a few "
           f"words that introduce it.)")

    def keeps_the_claim(before: str, after: str):
        new_urls = {u for _, u in _LINKED.findall(after)} - {u for _, u in _LINKED.findall(before)}
        if not new_urls:
            raise NotLanded("the edit added no link")
        if not new_urls <= w.research_urls:
            raise Refused("the link is not a source from the research: " + ", ".join(sorted(new_urls - w.research_urls)))
        if _plain(quote) not in _plain(after):
            raise Refused("the claim's words changed")
        if not _only_added(before, after, CITE_MAX_NEW_WORDS):
            raise Refused("the edit changed words beside adding the link")

    return _edit_place(w, quote, ins, check=keeps_the_claim)


def _section(w: Work, heading: str) -> dict:
    hits = [s for s in sections(w.body) if s["heading"] == " ".join(heading.split())]
    if len(hits) != 1:
        raise Refused(f"the section '{heading}' is not found exactly once any more")
    return hits[0]


def _word_for_word(w: Work, phrase: str, f: dict | None = None) -> bool:
    """Whether a keyword counts only word for word: the primary keyword and the SEO-mandatory
    ones (the article's and the project's), whose own checks count them so (RE56-01), and any
    phrase the finding itself asks for as "the exact phrase" (the fixer's guard reads the same
    words, fix_applier.asks_exact; R597-04)."""
    from .agents.fix_applier import asks_exact

    if f is not None and asks_exact(f.get("text")):
        return True
    research = w.state.keyword_research or {}
    exact = [research.get("primary_keyword"), *(research.get("mandatory_keywords") or []),
             *(_keyword_thresholds(w).get("mandatory") or [])]
    want = kr.tokens(phrase)
    return any(k and kr.tokens(str(k)) == want for k in exact)


def phrase_in(w: Work, text: str, phrase: str, f: dict | None = None) -> int:
    """How many times `text` says the phrase: the primary and the mandatory keywords, and a phrase
    the finding `f` asks for exactly, word for word; any other as a person writes it too, within
    one sentence, line or cell (keyword_report.phrase_count)."""
    return _count(text, phrase) if _word_for_word(w, phrase, f) else kr.phrase_count(text, phrase)


def _work_into(w: Work, f: dict, sec: dict, phrase: str) -> list[tuple]:
    """Work `phrase` into the existing sentences of one section: nothing outside it changes, no
    paragraph is added, and the phrase is there after (the primary keyword word for word; any
    other as a person writes it, which the exact-phrase rule turned into "means translation in
    real time:" and "… interpreting healthcare deployments", 2026-10-05)."""
    old_body = w.body
    bound = (sec["start"], sec["end"])
    if _word_for_word(w, phrase, f):
        ins = (f"{f['text']}\nWork the exact phrase '{phrase}' into one existing sentence of the section "
               f"'{sec['heading']}' so that the sentence still reads as a person would write it. Reword that "
               f"sentence; add no new sentence; change nothing outside that section.")
    else:
        # the examples sit in guillemets: the fixer's guard reads quoted words as wording the
        # change must contain (fix_applier.demanded_phrases)
        ins = (f"{f['text']}\nWork the phrase '{phrase}' into one existing sentence of the section "
               f"'{sec['heading']}' so that the sentence reads as a person would write it. Keep the phrase's "
               f"words in their order; you may put the small words a sentence needs between them («project "
               f"management for small teams» for «project management small teams»), make a word "
               f"plural or possessive, and hyphenate it («real-time»). Never bolt the phrase on, never repeat "
               f"what the sentence already says around it, never write anything a reader would notice as a "
               f"keyword. Reword that one sentence; add no new sentence; change nothing outside that section. "
               f"If no sentence of the section can take the phrase that way, change nothing and say so.")
    rep = w.fix(old_body, ins)
    if not rep.get("applied"):
        raise _not_made(rep)
    new_body = rep["markdown"]
    if not _within(old_body, new_body, bound):
        raise Refused(f"the change reached outside the section '{sec['heading']}'")
    p, old_end, new_end = _changed(old_body, new_body)
    new_body = new_body[:p] + _em_dashes(new_body[p:new_end]) + new_body[new_end:]
    before, after = old_body[bound[0]:bound[1]], new_body[bound[0]:bound[1] + len(new_body) - len(old_body)]
    if phrase_in(w, after, phrase, f) <= phrase_in(w, before, phrase, f):
        raise NotLanded(f"the phrase '{phrase}' is not in the section after the edit")
    lost = [k for k in _tracked_keywords(w) if " ".join(kr.tokens(k)) != " ".join(kr.tokens(phrase))
            and phrase_in(w, before, k) and not phrase_in(w, after, k)]
    if lost:
        # one keyword in, another out: live, the next change of the same review rewrote the
        # sentence an earlier one had worked a keyword into (2026-10-06)
        raise Refused(f"the change took the keyword '{lost[0]}' out of the section '{sec['heading']}'")
    if len(paragraphs(after)) != len(paragraphs(before)):
        raise Refused(f"the change added or removed a paragraph in the section '{sec['heading']}'")
    if len(after.split()) - len(before.split()) > len(phrase.split()) + KEYWORD_MAX_NEW_WORDS:
        raise Refused(f"the change added more than the phrase to the section '{sec['heading']}'")
    w.write(w.block, new_body)
    return [(f"the section '{sec['heading']}'", before, after)]


def _tracked_keywords(w: Work) -> list[str]:
    """The keywords the article's coverage counts (keyword_report.analyze): the primary, the
    secondary ones, each section's, the SEO-mandatory ones, the related pool and the LSI terms."""
    research, brief = w.state.keyword_research or {}, w.state.brief or {}
    out = [research.get("primary_keyword"), *((research.get("analysis") or {}).get("secondary_keywords") or []),
           *(k for s in brief.get("sections") or [] for k in (s.get("keywords") or [])),
           *(research.get("mandatory_keywords") or []), *(_keyword_thresholds(w).get("mandatory") or []),
           # the related pool and the LSI terms are counted in the coverage too (RE99-06)
           *(r.get("keyword") for r in research.get("related") or [] if isinstance(r, dict)),
           *(x.get("keyword") for x in research.get("lsi") or [] if isinstance(x, dict))]
    return list(dict.fromkeys(str(k) for k in out if k))


def _insert_keyword(w: Work, f: dict) -> list[tuple]:
    if f.get("target"):
        return _work_into(w, f, _section(w, f["target"]["heading"]), f["phrase"])
    return _keyword_count(w, f)


def _keyword_thresholds(w: Work) -> dict:
    from .keyword_report import thresholds_from

    try:
        return thresholds_from(w.project.seo)
    except Exception:  # noqa: BLE001
        return {}


def _keyword_count(w: Work, f: dict) -> list[tuple]:
    """The phrase is missing from the article as a whole: the code picks the sections that lack
    it, longest first, as many as the floor needs, and makes one bounded change in each."""
    phrase = f["phrase"]
    primary = str((w.state.keyword_research or {}).get("primary_keyword") or "").strip().lower()
    need = 1
    if phrase.lower() == primary:
        need = max(1, int(_keyword_thresholds(w).get("primary_min_total") or 1) - _count(w.body, phrase))
    out, why = [], []
    for sec in sorted(sections(w.body), key=lambda s: s["start"] - s["end"]):
        if len(out) >= need:
            break
        if sec["level"] < 2 or _count(w.body[sec["start"]:sec["end"]], phrase):
            continue
        try:
            sec = _section(w, sec["heading"])            # its place in the text as it is now
            out += _work_into(w, f, sec, phrase)
        except (Refused, NotLanded) as e:
            why.append(f"{sec['heading']}: {e}")
    if not out:
        raise NotLanded("; ".join(why) or f"no section without the phrase '{phrase}' to work it into",
                        plain=f"{TRIED}: no section could take the phrase '{phrase}'" if why else None)
    return out


def _keyword_density(w: Work, f: dict) -> list[tuple]:
    """The phrase repeats too often: the code picks the paragraphs that repeat it most and makes
    one change in each, one occurrence fewer, until the density is under the ceiling."""
    phrase = str((w.state.keyword_research or {}).get("primary_keyword") or "").strip()
    if not phrase:
        raise NotLanded("no primary keyword recorded")
    prim = (w.state.keywords or {}).get("primary") or {}
    ceiling = float(_keyword_thresholds(w).get("primary_density_max") or 0)
    density = float(prim.get("density") or 0)
    total = _count(w.body, phrase)
    k = max(1, math.ceil(total * (1 - ceiling / density))) if density and ceiling and density > ceiling else 1
    out, why, tried = [], [], set()
    while len(out) < k:
        pars = sorted(((_count(w.body[a:b], phrase), a, b) for a, b in paragraphs(w.body)
                       if not w.body[a:b].lstrip().startswith("#") and w.body[a:b] not in tried), reverse=True)
        cand = next((p for p in pars if p[0] >= 1), None)
        if cand is None:
            break
        n, a, b = cand
        tried.add(w.body[a:b])
        old_body, par = w.body, w.body[a:b]
        ins = (f"{f['text']}\nIn the paragraph «{' '.join(par.split())[:200]}», replace one occurrence of '{phrase}' "
               f"with a natural variant or a pronoun. Change nothing else.")
        rep = w.fix(old_body, ins)
        new_body = rep.get("markdown") or old_body
        if not rep.get("applied") or not _within(old_body, new_body, (a, b)):
            why.append(_why(rep) if not rep.get("applied") else "a change reached outside its paragraph")
            break
        after = new_body[a:b + len(new_body) - len(old_body)]
        if _count(after, phrase) >= n:
            why.append("the paragraph still repeats the phrase as often")
            break
        w.write(w.block, new_body)
        out.append(("the paragraph", par, after))
    if not out:
        raise NotLanded("; ".join(why) or "no paragraph repeats the phrase",
                        plain=f"{TRIED}: no paragraph could repeat the phrase less" if why else None)
    return out


def _picture(w: Work, f: dict, keep) -> list[tuple]:
    """Redraw or re-capture the target picture: the file (its previous bytes kept), its alt text
    and its caption; the rest of the text stays as it is."""
    from .agents.interactive import VisualProducerService
    from .tools.markdown_render import parse

    asset = f["target"]["asset"]
    alt = next((c.content for t in parse(w.body) for c in (t.children or [])
                if c.type == "image" and store.local_asset(c.attrGet("src")) == asset), "")
    keep(_picture_files(w.art, asset))
    try:
        res = VisualProducerService(w.project, w.config).edit_visual(w.art.dir, asset, feedback=f["text"], alt=alt)
    except Exception as e:  # noqa: BLE001 — a failed render changes nothing
        res = {"error": f"{type(e).__name__}: {str(e)[:160]}"}
    if not res.get("ok"):
        raise NotLanded(f"the picture could not be made again: {res.get('error') or 'no answer'}",
                        plain="the machine could not make the picture again")
    old_body = w.body
    new_body = retitle_picture(old_body, asset, res.get("alt") or "", res.get("caption") or "")
    if new_body != old_body:
        w.write(w.block, new_body)
    return [(asset, f"{asset} (the previous picture, kept)", f"{asset} (made again)"
             + (", with a new alt text and caption" if new_body != old_body else ""))]


def picture_lines(body: str, asset: str) -> tuple[int, list[str]] | None:
    """Where the picture `asset` sits in `body`, as whole lines: (the index of its line, its
    lines: the image on a line of its own and, when the next line is one, the caption under it).
    None unless exactly one line shows the picture, and that line is the image alone."""
    lines = body.split("\n")
    i = store.picture_line(body, asset)
    if i is None:
        return None
    n = 2 if i + 1 < len(lines) and store.caption_of(lines[i], lines[i + 1]) else 1
    return i, lines[i:i + n]


def retitle_picture(body: str, asset: str, alt: str, caption: str) -> str:
    """`body` with the picture's alt text and caption line in the producer's words for the new
    picture, and nothing else changed (Refused otherwise). Missing words leave the old ones."""
    from .agents.fix_routing import retitle_visual

    new_body = retitle_visual(body, asset, alt or "", caption or "")
    if new_body == body:
        return body
    # held to the picture's own lines (the image and the caption under it), whichever of the two
    # changed: a new caption under the same alt text was refused as reaching outside (RBC6-01)
    old, new = picture_lines(body, asset), picture_lines(new_body, asset)
    lines = body.split("\n")
    if old is not None and new is not None:
        lines[old[0]:old[0] + len(old[1])] = new[1]
    if old is None or new is None or "\n".join(lines) != new_body:
        raise Refused(f"the new alt text or caption of {asset} reached outside its picture")
    if not store.reads_as_picture("\n".join(new[1]), asset):
        raise Refused(f"the new alt text or caption of {asset} would not read as its picture and caption")
    return new_body


def picture_problem(art: Article, asset: str) -> str:
    """What the latest review says is wrong with a picture: its open finding, else what the check
    still saw after a change made it again; "" when nothing. A person's Redraw passed nothing, and
    the producer captured the same page again (2026-10-05)."""
    try:
        rv = art.run().get("review") or {}
    except Exception:  # noqa: BLE001 — an unreadable record: the producer works from the picture alone
        return ""
    last = (latest_review(rv) or {}).get("id")
    for f in rv.get("findings") or []:
        if f.get("review") == last and f.get("status") == "open" and (f.get("target") or {}).get("asset") == asset:
            return str(f.get("text") or "")
    for g in reversed(rv.get("changes") or []):
        if g.get("review") == last and not g.get("reverted") and asset in (g.get("where") or []):
            # the check's word while it still stands: a change the person kept as it is or
            # adjusted is decided, and the next Redo does not take it for the task (R058-10)
            return str(g.get("check_note") or "") if qm.half_done(g) else ""
    return ""


def _picture_files(art: Article, asset: str) -> list[str]:
    """The files a redraw of `asset` writes: the picture, its spec beside it, and for the cover
    the background it is composed over."""
    p = Path(asset)
    out = [asset, str(p.with_suffix(".json"))]
    if p.name == "cover.png":
        out.append(str(p.with_name("cover_bg.png")))
    return out


def _remove_picture(w: Work, f: dict) -> list[tuple]:
    from .agents.fix_routing import remove_picture

    asset = f["target"]["asset"]
    new_body, why = remove_picture(w.body, asset)
    if why:
        raise NotLanded(why)
    old_body = w.body
    w.write(w.block, new_body)
    p, old_end, new_end = _changed(old_body, new_body)
    return [(asset, old_body[p:old_end], new_body[p:new_end] or "(removed, with its caption)")]


def _picture_place(w: Work, f: dict) -> int | None:
    """Where a new picture goes: after the block holding the words the finding names (after the
    whole table when they are in one), else after the first block of its section."""
    body, quote = w.body, (f.get("target") or {}).get("quote")
    span = None
    if quote and body.count(quote) == 1:
        at = body.find(quote)
        span = paragraph_of(body, at, at + len(quote))
    if span is None:
        heading = " ".join(str(f.get("section") or "").split())
        sec = next((s for s in sections(body) if s["heading"] == heading), None) if heading else None
        if sec:
            span = next(((a, b) for a, b in paragraphs(body) if sec["start"] < a < sec["end"]), None)
    if span is None:
        return None
    return _past_table(body, span[1])


def _past_table(body: str, end: int) -> int:
    """`end`, or the end of the table it falls in: a table's rows are blocks of their own, and a
    picture goes after the whole table, read with the page's parser (a table without its outer
    pipes was split by a picture, RE99-05)."""
    from .tools.markdown_render import parse

    starts = _line_starts(body)
    for t in parse(body):
        if t.type == "table_open" and t.map:
            a, b = _span(starts, t.map, len(body))
            if a < end < b:
                return b
    return end


def _add_picture(w: Work, f: dict, keep) -> list[tuple]:
    """A picture the article lacks (`add_picture`), made by the picture producer, which picks the
    form (a chart, an infographic, a screenshot it looks at, an illustration) from what the
    finding asks, and put after the block the finding names. Its files are new: a revert takes
    them back out with the text."""
    from .agents.interactive import VisualProducerService
    from .agents.visual_director import next_free

    at = _picture_place(w, f)
    if at is None:
        raise NotLanded("there is no place for the picture: the words it goes after are not in the article once")
    body = w.body
    if body[at:].lstrip("\n").startswith("!["):
        # never two pictures in a row: after the text that follows that picture, in the section
        nxt = next(((a, b) for a, b in paragraphs(body) if a > at and not body[a:b].lstrip().startswith("![")), None)
        if nxt is None or body[nxt[0]:nxt[1]].lstrip().startswith("#") or \
                body[_past_table(body, nxt[1]):].lstrip("\n").startswith("!["):
            raise NotLanded("a picture already follows that paragraph, and no text follows it in the section")
        at = _past_table(body, nxt[1])
    assets = w.art.dir / "assets"
    # the file the producer will write is new: kept as "none before", so a failed attempt and a
    # revert both take it out (the producer takes the next free name of its kind)
    keep([f"assets/{p}_{next_free(assets, p)}{ext}" for p in dict.fromkeys(VisualProducerService._PREFIX.values())
          for ext in (".png", ".json")])
    shown = [ref for _, ref in store.shown_image_refs(body)]
    res = VisualProducerService(w.project, w.config).create(
        w.art.dir, f["text"], body[:at], body[at:],
        avoid_urls=[u for u in (_shot_url(w.art, r) for r in shown) if u])
    if res.get("error"):
        raise NotLanded(f"the picture could not be made: {res['error']}",
                        plain="the machine could not make the picture")
    new_body = body[:at].rstrip("\n") + "\n\n" + res["markdown"] + "\n\n" + body[at:].lstrip("\n")
    w.write(w.block, new_body)
    return [(res["path"], "", f"{res['path']} (a new picture)")]


def _shot_url(art: Article, ref: str) -> str | None:
    """The page a screenshot in the article shows (its sidecar), so a new one is not the same page."""
    try:
        side = json.loads((art.dir / ref).with_suffix(".json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(side, dict) or side.get("producer") != "screenshot":
        return None
    return str((side.get("spec") or {}).get("url") or "") or None


def _anywhere(w: Work, f: dict, cite: bool = False) -> list[tuple]:
    """A proposal the person ticked whose change has no bound the machine keeps to (no action
    from the list, or a target that is not found once): the fixer and all its guards, on the body.
    A citation (`cite`) still only adds its link: its quote had been reworded by an earlier
    change, and the fixer put the old words back with the link, undoing that change (2026-10-06)."""
    old_body = w.body
    ins = f["text"]
    if cite:
        ins += ("\n(Only add the link at the claim: keep every word of the text as it is now. The words quoted "
                "above may read differently now, because an earlier change reworded them.)")
    rep = w.fix(old_body, ins)
    if not rep.get("applied"):
        raise _not_made(rep)
    new_body = rep["markdown"]
    if cite:
        new_urls = {u for _, u in _LINKED.findall(new_body)} - {u for _, u in _LINKED.findall(old_body)}
        if not new_urls:
            raise NotLanded("the edit added no link")
        if not new_urls <= w.research_urls:
            raise Refused("the link is not a source from the research: " + ", ".join(sorted(new_urls - w.research_urls)))
        if not _only_added(old_body, new_body, CITE_MAX_NEW_WORDS):
            raise Refused("the edit changed words beside adding the link")
    p, old_end, new_end = _changed(old_body, new_body)
    new_body = new_body[:p] + _em_dashes(new_body[p:new_end]) + new_body[new_end:]
    a = old_body.rfind("\n\n", 0, p) + 1 if p else 0
    b = old_body.find("\n\n", old_end)
    b = len(old_body) if b < 0 else b
    w.write(w.block, new_body)
    return [("the text", old_body[a:b].strip("\n"), new_body[a:b + len(new_body) - len(old_body)].strip("\n"))]


def _keeps_caption_links(w: Work, before: str, after: str) -> None:
    """The caption under a screenshot of our own page links that page (operator, 2026-09-28);
    a change that takes the link out while the picture stays is refused. Two readers asked to
    unlink such captions as repeated links, and the machine did (2026-10-06)."""
    from .validators import caption_link_spans

    own = w.project.brand.is_own
    had = {asset for _, _, asset, url in caption_link_spans(before) if own(url)}
    has = {asset for _, _, asset, url in caption_link_spans(after) if own(url)}
    shown = {ref for _, ref in store.shown_image_refs(after)}
    lost = sorted(a for a in had - has if a in shown)
    if lost:
        raise Refused(f"the caption under {lost[0]} links our page it shows, and keeps that link")


def change_for(w: Work, f: dict, keep, chosen: bool = False) -> list[tuple]:
    """Make the change a finding asks for, within its bound; raises Refused or NotLanded."""
    action = f.get("action")
    quote = (f.get("target") or {}).get("quote")
    if chosen and action in ("edit", "cite") and quote and _quote_count(w, quote) != 1:
        # its words changed since the review (an earlier change rewrote the paragraph): a ticked
        # proposal without a clear target goes to the fixer with all its guards. The bounded
        # edit refused it every time, and the card's button could never land (2026-10-05)
        return _anywhere(w, f, cite=action == "cite")
    if action == "edit" and f.get("kind") != "fact" and f.get("target"):
        return _edit(w, f)
    if action == "edit" and chosen and f.get("target"):
        return _edit(w, f)                          # a changed claim, ticked by the person
    if action == "cite" and f.get("target"):
        return _cite(w, f)
    if action == "insert_keyword" and f.get("phrase"):
        return _insert_keyword(w, f)
    if action == "edit" and f.get("kind") == "keyword" and f.get("check") == "Primary keyword density":
        return _keyword_density(w, f)
    if action in ("redraw", "recapture") and f.get("target"):
        return _picture(w, f, keep)
    if action == "remove_picture" and chosen and f.get("target"):
        return _remove_picture(w, f)
    if action == "add_picture" and chosen:
        return _add_picture(w, f, keep)
    if chosen and action != "outline":
        return _anywhere(w, f)
    raise NotLanded("this change is not one the machine makes")


def machine_change(f: dict, text: str, cover: str | None) -> bool:
    """Whether the machine makes this finding's change by itself after its review: on the safe
    list with its target found once, or the primary keyword's count and density, whose targets
    the code picks."""
    if f.get("status") != "open" or f.get("needs_choice"):
        return False
    if qm.route(f, text, cover)[0] == "machine":
        return True
    return (f.get("kind") == "keyword" and f.get("source") == "code" and not f.get("target")
            and ((f.get("action") == "insert_keyword" and f.get("phrase"))
                 or (f.get("action") == "edit" and f.get("check") == "Primary keyword density")))


# ---------------------------------------------------------------------------------------------
# groups: what was changed, with the previous files kept
# ---------------------------------------------------------------------------------------------
def _sha(p: Path) -> str | None:
    try:
        return hashlib.sha1(p.read_bytes()).hexdigest()
    except OSError:
        return None


class Group:
    """One change group in the making: the article's text and the files it may touch are copied
    to `_kept/<id>/` before anything changes, so the group can be taken back exactly."""

    def __init__(self, w: Work, review: str | None):
        self.w, self.review = w, review
        self.id = _next_group_id(w.rv)
        while (w.art.dir / KEPT / self.id).exists() or any(g["id"] == self.id for g in w.made):
            self.id = f"c{int(self.id[1:]) + 1}"
        self.dir = w.art.dir / KEPT / self.id
        self.version_before = w.art.version()
        self.kept: dict[str, str | None] = {}
        self.before_files: dict[str, str | None] = {}
        self.attempt_pre: dict[str, bytes | None] = {}   # the files as one attempt found them
        self.findings: list[str] = []
        self.places: list[tuple] = []
        from . import versions

        versions.ensure(w.art)          # the state before the group is a version (core/versions.py)
        self.keep(["article.md"])

    def keep(self, rels: list[str]) -> None:
        """Copy the files a change is about to write: their bytes before the group (kept for the
        revert) and before this attempt (put back if the attempt does not land)."""
        for rel in rels:
            src = self.w.art.dir / rel
            if rel not in self.attempt_pre:
                self.attempt_pre[rel] = src.read_bytes() if src.is_file() else None
            if rel in self.kept:
                continue
            self.before_files[rel] = _sha(src)
            if src.is_file():
                dst = self.dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                self.kept[rel] = str(Path(KEPT) / self.id / rel)
            else:
                self.kept[rel] = None

    def discard(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def _picture_left(self, files: list[dict]) -> dict:
        """What a picture group (redraws and re-captures only) left, so that it can be taken back
        on its own while that stays as it is (operator, 2026-10-05): `left`, its files' hashes,
        and `text`, its picture's lines (the image and the caption under it) before and after,
        when it gave the picture a new alt text or caption. {} for any other group, and for one
        whose text changed anywhere but those lines: that one reverts only as the latest."""
        fs = [f for f in self.w.rv["findings"] if f["id"] in self.findings]
        assets = {p[0] for p in self.places}
        if not fs or any(f.get("action") not in ("redraw", "recapture") for f in fs) or len(assets) != 1:
            return {}
        asset = next(iter(assets))
        rec = {"left": {r["path"]: _sha(self.w.art.dir / r["path"]) for r in files if r["path"] != "article.md"}}
        if any(r["path"] == "article.md" for r in files):
            old_block, old = split_frontmatter((self.dir / "article.md").read_text(encoding="utf-8"))
            new_block, new = split_frontmatter(self.w.art.path("article.md").read_text(encoding="utf-8"))
            ob, nb = picture_lines(old, asset), picture_lines(new, asset)
            if ob is None or nb is None or old_block != new_block:
                return {}
            lines = old.split("\n")
            lines[ob[0]:ob[0] + len(ob[1])] = nb[1]
            if "\n".join(lines) != new:
                return {}
            rec["text"] = {"asset": asset, "before": "\n".join(ob[1]), "after": "\n".join(nb[1])}
        return rec

    def close(self, at: str | None = None, kind: str = "review", who: str | None = None) -> dict | None:
        """The group record (appended to the review's changes), or None when nothing changed. The
        state after it is a version of its own (`kind`: review, proposal, fix, edit)."""
        from . import versions

        files = [{"path": rel, "kept": kept} for rel, kept in self.kept.items()
                 if _sha(self.w.art.dir / rel) != self.before_files.get(rel)]
        if not self.findings or not files:
            self.discard()
            return None
        g = qm.change_group(id=self.id, findings=self.findings, where=[p[0] for p in self.places],
                            before=[p[1] for p in self.places], after=[p[2] for p in self.places],
                            files=files, version_before=self.version_before,
                            version_after=self.w.art.version(), review=self.review, at=at,
                            **self._picture_left(files))
        texts = [f.get("text") or "" for f in self.w.rv["findings"] if f["id"] in self.findings]
        v = versions.record(self.w.art, who or (f"the review {self.review}" if self.review else "the Studio"),
                            (texts[0] if texts else "A change")[:160], ", ".join(self.findings), kind=kind,
                            group=self.id)
        if v:
            g["version_n"] = v["n"]
        self.w.rv["changes"].append(g)
        self.w.made.append(g)
        return g


def attempt(w: Work, f: dict, group: Group | None = None, chosen: bool = False) -> tuple[Group | None, str | None]:
    """Try one finding's change, inside `group` (a new one when None). Returns (the group, None)
    when it landed, or (the group or None, why not). A change that left its target makes the
    finding a proposal; one that did not land leaves it open with the reason."""
    own = group is None
    g = group or Group(w, f.get("review"))
    g.attempt_pre = {}
    snapshot = (w.block, w.body)
    try:
        places = change_for(w, f, g.keep, chosen=chosen)
        _keeps_caption_links(w, snapshot[1], w.body)
    except Exception as e:  # noqa: BLE001 — a failed call (Refused, NotLanded, a provider error) undoes this attempt only
        if not isinstance(e, (Refused, NotLanded)):
            e = NotLanded(f"the fixer call failed: {type(e).__name__}: {str(e)[:200]}",
                          plain="the model did not answer; try again")
        if (w.block, w.body) != snapshot:
            w.write(*snapshot)
        _undo_attempt(w.art, g)
        if isinstance(e, Refused) and not chosen:
            f["needs_choice"] = f"{e}: your choice"
        f["note"] = str(e)
        f["note_plain"] = getattr(e, "plain", None) or f"{TRIED}: {e}"     # the card's (R058-08)
        f["tried"] = int(f.get("tried") or 0) + 1
        if own:
            g.discard()
        return (None if own else g), str(e)
    g.findings.append(f["id"])
    g.places += places
    f["status"] = "changed"
    f["note"] = f["note_plain"] = None
    f["tried"] = int(f.get("tried") or 0) + 1
    return g, None


def _undo_attempt(art: Article, g: Group) -> None:
    """Put the files one failed attempt touched back as that attempt found them (the text is
    written back by the caller)."""
    from .store import atomic_write_bytes

    for rel, data in g.attempt_pre.items():
        if rel.endswith(".md"):
            continue
        dst = art.dir / rel
        if data is None:
            if dst.exists():
                dst.unlink()
        elif not dst.is_file() or dst.read_bytes() != data:
            atomic_write_bytes(dst, data)


# ---------------------------------------------------------------------------------------------
# the check: a hint beside each group
# ---------------------------------------------------------------------------------------------
def check_groups(w: Work, groups: list[dict]) -> None:
    """The check's opinion of each group (agents/edit_verifier for text and drawn pictures, the
    picture check for a screenshot or the cover): fixed / still_there / not_checked, with its
    reason. It changes no status and starts nothing; a check that cannot run says not_checked."""
    if not groups:
        return
    try:
        _check_groups(w, groups)
    except Exception as e:  # noqa: BLE001 — the hint must never cost a change its record
        for g in groups:
            if g.get("check") == "not_checked" and not g.get("check_note"):
                g["check_note"], g["check_failed"] = f"{CHECK_FAILED}: {str(e)[:160]}", True


def _check_groups(w: Work, groups: list[dict]) -> None:
    from .agents.edit_verifier import EditVerifier

    by_id = {f["id"]: f for f in w.rv["findings"]}
    verifier = EditVerifier(w.project, w.config)
    w.state.draft_markdown, w.state.frontmatter = w.body, w.fields
    batch, seen = [], {}
    for g in groups:
        fs = [by_id[x] for x in g["findings"] if x in by_id]
        pictures = [p for p in g["where"] if str(p).startswith("assets/")]
        if pictures and all(f.get("action") in ("redraw", "recapture") for f in fs):
            # the picture check looks at what was drawn; a drawn picture it finds right is then
            # read against the facts through its spec as well, so both have their say
            looked = verifier._look(w.state, pictures[0])
            spec = verifier._visual_spec(w.state, pictures[0])
            if looked is not None and (not looked[0] or spec is None):
                g["check"], g["check_note"] = ("fixed" if looked[0] else "still_there"), looked[1]
                g.pop("check_failed", None)
                continue
            if spec is None:
                g["check"], g["check_note"] = "not_checked", "a picture the check cannot read: look at it"
                g.pop("check_failed", None)
                continue
            if looked is not None:
                seen[g["id"]] = looked[1]
        else:
            spec = None
        sev = max((f.get("severity") for f in fs), key=lambda s: qm.POINTS.get(s, 7), default="unknown")
        batch.append({"id": g["id"], "severity": sev, "check": fs[0].get("kind") if fs else None,
                      "text": "\n".join(f["text"] for f in fs),
                      "last_edit": {"find": "\n\n".join(map(str, g["before"])),
                                    "replace": "\n\n".join(map(str, g["after"]))},
                      **({"_spec": spec} if spec else {})})
    if not batch:
        return
    try:
        rows, failed = verifier._ask(w.state, batch), False
        why_none = "the check gave no answer for this change"
    except Exception as e:  # noqa: BLE001 — a check that could not run is never a pass
        rows, why_none, failed = {}, f"{CHECK_FAILED}: {str(e)[:160]}", True
    from .agents.base import strict_bool

    for g in (g for g in groups if g["id"] in {b["id"] for b in batch}):
        v = rows.get(g["id"]) or {}
        verdict = strict_bool(v.get("resolved")) if v else None
        g["check"] = {True: "fixed", False: "still_there"}.get(verdict, "not_checked")
        note = str(v.get("why") or "").strip() if v else why_none
        np_ = v.get("new_problem") if isinstance(v, dict) else None
        fs = [by_id[x] for x in g["findings"] if x in by_id]
        if (isinstance(np_, dict) and str(np_.get("check") or "") == "keywords" and g["check"] == "fixed"
                and any(f.get("action") == "insert_keyword" or f.get("kind") == "keyword" for f in fs)):
            # a keyword worked in that a reader would notice: "fixed" in green hid it (2026-10-06)
            g["check"] = "still_there"
        if isinstance(np_, dict) and str(np_.get("fix") or "").strip():
            from .agents.visual_checker import _clip

            note += (" " if note else "") + "The check also sees: " + _clip(" ".join(str(np_["fix"]).split()), 300)
        if g["id"] in seen:
            note = seen[g["id"]][0].upper() + seen[g["id"]][1:] + (". " + note if note else "")
        g["check_note"] = note or ("the check gave no clear answer" if verdict is None else None)
        # a check that could not run, said by its state, not by where its words sit in the note:
        # a picture's look goes before them (R9F0-06)
        if failed and not v:
            g["check_failed"] = True
        else:
            g.pop("check_failed", None)


# ---------------------------------------------------------------------------------------------
# the person's actions (each under the article's lock, each with the version the page showed)
# ---------------------------------------------------------------------------------------------
def _session(slug: str, op: str, label: str, config=None, project=None):
    from contextlib import contextmanager

    from .config import load_core_config, load_project, runtime_config

    @contextmanager
    def cm():
        cfg = config or load_core_config()
        proj = project or load_project(core=cfg)
        cfg = runtime_config(cfg, proj)
        art = store.Repository(config=cfg).get(slug)
        with art.lock(wait=False, holder=f"{op} {label}".strip()), \
                usage.recording(art.dir, op, label=label, config=cfg), usage.stage("edits"):
            yield art, cfg, proj
    return cm()


def _report(w: Work, landed: list[str], missed: dict, done: dict | None = None) -> dict:
    out = {"version": w.art.version(), "groups": [g["id"] for g in w.made], "landed": landed,
           "not_landed": missed}
    if done:
        out["already_done"] = done
    return out


def _already_there(w: Work, f: dict) -> str | None:
    """Why a keyword finding needs nothing, by the code's count: the phrase it asks to work into a
    section is in that section already (an earlier change of the same review put it there, or a
    read missed it). It sat among what needs the person with "tried and not kept: the fixer says
    the text already does this" and no word of which phrase (2026-10-05)."""
    phrase, target = f.get("phrase"), f.get("target") or {}
    if f.get("action") != "insert_keyword" or not phrase or "heading" not in target:
        return None
    hits = [x for x in sections(w.body) if x["heading"] == " ".join(str(target["heading"]).split())]
    if len(hits) != 1 or not phrase_in(w, w.body[hits[0]["start"]:hits[0]["end"]], phrase, f):
        return None
    return f"the phrase «{phrase}» is already in the section «{hits[0]['heading']}»"


def _close_done(f: dict, why: str) -> None:
    qm.close(f, "the review", why)
    f["closed"]["already"] = True        # the code found it done: not a person's decision


def apply_safe(w: Work, review: dict) -> dict:
    """After `review`: the machine makes every change it may make by itself, findings on the same
    paragraph or picture in one group. Called by the review (core/qa_review), inside its lock."""
    if not review or review.get("id") != (latest_review(w.rv) or {}).get("id"):
        raise Stale("only the latest review's changes are made")
    text, cover = w.text(), w.art.cover()
    todo = [f for f in w.rv["findings"] if f.get("review") == review["id"] and machine_change(f, text, cover)]
    keyed: dict = {}
    for f in todo:
        keyed.setdefault(_group_key(w, f), []).append(f)
    landed, missed, done = [], {}, {}
    for key, fs in keyed.items():
        g = None
        for f in fs:
            if (why := _already_there(w, f)):
                _close_done(f, why)
                done[f["id"]] = why
                continue
            g, why = attempt(w, f, g)
            (missed.__setitem__(f["id"], why) if why else landed.append(f["id"]))
        if g is not None:
            g.close()
        _save(w)                  # the text and its record move together (review R829-02)
    check_groups(w, w.made)
    _save(w)
    return _report(w, landed, missed, done)


def _group_key(w: Work, f: dict):
    """Findings on the same paragraph or the same picture share a group."""
    t = f.get("target") or {}
    if "asset" in t:
        return ("picture", t["asset"])
    if "quote" in t and w.body.count(t["quote"]) == 1:
        at = w.body.index(t["quote"])
        return ("paragraph", paragraph_of(w.body, at, at + len(t["quote"])))
    return ("finding", f["id"])


def _save(w: Work) -> None:
    w.run["review"] = w.rv
    w.art.save_run(w.run)


def apply_proposals(slug: str, ids: list[str], version: str, review: str, config=None, project=None) -> dict:
    """Apply the proposals the person ticked on the page that showed `version` under `review`:
    each in a group of its own. A tick from another review or another version changes nothing."""
    with _session(slug, "proposals", ",".join(ids), config, project) as (art, cfg, proj):
        run, rv = records(art)
        check_version(art, version)
        if (latest_review(rv) or {}).get("id") != review:
            raise Stale("a later review replaced the one these proposals came from: look at the page again")
        picked = [finding_by_id(rv, fid) for fid in ids]
        if any(f is None or f.get("review") != review for f in picked):
            raise Stale("a proposal is not one of this review's: look at the page again")
        w = Work(art, run, rv, cfg, proj)
        text, cover = w.text(), art.cover()
        landed, missed = [], {}
        for f in picked:
            if f.get("status") != "open" or qm.route(f, text, cover)[0] != "proposal":
                missed[f["id"]] = "not an open proposal"
                continue
            if f.get("choices"):
                missed[f["id"]] = "the reads named different changes for this picture: pick one on its card"
                continue
            g, why = attempt(w, f, chosen=True)
            if why:
                missed[f["id"]] = why
            else:
                landed.append(f["id"])
                g.close(kind="proposal")
            _save(w)
        check_groups(w, w.made)
        _save(w)
        return _report(w, landed, missed)


def fix_again(slug: str, fid: str, version: str, config=None, project=None) -> dict:
    """What is left: a safe change that did not land, tried once more."""
    with _session(slug, "fix", fid, config, project) as (art, cfg, proj):
        run, rv = records(art)
        check_version(art, version)
        f = _current(rv, fid)
        w = Work(art, run, rv, cfg, proj)
        if not machine_change(f, w.text(), art.cover()):
            return {**_report(w, [], {fid: qm.route(f, w.text(), art.cover())[1] or "not a machine change"})}
        if (done := _already_there(w, f)):
            _close_done(f, done)
            _save(w)
            return _report(w, [], {}, {fid: done})
        g, why = attempt(w, f)
        if g is not None and not why:
            g.close(kind="fix")
        _save(w)
        check_groups(w, w.made)
        _save(w)
        return _report(w, [] if why else [fid], {fid: why} if why else {})


def how_it_reverts(art: Article, rv: dict, g: dict, now: str) -> str | None:
    """How a group of the latest review is taken back now: "latest" (the newest group, while
    nothing has changed after it), "picture" (a picture group whose picture and caption are still
    as it left them, whatever came after), or None (it is undone in History)."""
    if g.get("reverted") or g.get("review") != (latest_review(rv) or {}).get("id"):
        return None
    lg = latest_group(rv)
    if lg is not None and lg["id"] == g["id"] and g.get("version_after") == now:
        return "latest"
    return "picture" if _picture_alone(art, g) else None


def _picture_alone(art: Article, g: dict) -> bool:
    """Whether a picture group can be taken back on its own. Its record came with the article (an
    import brings its own), so all of it is checked before anything is written: it recorded what
    it left (groups from before 2026-10-05 did not); its files are pictures, each with its one
    record and its kept copy, and still the bytes it made; and its text, when it changed it, is
    its picture's lines alone (the image and its caption), which are in the body now, as whole
    lines, exactly as it wrote them (R7DC-01..03)."""
    from .store import UnsafePath

    left, text, recs = g.get("left"), g.get("text"), g.get("files")
    if not isinstance(left, dict) or not left or not all(str(rel).startswith("assets/") for rel in left):
        return False                    # only pictures: the text goes back by its picture's lines
    if not isinstance(recs, list) or not all(isinstance(r, dict) and isinstance(r.get("path"), str) for r in recs):
        return False                    # a path of another type broke the article's page (RCAA-01)
    pics = [r["path"] for r in recs if r["path"] != "article.md"]
    if len(pics) != len(set(pics)) or set(pics) != set(left) or len(recs) - len(pics) != (1 if text else 0):
        return False                    # one record for each picture file, and the text's iff it changed
    try:
        if any(_sha(art.own_file(rel)) != sha for rel, sha in left.items()):
            return False
        _restore_plan(art, g, str(g.get("id")), paths=set(left))
    except (UnsafePath, Refused):
        return False
    if text:
        if not isinstance(text, dict) or not isinstance(text.get("asset"), str) or text["asset"] not in left:
            return False
        before, after = text.get("before"), text.get("after")
        if not all(isinstance(x, str) and picture_lines(x, text["asset"]) == (0, x.split("\n")) for x in (before, after)):
            return False
        now = picture_lines(art.read()[1], text["asset"]) if art.path("article.md").is_file() else None
        if now is None or "\n".join(now[1]) != after:
            return False
    return True


def _restore_plan(art: Article, g: dict, group_id: str, paths=None) -> list[tuple]:
    """The group's files (those in `paths`, else all) and the copies it kept, all checked before
    the first write: the group's record came with the article (an import brings its own), so only
    the article's text and pictures are written, only from this group's kept copies (RC77-01)."""
    from .store import UnsafePath

    try:
        plan = [(art.own_file(rec["path"]),
                 art.own_file(rec["kept"], under=(f"{KEPT}/{group_id}/",)) if rec.get("kept") else None)
                for rec in g["files"] if paths is None or rec["path"] in paths]
    except (UnsafePath, KeyError, TypeError) as e:
        raise Refused(f"{group_id} cannot be reverted: {e}") from None
    if any(src is not None and not src.is_file() for _, src in plan):
        raise Refused(f"{group_id} cannot be reverted: a copy it kept is missing")
    return plan


def _put_back(plan: list[tuple]) -> None:
    for dst, src in plan:
        if src is not None:
            if dst.suffix == ".json" and dst.parent.name == "assets" and dst.is_file():
                # the sidecar goes back as it was; what made the bytes it replaces, which History
                # still holds, is noted beside it (R9F0-02)
                from .agents.interactive import remember_made

                newer = store.read_json_lenient(dst, None)
                if isinstance(newer, dict):
                    remember_made(dst.parent.parent, f"assets/{dst.name}", newer)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        elif dst.exists():
            dst.unlink()


def keep_as_is(slug: str, group_id: str, version: str, who: str, config=None, project=None) -> dict:
    """A change the check says left its problem in place, kept as it is by the person: it leaves
    what needs them. Changes no text; one click (the operator, 2026-10-06: a problem that is not
    fixed belongs with the problems, until someone decides)."""
    with _session(slug, "keep", group_id, config, project) as (art, cfg, proj):
        run, rv = records(art)
        check_version(art, version)
        last = (latest_review(rv) or {}).get("id")
        g = next((c for c in rv.get("changes") or [] if c.get("id") == group_id and not c.get("reverted")), None)
        if g is None or g.get("review") != last:
            raise Stale(f"{group_id} is not a change of the latest review: look at the page again")
        if g.get("check") != "still_there":
            raise Refused(f"the check does not say {group_id} left its problem in place")
        g["kept_as_is"] = {"who": who, "at": qm.now_iso()}
        run["review"] = rv
        art.save_run(run)
        return {"version": art.version(), "kept": group_id}


def revert(slug: str, group_id: str, version: str, config=None, project=None, who: str = "the Studio") -> dict:
    """Take back the latest group, while nothing has changed after it: its text and its files,
    exactly as they were. An older group is not reverted over later changes, except a picture
    group whose picture and caption are still as it left them: its files and those lines go back,
    and the rest of the text stays as it is now."""
    with _session(slug, "revert", group_id, config, project) as (art, cfg, proj):
        run, rv = records(art)
        now = check_version(art, version)
        lg = latest_group(rv)
        g = next((c for c in rv.get("changes") or [] if c.get("id") == group_id and not c.get("reverted")), None)
        how = how_it_reverts(art, rv, g, now) if g is not None else None
        if how is None:
            if lg is not None and lg.get("review") != (latest_review(rv) or {}).get("id"):
                raise Stale(f"{group_id} belongs to an earlier review: it is undone in the editor")
            if g is not None and g.get("left") and (lg is None or lg["id"] != group_id):
                raise Stale(f"the picture or the caption of {group_id} changed after it: it is undone in History")
            if lg is None or lg["id"] != group_id:
                raise Stale(f"{group_id} is not the latest change: an older change is undone in the editor")
            raise Stale(f"the article changed after {group_id}: it is undone in the editor")
        from . import versions

        if how == "latest":
            plan = _restore_plan(art, g, group_id)
            with versions.change(art, who, "Change reverted", group_id, kind="revert", group=group_id):
                _put_back(plan)
        else:
            plan = _restore_plan(art, g, group_id, paths=set(g["left"]))
            text = g.get("text")
            with versions.change(art, who, "Change reverted", group_id, kind="revert", group=group_id):
                _put_back(plan)
                if text:
                    _, body, block = art.read()
                    at, now_lines = picture_lines(body, text["asset"])
                    lines = body.split("\n")
                    lines[at:at + len(now_lines)] = text["before"].split("\n")
                    art.write_article(block, "\n".join(lines), exact=True)
        g["reverted"] = {"at": qm.now_iso(), "version": art.version()}
        still = {x for c in rv["changes"] if not c.get("reverted") for x in c["findings"]}
        for f in rv["findings"]:
            if f["id"] in g["findings"] and f["id"] not in still and f.get("status") == "changed":
                f["status"], f["note"] = "open", f"its change {group_id} was undone"
                f["note_plain"] = "its change was undone, so it is open again"
        run["review"] = rv
        art.save_run(run)
        return {"version": art.version(), "reverted": group_id}


def edit_block(slug: str, version: str, before: str, after: str, fid: str | None = None, who: str = "the Studio",
               config=None, project=None) -> dict:
    """The person's own words for one block of the text (a review card's *Edit it yourself*, or
    *Adjust it yourself* on a machine change): the block that reads `before`, found once, becomes
    `after`. With `fid`, the open finding it answers is closed as changed, by the person. A
    change of the latest review that the check says left its problem in place, and that left
    this very block, is marked adjusted by the person: it leaves Needs you (R6FA-02). A version
    of its own; a picture is not added or taken out here (that is its own button)."""
    from collections import Counter

    from . import versions

    with _session(slug, "edit", fid or "a paragraph", config, project) as (art, cfg, proj):
        run, rv = records(art)
        check_version(art, version)
        f = _current(rv, fid) if fid else None
        block, body = split_frontmatter(art.text())
        want, new = before.strip(), after.strip()
        if not new:
            raise Refused("an empty paragraph: a block is deleted in the editor")
        if new == want:
            # nothing to save: no version, and no finding or change is marked as the person's (R98D-01)
            raise Refused("nothing changed in the paragraph")
        spans = [(a, b) for a, b in paragraphs(body) if body[a:b].strip() == want]
        if len(spans) != 1:
            raise Stale("that paragraph is not in the article once any more: look at the page again")
        a, b = spans[0]
        old = body[a:b]
        lead, trail = old[:len(old) - len(old.lstrip())], old[len(old.rstrip()):]
        candidate = body[:a] + lead + new + trail + body[b:]
        # the pictures of the whole page, as it renders, remote ones too: a reference-style
        # image is defined elsewhere in the text, so the block alone does not say what it shows
        # (reviews RC77-04, R5CC-02)
        if Counter(store.shown_image_sources(body)) != Counter(store.shown_image_sources(candidate)):
            raise Refused("the edit took out or added a picture: a picture is removed or added with its own button")
        last = (latest_review(rv) or {}).get("id")
        left_here = [g for g in rv.get("changes") or [] if g.get("review") == last and not g.get("reverted")
                     and qm.half_done(g) and want in [a.strip() for a in g.get("after") or [] if isinstance(a, str)]]
        what = f"Edited by hand: {f['text']}" if f else "A paragraph edited by hand"
        with versions.change(art, who, what[:160], fid or "", kind="edit", finding=fid) as box:
            art.write_article(block, candidate)
        v = box.get("version")
        if f is not None:
            f["status"], f["note"], f["note_plain"] = "changed", None, None
            f["changed_by"] = {"who": who, "at": qm.now_iso(), "version": art.version()}
        for g in left_here:
            g["adjusted"] = {"who": who, "at": qm.now_iso(), "version_n": v["n"] if v else None}
        if f is not None or left_here:
            run["review"] = rv
            art.save_run(run)
        return {"version": art.version(), "changed": fid, "version_n": v["n"] if v else None,
                "adjusted": [g["id"] for g in left_here]}


def remove_picture_by_person(slug: str, version: str, asset: str, who: str = "the Studio", config=None,
                             project=None) -> dict:
    """A picture the person takes out of the text, with its caption (the Pictures tab): a version
    of its own, the file kept in the versions. An open finding of the latest review that asked
    for it is closed as changed, by the person."""
    from . import versions
    from .agents.fix_routing import remove_picture

    with _session(slug, "remove-picture", asset, config, project) as (art, cfg, proj):
        run, rv = records(art)
        check_version(art, version)
        if store.local_asset(asset) != asset:
            raise Refused(f"'{asset}' is not one of the article's pictures")
        block, body = split_frontmatter(art.text())
        new_body, why = remove_picture(body, asset)
        if why:
            raise Refused(why)
        with versions.change(art, who, f"Picture removed: {asset}", asset, kind="picture") as box:
            art.write_article(block, new_body)
        v = box.get("version")
        answered_by_person(art, run, rv, asset, who, "removed", version_n=v["n"] if v else None)
        return {"version": art.version(), "removed": asset, "version_n": v["n"] if v else None}


def answered_by_person(art: Article, run: dict, rv: dict, asset: str, who: str, how: str,
                       version_n: int | None = None) -> list[str]:
    """The person changed a picture (removed it, made it again, put their own in), in the version
    numbered `version_n` their act made: the latest review's open findings on it are closed as
    changed by them, so the page stops showing the old picture's problem as the new one's
    (2026-10-06). Only an act that changed something calls it: a Redo or an upload that gave the
    same picture answers nothing (R058-09). Saves the record; returns the ids."""
    last = (latest_review(rv) or {}).get("id")
    done = []
    for f in rv["findings"]:
        if f.get("review") == last and f.get("status") == "open" and (f.get("target") or {}).get("asset") == asset:
            f["status"], f["note"], f["note_plain"] = "changed", None, None
            f["changed_by"] = {"who": who, "at": qm.now_iso(), "version": art.version(), "how": how}
            done.append(f["id"])
    # a change of the review on this picture that the check says left its problem: the person's act
    # answers it too, so it leaves Needs you on Review as on Pictures (the operator, 2026-10-07)
    for g in rv.get("changes") or []:
        if (g.get("review") == last and not g.get("reverted") and qm.half_done(g)
                and asset in [str(w) for w in g.get("where") or []]):
            g["adjusted"] = {"who": who, "at": qm.now_iso(), "version_n": version_n, "how": how}
            done.append(g["id"])
    # the picture itself, with or without a finding: the review's word ("no problem found") was
    # about the old bytes (RD45-04); a later review, or other bytes, replace this record's word
    rv.setdefault("by_person", {})[asset] = {"who": who, "at": qm.now_iso(), "how": how,
                                            "sha": _sha(art.dir / asset), "version_n": version_n}
    run["review"] = rv
    art.save_run(run)
    return done


def not_important(slug: str, fid: str, version: str, who: str, why: str = "", config=None, project=None) -> dict:
    """Close an open finding as not important, with who (and why, when given): one click."""
    with _session(slug, "not-important", fid, config, project) as (art, cfg, proj):
        run, rv = records(art)
        check_version(art, version)
        f = _current(rv, fid)
        qm.close(f, who, why)
        f["closed"]["subject"] = subject_of(art, f)
        run["review"] = rv
        art.save_run(run)
        return {"version": art.version(), "closed": fid}
