"""Uniqueness report per article -> `07_plagiarism.md` + `_plagiarism.json`.

The point of this pass is not the percentage. It is the LIST OF MATCHED PASSAGES: the founder's
requirement was to see which chunks are matched so they can be rewritten, rather than being
handed a score and left to guess. So the report leads with the passages, quotes them verbatim,
names the source each came from, and separates the three cases that look identical in a score
but are completely different editorially:

  - a match against **our own** site (the project's brand.domains) — not plagiarism, and
    excluded from the API call in the first place
  - a match inside a **cited** source — the thing citation is for
  - a match against something else — the only case that needs a rewrite

The text submitted is the reader-visible prose, extracted with the SAME function the keyword
report uses, so headings, tables and anchor text are included while URLs, image markers and
frontmatter are not. Sending raw Markdown would have URLs and syntax counted as content.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from pathlib import Path

from .keyword_report import split_zones
from .tools import plagiarism as api

DEFAULTS = {
    # "уникальность в идеале 90%+" — the founder's number, so it is the warn line, not a guess
    "warn_below": 90,
    "fail_below": 80,
    # a match shorter than this is a common phrase, not copied text ("real-time translation
    # for online meetings" appears everywhere). Counted in the score by the vendor; not worth
    # an editor's attention as a passage to rewrite.
    "min_passage_words": 8,
}


def settings_from(seo: dict | None) -> dict:
    """The thresholds, plus `our_domains`: what the check does not match against (reusing our own
    phrasing is not plagiarism) — the project's domains unless the settings name others."""
    c = {**DEFAULTS, **((seo or {}).get("plagiarism") or {})}
    if not c.get("our_domains"):
        from .brand import default as _brand

        doms = list(_brand().domains)
        c["our_domains"] = doms + [f"www.{d}" for d in doms] + [h for h in (_brand().blog_host,) if h and h not in doms]
    return c


def article_text(markdown: str) -> str:
    """The reader-visible prose, in reading order."""
    z = split_zones(markdown)
    parts = [z["h1"]] + [t for _, t in z["headings"]] + [z["body"]]
    return "\n\n".join(p for p in parts if p and p.strip())


def analyse(markdown: str, cfg: dict | None = None, dry_run: bool = False) -> dict:
    """Run the check and shape the verdict. `dry_run` costs nothing and calls no API."""
    c = {**DEFAULTS, **(cfg or {})}
    text = article_text(markdown)
    est = api.estimate_credits(text)
    if dry_run:
        return {"dry_run": True, "chars": len(text), "words": len(text.split()),
                "estimated_credits": est, "excluded": c["our_domains"],
                "chunks": len(api.split_text(text))}

    parts = [api.normalise(api.check(chunk, excluded_sources=c["our_domains"]), c["our_domains"])
             for chunk in api.split_text(text)]
    data = api.merge(parts)
    data["estimated_credits"] = est
    data["thresholds"] = {k: c[k] for k in ("warn_below", "fail_below", "min_passage_words")}
    data["excluded"] = c["our_domains"]
    data["verdict"] = _verdict(data, c)
    data["passages"] = _passages(data, c)
    return data


def _verdict(d: dict, c: dict) -> str:
    u = d.get("uniqueness")
    if u is None:
        return "unknown"
    if u < c["fail_below"]:
        return "fail"
    return "warn" if u < c["warn_below"] else "ok"


def _passages(d: dict, c: dict) -> list[dict]:
    """Every matched passage worth an editor's time, longest first, deduped.

    The same sentence often matches several sources; showing it once with all of them is what
    an editor needs, and it keeps the list honest about how much text is actually affected.
    """
    seen: dict[str, dict] = {}
    for s in d["sources"]:
        if s["is_ours"] or s["is_excluded"]:
            continue
        for m in s["matches"]:
            words = len(m["text"].split())
            if words < c["min_passage_words"]:
                continue
            key = " ".join(m["text"].lower().split())
            row = seen.setdefault(key, {"text": m["text"], "words": words, "start": m["start"],
                                        "sources": [], "cited": True})
            row["sources"].append({"url": s["url"], "title": s["title"],
                                   "is_citation": s["is_citation"]})
            # a passage counts as cited only if EVERY source it matches is a cited one
            row["cited"] = row["cited"] and s["is_citation"]
    return sorted(seen.values(), key=lambda r: -r["words"])


# ---- rendering ---------------------------------------------------------------------
_MARK = {"ok": "✅", "warn": "⚠️", "fail": "❌", "unknown": "❔"}


def render(d: dict, topic: str = "") -> str:
    t = d.get("thresholds") or DEFAULTS
    u = d.get("uniqueness")
    L = [f"# Uniqueness — {topic}".rstrip(), "",
         f"**{_MARK[d['verdict']]} {d['verdict'].upper()} · uniqueness {u}%** "
         f"(target ≥{t['warn_below']}%, fails below {t['fail_below']}%)", "",
         f"- {d['plagiarised_words']} of {d['total_words']} words matched something online "
         f"({d.get('identical_words')} identical, {d.get('similar_words')} similar) "
         f"across {d['source_count']} source(s)",
         f"- Checked with {d['provider']}; our own domains were excluded from matching: "
         f"{', '.join(d.get('excluded') or [])}",
         f"- Cost {d.get('credits_used')} credits, {d.get('credits_remaining')} remaining"]
    if d.get("chunks", 1) > 1:
        L.append(f"- The article exceeded the per-request limit and was scanned in "
                 f"{d['chunks']} parts; the percentage is over the whole text")
    hc = d.get("hidden_characters") or {}
    if hc.get("zero_width_space") or hc.get("homoglyph"):
        L += ["", f"> ⚠️ **Invisible characters found** — zero-width spaces: "
                  f"{hc.get('zero_width_space')}, look-alike letters: {hc.get('homoglyph')}. "
                  f"These do not belong in our text and are worth stripping before publishing."]
    L.append("")

    passages = d.get("passages") or []
    rewrite = [p for p in passages if not p["cited"]]
    L += [f"## Passages to rewrite ({len(rewrite)})", "",
          f"_Matched runs of {t['min_passage_words']}+ words, longest first, excluding anything "
          f"matched only inside a source the article cites. Shorter matches are common phrasing "
          f"and are left out of this list even though the vendor counts them in the score._", ""]
    if not rewrite:
        L.append("_Nothing above the threshold. Rewriting is not needed._")
    for p in rewrite:
        srcs = ", ".join(f"[{(s['title'] or s['url'])[:60]}]({s['url']})" for s in p["sources"][:3])
        L += [f"- **{p['words']} words** — matches {srcs}", f"  > {p['text']}"]
    L.append("")

    cited = [p for p in passages if p["cited"]]
    if cited:
        L += [f"## Matched, but inside a cited source ({len(cited)})", "",
              "_Left alone: quoting a source the article links to is what citation is for._", ""]
        for p in cited[:15]:
            L.append(f"- {p['words']} words from "
                     + ", ".join(f"[{(s['title'] or s['url'])[:50]}]({s['url']})"
                                 for s in p["sources"][:2]))
        L.append("")

    L += ["## Every source that matched", "",
          "| Matched words | Identical | Similar | Cited | Ours | Source |",
          "|---|---|---|---|---|---|"]
    for s in d["sources"]:
        L.append(f"| {s['words']} | {s.get('identical')} | {s.get('similar')} | "
                 f"{'yes' if s['is_citation'] else ''} | {'yes' if s['is_ours'] else ''} | "
                 f"[{(s['title'] or s['url'] or '')[:70]}]({s['url']}) |")
    return "\n".join(L) + "\n"


def text_sha(markdown: str) -> str:
    """The version of the text the check reads (the article's prose, not its frontmatter)."""
    import hashlib

    return hashlib.sha256(article_text(markdown).encode("utf-8")).hexdigest()[:16]


# ---- a result after the text moved ----------------------------------------------------
# a word as the check may count it: the longest count splits at every non-letter and gives each
# CJK character its own word; the shortest counts each space-separated token with a letter or a
# digit (Codex R11F-01: the check's own units are not ours, so the bound holds for any of them
# between, and is given only when the check's count of the checked text lies between: R8B9-01)
_CJK = "\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\uf900-\ufaff"
_FINE = re.compile(rf"[{_CJK}]|[^\W{_CJK}]+")


def _signs(text: str) -> int:
    """Tokens of signs alone ("—", "---"): a check may count each as a word."""
    return sum(1 for w in text.split() if not re.search(r"\w", w))


def _most_words(text: str) -> int:
    return len(_FINE.findall(text)) + _signs(text)


def _least_words(text: str) -> int:
    return sum(1 for w in text.split() if re.search(r"\w", w))


# the scripts that put spaces between words, where the two counts above bound any count of a
# word: a text in another (Thai, Lao, Khmer, Myanmar, Tibetan...) gets no bound (Codex R8B9-01)
_SPACED = ("LATIN", "CYRILLIC", "GREEK", "ARMENIAN", "GEORGIAN", "HEBREW", "ARABIC", "DEVANAGARI",
           "BENGALI", "GURMUKHI", "GUJARATI", "TAMIL", "TELUGU", "KANNADA", "MALAYALAM", "HANGUL")


def _spaced(text: str) -> bool:
    import unicodedata

    for ch in set(text):
        if ch.isalpha() and not re.match(rf"[{_CJK}]", ch):
            if not unicodedata.name(ch, "").startswith(_SPACED):
                return False
    return True


def _units(markdown: str) -> list[str]:
    """The text's sentences as a reader sees them: the title, each heading, each sentence of a
    paragraph with its soft line breaks joined, each list item and table cell (keyword_report's
    units, on the prose the check reads: Codex R11F-02); a quote's lines are one paragraph, as the
    page's parser reads them (R8B9-02)."""
    z = split_zones(re.sub(r"^[ \t]*(?:>[ \t]?)+", "", markdown or "", flags=re.M))
    raw = [z["h1"]] + [t for _, t in z["headings"]] + list(z["body_units"])
    return [t for t in (" ".join(str(x).split()) for x in raw) if t]


def _checked_text(art, result: dict) -> tuple[int | None, str] | None:
    """(its version number, its Markdown) of the text the check behind `result` read: the version
    the result names, else the newest kept version whose prose has the result's hash, else the
    text the result kept itself (a draft that was no version: Codex R11F-03)."""
    from . import versions

    sha = result.get("text_sha")
    if not sha:
        return None
    named = result.get("version_n")
    order = sorted(versions.index(art), key=lambda v: (v.get("n") != named, -int(v.get("n") or 0)))
    for v in order:
        try:
            md = versions.text_of(art, v["n"])
        except (KeyError, ValueError):          # not kept, or its kept bytes are damaged
            continue
        if text_sha(md) == sha:
            return v["n"], md
    own = result.get("checked_markdown")
    if isinstance(own, str) and text_sha(own) == sha:
        return None, own
    return None


def since_check(art, result: dict, markdown: str) -> dict | None:
    """What changed since the check behind `result`, for a text that moved after it (operator,
    2026-10-23: "not checked" after every edit alarmed people): {"n": the version it read (None
    for a draft the result kept), "new_words" and "new" (their share, in %): the words of the
    prose now that the checked text does not hold (a sentence it does not have word for word, or
    one more copy of a sentence than it had: Codex R9D2-01), "lowest": the lowest uniqueness the
    prose now can have, were every new word found elsewhere and every match the check found
    still in place}; {"n", "empty": True} when the text now has no prose to compare (R11F-04).
    None when the text it read cannot be found. The new words are counted at their most and the
    kept ones at their least, so the bound holds whatever the check calls a word (R11F-01)."""
    got = _checked_text(art, result)
    if got is None or result.get("uniqueness") is None:
        return None
    n, checked = got
    old, now = _units(checked), _units(markdown)
    left = Counter(t.lower() for t in old)
    kept = new = 0
    for t in now:
        if left[t.lower()] > 0:
            left[t.lower()] -= 1
            kept += _least_words(t)
        else:
            new += _most_words(t)
    if not kept + new:
        return {"n": n, "empty": True}
    # the prose the check reads holds signs the sentences leave out (a table's "---"): the ones
    # the text gained count as new words
    old_prose, now_prose = article_text(checked), article_text(markdown)
    new += max(0, _signs(now_prose) - _signs(old_prose))
    # a bound only where our counts bound the check's own: every word in a spaced script, and the
    # check's count of the prose it read between ours (Codex R8B9-01); else no promise
    tw = result.get("total_words")
    if not _spaced(old_prose + now_prose) or (
            isinstance(tw, (int, float)) and not _least_words(old_prose) <= tw <= _most_words(old_prose)):
        return {"n": n, "new_words": new, "new": 100 * new / (kept + new)}
    # the matches the check found, in its own words when it kept the count, else from its
    # percentage at the safe end of its rounding, over the most words the old text can have
    pw = result.get("plagiarised_words")
    matched = (float(pw) if isinstance(pw, (int, float)) and pw >= 0 else
               math.ceil((100 - float(result["uniqueness"]) + 0.005) / 100 * sum(_most_words(t) for t in old)))
    lowest = 100 * (1 - (min(kept, matched) + new) / (kept + new))
    return {"n": n, "new_words": new, "new": 100 * new / (kept + new), "lowest": max(0, math.floor(lowest))}


def bind_version(art, n: int) -> None:
    """The result of a check made before its text was a version (the pipeline checks the finished
    text, then keeps it as the first version): version `n` is the text it read, when the prose is
    the same (Codex R9D2-04)."""
    from .store import read_json_lenient, write_json

    p = art.path("_plagiarism.json")
    data = read_json_lenient(p, {}) or {}
    if data and data.get("version_n") is None and data.get("text_sha") == text_sha(art.text()):
        write_json(p, {**data, "version_n": n})


def write_report(article_dir, data: dict, topic: str = "") -> None:
    from .store import atomic_write_text, write_json

    d = Path(article_dir)
    atomic_write_text(d / "07_plagiarism.md", render(data, topic or d.name))
    write_json(d / "_plagiarism.json", data)


def build(article_dir, seo: dict | None = None, dry_run: bool = False,
          write: bool = True) -> dict:
    from .store import Article, read_json_lenient

    d = Path(article_dir)
    src = Article(d.parent, d.name).source
    if src is None:
        raise FileNotFoundError(f"no article.md or 04_draft.md in {d}")
    topic = (read_json_lenient(d / "_run.json", {}) or {}).get("topic") or d.name
    md = src.read_text(encoding="utf-8")
    data = analyse(md, settings_from(seo), dry_run=dry_run)
    data["slug"] = d.name
    data["text_sha"] = text_sha(md)     # the scorecard marks the result stale once the text moves
    # the version it read, so a later page can say what changed since (since_check)
    if src.name == "article.md":
        from . import versions

        raw = hashlib.sha256(src.read_bytes()).hexdigest()
        data["version_n"] = next((v["n"] for v in reversed(versions.index(Article(d.parent, d.name)))
                                  if (v.get("files") or {}).get("article.md") == raw), None)
    if data.get("version_n") is None and not dry_run:
        data["checked_markdown"] = md   # a text no version holds (a draft): kept to compare later
    if write and not dry_run:
        write_report(d, data, topic)
    return data
