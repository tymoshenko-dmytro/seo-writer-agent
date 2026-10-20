"""The QA model: what the review found, what the machine changed, and which version a decision
is about.

Three facts are kept apart and never folded into one flag again:

  1. **QA**: the review's findings and the change groups the machine made for them, in the run
     record (`_run.json` -> `review`), and the score as a hint;
  2. **the person's review**: `accepted`, the version a person marked as good;
  3. **WordPress**: `published`, what the Studio last sent and what it last knew of the post.

The last two live in the article's own record (`_article.json`, `store.Article.record()`): a
regeneration rewrites `_run.json`, and neither a person's acceptance nor a post on WordPress is a
fact about one run.

A finding is plain data:

    {"id", "text", "kind", "target", "action", "phrase", "severity", "status", "closed",
     "source", "review", "section", "basis", "check", "needs_choice"}

`needs_choice` is set when an attempt showed the change cannot stay inside its target (it reached
outside its paragraph, its section or its picture): from then on it is a proposal. It is set too
when the reads asked different things of one picture (remove it, redraw it): the finding keeps the
picture as its target, no action, and `choices` ([{"action", "text"}], each read's words), and the
person picks one on the card.

`kind`, `target` and `action` come from the STRUCTURED answer of the review (its output
contract, prompts/editor.md) or of the picture check (agents/visual_checker), never from the
prose of the finding: the word rules that read "merge", "300 words" or "chart" out of a finding's
wording are what this model replaces.

What the machine may do with a finding is `route()`: `machine` (on the safe list, bounded to its
target: applied in the working version after its own review), `proposal` (needs the person's
choice: a picture removal, a changed factual claim, anything off the safe list or without a
clear target) or `manual` (an outline change: the person makes it in the editor). Only a finding
of the review that just ran is ever applied without a click (`machine_may_apply`). The old
checklist is not carried into this model: an article from before it keeps its old findings as
history only (core/qa_migration.py).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

KINDS = ("text", "fact", "link", "keyword", "picture", "structure", "meta")
ACTIONS = ("edit", "cite", "insert_keyword", "redraw", "recapture", "remove_picture", "add_picture", "outline")
STATUSES = ("open", "changed", "not_important")
SOURCES = ("reader", "picture_check", "code")
CHECK_RESULTS = ("fixed", "still_there", "not_checked")
SEVERITIES = ("critical", "major", "minor", "nitpick", "unknown")
# what a finding takes off the score while it is open: the editor rubric's own deductions (an
# unknown severity costs like a Major)
POINTS = {"critical": 15, "major": 7, "minor": 3, "nitpick": 1, "unknown": 7}

# what each action works on: a span quoted from the article, a section heading, or a picture file
TARGET_TYPES = {"edit": "quote", "cite": "quote", "insert_keyword": "heading", "outline": "heading",
                "redraw": "asset", "recapture": "asset", "remove_picture": "asset",
                # the words the new picture goes after (a picture the article lacks, 2026-10-06)
                "add_picture": "quote"}
# Contract 2: what the machine applies without asking, each bounded to its target. Everything
# else is a proposal: `remove_picture`, any `edit` of a `fact` (a changed claim), any other pair.
SAFE = {("edit", "text"), ("edit", "keyword"), ("edit", "meta"),
        ("cite", "fact"), ("cite", "link"),
        ("insert_keyword", "keyword"),
        ("redraw", "picture"), ("recapture", "picture")}
# the picture check's own vocabulary (agents/visual_checker.ACTIONS): a cover is recomposed,
# which is a redraw of the cover file
PICTURE_ACTIONS = {"redraw": "redraw", "recapture": "recapture", "recompose": "redraw",
                   "remove": "remove_picture"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def norm_severity(sev) -> str:
    s = str(sev or "").strip().lower()
    return s if s in SEVERITIES else "unknown"


# the order a person meets them in: the most important first; an unknown one weighs as a major
# (POINTS), so it sorts with them (testers, 2026-10-20: "Critical issues should come first")
_RANK = {"critical": 0, "major": 1, "unknown": 1, "minor": 2, "nitpick": 3}


def severity_rank(sev) -> int:
    return _RANK[norm_severity(sev)]


def worst(severities) -> str:
    """The most important of several severities ("unknown" when there are none)."""
    found = [norm_severity(s) for s in severities if s]
    return min(found, key=lambda s: (_RANK[s], s != "major")) if found else "unknown"


def norm_basis(b) -> str | None:
    """Why a sourcing finding was raised: a claim the research is silent on is UNKNOWN, not false
    (measured: of 56 flagged third-party claims, 21 were true); a claim a listed source
    contradicts is a real error."""
    t = str(b or "").strip().lower().replace("-", "_").replace(" ", "_")
    if t in ("contradicted", "contradicts", "contradicted_by_source"):
        return "contradicted"
    if t in ("not_in_research", "not_in_corpus", "unsourced", "research_gap", "gap"):
        return "not_in_research"
    return "other" if t == "other" else None


def as_finding(raw) -> dict:
    """One read's finding (a reader's, the code's, the picture check's) normalized: {fix,
    severity, check, section, source, voice, hinted, basis, asset, action}. `voice` names the
    read that raised it and `hinted` whether that read carried the code's attention hints."""
    if isinstance(raw, str):
        return {"fix": raw.strip(), "severity": "unknown", "check": None, "section": None,
                "source": "editor", "voice": None, "hinted": False, "basis": None}
    d = raw or {}
    return {"fix": str(d.get("fix") or d.get("text") or "").strip(),
            "severity": norm_severity(d.get("severity")),
            "check": d.get("check"), "section": d.get("section"),
            "source": d.get("source") or "editor",
            "voice": d.get("voice"), "hinted": bool(d.get("hinted")),
            "basis": norm_basis(d.get("basis")), "asset": d.get("asset"), "action": d.get("action")}


def instruction(item: dict) -> str:
    """What the fixer is asked to do: the latest wording when a later read raised the finding in
    other words (it quotes the text as it is now), with the first wording after it."""
    text, latest = item.get("text") or "", item.get("latest_text")
    return f"{latest}\n(First raised as: {text})" if latest and latest != text else text


def _word(value, allowed) -> str | None:
    """A value from a fixed list, or None: "Insert-Keyword" reads as insert_keyword, anything
    outside the list as nothing (an unknown action is never guessed into a known one)."""
    t = re.sub(r"[\s-]+", "_", str(value or "").strip().lower())
    return t if t in allowed else None


# ---------------------------------------------------------------------------------------------
# targets
# ---------------------------------------------------------------------------------------------
_BARE_FILE = re.compile(r"^[^/]+\.(?:png|jpe?g|webp|gif|svg)$", re.I)


def norm_target(value, action: str | None) -> dict | None:
    """The target the action works on, typed by the action: {"quote": exact words},
    {"heading": a section's heading} or {"asset": "assets/<file>"}. The structured answer gives
    it as a string (or already typed); None when it is missing or not the type the action needs."""
    want = TARGET_TYPES.get(action or "")
    if not want:
        return None
    if isinstance(value, dict):
        value = value.get(want)
    if not isinstance(value, str):
        return None
    if want == "quote":
        return {"quote": value} if value.strip() else None
    if want == "heading":
        h = re.sub(r"^#{1,6}\s*", "", value.strip()).strip()
        return {"heading": h} if h else None
    from .store import local_asset

    v = value.strip()
    if _BARE_FILE.match(v):
        v = "assets/" + v
    asset = local_asset(v)          # the file as the page reads its address (store.local_asset)
    return {"asset": asset} if asset else None


def target_label(target: dict | None) -> str:
    """The target as one short line for a page or a log."""
    if not target:
        return "(no target)"
    if "asset" in target:
        return target["asset"]
    if "heading" in target:
        return f"the section '{target['heading']}'"
    q = " ".join(str(target.get("quote") or "").split())
    return f"«{q if len(q) <= 80 else q[:79] + '…'}»"


def _headings(body: str) -> list[str]:
    """The body's headings as the page renders them (a `#` line inside a code block is none)."""
    from .tools.markdown_render import parse

    tokens = parse(body or "")
    return [" ".join(tokens[i + 1].content.split()) for i, t in enumerate(tokens)
            if t.type == "heading_open" and i + 1 < len(tokens) and tokens[i + 1].type == "inline"]


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def locate(target: dict | None, text: str, cover: str | None = None) -> int:
    """How many times the target occurs in the article: a quote by its exact words in the body
    and in the frontmatter's text (the title, the meta fields, the FAQ), a heading among the
    headings the page renders, a picture once for every time the page shows it
    (store.shown_images, the same reading as the version's) and once more as the cover. `cover`
    is the article's (store.Article.cover()); without it, the frontmatter's. A machine change
    needs exactly one."""
    from .store import cover_asset, frontmatter_fields, shown_images, split_frontmatter

    if not target or not text:
        return 0
    block, body = split_frontmatter(text)
    fields = frontmatter_fields(block)
    if "quote" in target:
        q = target["quote"]
        return sum(s.count(q) for s in [body, *_strings(fields)]) if q else 0
    if "heading" in target:
        want = " ".join(target["heading"].split())
        return sum(1 for h in _headings(body) if h == want)
    if "asset" in target:
        cover = cover if cover is not None else cover_asset(fields, None)
        return shown_images(body).count(target["asset"]) + (1 if cover == target["asset"] else 0)
    return 0


# ---------------------------------------------------------------------------------------------
# the structured answers
# ---------------------------------------------------------------------------------------------
def structured_answer(raw: dict) -> dict | None:
    """kind, action, target and phrase as one review read gave them (the output contract of
    prompts/editor.md), each held to its list; None when the answer carries none of them (a read
    from before the contract, a plain-string finding). Never read from the finding's prose."""
    if not isinstance(raw, dict) or not any(raw.get(k) for k in ("kind", "action", "target")):
        return None
    action = _word(raw.get("action"), ACTIONS)
    phrase = str(raw.get("phrase") or "").strip() if action == "insert_keyword" else ""
    return {"kind": _word(raw.get("kind"), KINDS), "action": action,
            "target": norm_target(raw.get("target"), action), "phrase": phrase or None}


def picture_answer(asset: str | None, action: str | None) -> dict | None:
    """The picture check's answer (its action about the file it was shown) in this model's
    terms; None for an entry without both (the check's first releases)."""
    mapped = PICTURE_ACTIONS.get(str(action or ""))
    target = norm_target(asset, mapped) if mapped else None
    if not (mapped and target):
        return None
    return {"kind": "picture", "action": mapped, "target": target, "phrase": None}


# ---------------------------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------------------------
def finding(*, id: str, text: str, review: str, source: str, severity=None, kind=None,
            action=None, target=None, phrase=None, section=None, basis=None, check=None) -> dict:
    """A new open finding. `kind`, `action` and `target` outside their lists are kept as None
    (a proposal), never coerced."""
    action = _word(action, ACTIONS)
    rec = {"id": str(id), "text": str(text or "").strip(),
           "kind": _word(kind, KINDS), "action": action,
           "target": norm_target(target, action) if target is not None else None,
           "phrase": (str(phrase).strip() or None) if phrase and action == "insert_keyword" else None,
           "severity": norm_severity(severity), "status": "open", "closed": None,
           "source": source if source in SOURCES else "reader", "review": str(review),
           "section": section or None, "basis": basis or None, "check": check or None,
           "needs_choice": None}
    return rec


def close(item: dict, who: str, why: str = "", at: str | None = None) -> dict:
    """Not important: closed by a person, with who and, when they gave one, why. The reason is not
    asked for (the operator, 2026-10-06): it changed nothing, the finding matcher never read it."""
    why = " ".join(str(why or "").split())
    item["status"] = "not_important"
    item["closed"] = {"who": who or "operator", "why": why, "at": at or now_iso()}
    return item


def review_record(*, id: str, version: str, at: str | None = None, **extra) -> dict:
    """One review: the panel's reads, the picture check and the code checks on one version."""
    return {"id": str(id), "at": at or now_iso(), "version": version, **extra}


def next_review_id(reviews: list[dict]) -> str:
    """r1, r2, …: never an id an earlier review of this article had, so a proposal shown under
    one review never takes a meaning from another (contract 1)."""
    n = [int(m.group(1)) for r in reviews or [] if (m := re.fullmatch(r"r(\d+)", str(r.get("id"))))]
    return f"r{max(n, default=0) + 1}"


def change_group(*, id: str, findings: list[str], before, after, version_before: str,
                 version_after: str, files: list[dict] | None = None, check: str = "not_checked",
                 review: str | None = None, at: str | None = None, where: list | None = None,
                 **extra) -> dict:
    """The changes made for one finding, or for findings on the same paragraph or picture.
    `where`, `before` and `after` say what changed, one entry per place (a paragraph, a section,
    a picture). `files` are the files the group changed: [{"path", "kept"}], `kept` the copy of
    the previous bytes (None for a file the group created). `check` is the check's opinion of
    the result (fixed / still_there / not_checked); it changes no status."""
    if check not in CHECK_RESULTS:
        raise ValueError(f"unknown check result '{check}'")
    if not findings:
        raise ValueError("a change group is made for at least one finding")
    return {"id": str(id), "findings": [str(f) for f in findings], "where": list(where or []),
            "before": before, "after": after, "files": list(files or []), "check": check,
            "version_before": version_before, "version_after": version_after, "review": review,
            "at": at or now_iso(), "reverted": None, **extra}


def half_done(group: dict) -> bool:
    """A change the check says left its problem in place, which the person has neither kept as it
    is nor adjusted by hand: one of the things that need them (the operator, 2026-10-06). The
    Review tab, the counts, the agent's view and Before a send all ask this (R6FA-02, R6FA-03)."""
    return group.get("check") == "still_there" and not group.get("kept_as_is") and not group.get("adjusted")


def accepted_record(version: str, who: str, origin: str, at: str | None = None) -> dict:
    """A person's mark "I looked, this version is good" (or, once, the migration's carry-over of
    an old automatic READY: `origin` says which)."""
    if not version:
        raise ValueError("an acceptance is of one version")
    return {"version": version, "who": who, "at": at or now_iso(), "origin": origin}


def published_record(post_id, wp_status=None, sent_version=None, sent_at=None,
                     last_known: dict | None = None, **extra) -> dict:
    """What the Studio knows of the article's WordPress post: what it last sent (`sent_version`,
    `sent_at`; None = unknown) and the post's status as last read (`last_known`: {status,
    checked_at}; None until a check reads the post)."""
    return {"post_id": post_id, "wp_status": wp_status, "sent_version": sent_version,
            "sent_at": sent_at, "last_known": last_known, **extra}


# ---------------------------------------------------------------------------------------------
# what may happen to a finding
# ---------------------------------------------------------------------------------------------
_WHAT = {"text": "the wording", "fact": "a factual claim", "link": "a link", "keyword": "a keyword",
         "picture": "a picture", "structure": "the structure", "meta": "the meta tags"}


def route(item: dict, text: str | None = None, cover: str | None = None) -> tuple[str, str | None]:
    """("machine" | "proposal" | "manual", why). With the article's text, a target that is not
    found exactly once is a proposal too. Decided by the structured fields alone."""
    action, kind, target = item.get("action"), item.get("kind"), item.get("target")
    if item.get("needs_choice"):
        return "proposal", item["needs_choice"]
    if action == "outline":
        return "manual", "an outline change (merge, move or reorder sections) is made in the editor"
    if not action:
        return "proposal", "the review named no action from the list"
    if action == "remove_picture":
        return "proposal", "it removes a picture"
    if action == "add_picture":
        return "proposal", "it adds a picture"
    if kind == "fact" and action == "edit":
        return "proposal", "it changes a factual claim"
    if (action, kind) not in SAFE:
        # in the person's words: "'edit' of a 'link' finding is not on the safe list" read as code
        return "proposal", f"it changes {_WHAT.get(kind, 'something')}, which the machine leaves to you"
    if not target:
        return "proposal", "the target is missing or not what this action works on"
    if action == "insert_keyword" and not item.get("phrase"):
        return "proposal", "no keyword phrase is named"
    if text is not None:
        n = locate(target, text, cover)
        if n != 1:
            return "proposal", (f"{target_label(target)} is not in the article" if n == 0 else
                                f"{target_label(target)} occurs {n} times in the article")
    return "machine", None


def machine_may_apply(item: dict, review: dict | None, text: str | None = None,
                      cover: str | None = None) -> bool:
    """Whether the machine applies this finding without a click: open, on the safe list with its
    target found once, and raised by `review`, the review that just ran (a proposal shown under one
    review never takes effect under another)."""
    return bool(review and item.get("status") == "open"
                and item.get("review") == review.get("id") and route(item, text, cover)[0] == "machine")


def points(item: dict) -> int:
    return POINTS.get(norm_severity(item.get("severity")), 7)


def score(findings: list[dict]) -> int:
    """The hint: 100 minus the open findings' points."""
    return max(0, 100 - sum(points(f) for f in findings or [] if f.get("status") == "open"))
