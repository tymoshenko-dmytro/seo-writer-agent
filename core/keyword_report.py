"""Keyword coverage report — does the finished article actually contain what it targets?

A script, not an agent: pure counting, no model call, no cost. The Editor judges whether the
keywords read naturally; this answers the separate question the SEO team kept having to answer
by reading the piece — which targets landed, how many times, and where.

Three things make the numbers trustworthy rather than decorative:

1. **Zones are separated.** A keyword in an H2 is not the same signal as a keyword in body
   prose, so headings, H1, body, image alt text and the meta tags are counted apart. The SEO
   team asked for headings and body specifically.
2. **URLs are excluded.** `[real-time translation](…/real-time-translation-api)` would
   otherwise count the phrase twice: once as anchor text and once inside the slug. Only the
   anchor text is text on the page.
3. **Plurals are folded, word families are not.** "apps" counts towards "app", but
   "translation" does NOT count towards "translator" — merging a word family would inflate
   every count for a topic like ours, where half the vocabulary shares a stem.

The thresholds are OURS, set in `config.yaml` → `seo.keywords` and editable in the Studio.
They are a house style we can defend, not an external standard: nobody publishes the real
weighting, and quoting a number as if an authority set it would be an invention.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

# ---- thresholds (defaults; overridable via seo.keywords) ----------------------------
DEFAULTS = {
    "primary_min_total": 3,        # times the primary keyword should appear at all
    "primary_density_min": 0.5,    # % — REPORTED, not enforced (see _checks: a long-tail
                                   # phrase legitimately lands few times, so the count check
                                   # above is the real floor and this would only add noise)
    "primary_density_max": 2.5,    # % — ENFORCED as a ceiling: above this it reads as stuffing
    "primary_in_h1": True,         # the H1 must carry the primary keyword
    "primary_in_headings_min": 1,  # plus at least this many H2/H3s
    "primary_in_first_words": 100, # and it should appear in the opening
    "secondary_coverage_min": 50,  # % of the analyst's secondary targets present at least once
    "lsi_coverage_min": 30,        # % of the LSI vocabulary present
    "section_keyword_coverage_min": 50,  # % of per-section brief keywords present
    # The ranked keyword pool has NO threshold on purpose: it is 60-130 long-tail Ahrefs
    # phrases and an article will never contain most of them verbatim, so any bar would warn
    # on every single run. It is reported for information.
    # "mandatory" (a list, no default) holds keywords SEO decided every article must carry. It
    # is project-wide until Studio can pause before the brief, so any keyword in it blocks the
    # gate of every article that lacks it; core/settings.py declares only numeric keywords.*
    # keys and must not list this one without a list type.
}

from .store import FRONTMATTER as _FM  # noqa: E402
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_FENCE = re.compile(r"```.*?```", re.DOTALL)
_IMG = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\((?:[^)]*)\)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_ITALIC_LINE = re.compile(r"^\*[^*].*\*$")
_WORD = re.compile(r"[a-z0-9]+")
_SENTENCE = re.compile(r"(?<=[.!?;:])\s+|\n")


def _norm(tok: str) -> str:
    """Fold plurals only. Deliberately NOT a stemmer.

    "apps" -> "app" and "APIs" -> "api" are the same word to a reader and to a search engine.
    "translation" -> "translat" would also swallow "translator" and "translate", which on this
    blog would turn three distinct targets into one inflated count.
    """
    if len(tok) > 4 and tok.endswith("ies"):
        return tok[:-3] + "y"
    if len(tok) > 4 and tok.endswith(("ches", "shes", "sses", "xes", "zes")):
        return tok[:-2]
    # -us/-is are usually a Latin singular ("status", "analysis", "basis"), so they are left
    # alone — EXCEPT for short words, where the plural reading is the right one. Without this
    # "APIs" never folded to "api", and on this blog that is one of the most common targets.
    latin = tok.endswith(("us", "is")) and len(tok) >= 5
    if len(tok) > 2 and tok.endswith("s") and not tok.endswith("ss") and not latin:
        return tok[:-1]
    return tok


def tokens(text: str) -> list[str]:
    return [_norm(t) for t in _WORD.findall((text or "").lower())]


def mark_mandatory(keywords, mandatory) -> str:
    """Section keywords as the brief and the writer show them, SEO-mandatory ones marked."""
    mand = {" ".join(tokens(str(m))) for m in (mandatory or [])}
    return ", ".join(f"{k} (mandatory)" if " ".join(tokens(str(k))) in mand else str(k)
                     for k in (keywords or []))


def _plain(text: str) -> str:
    """Markdown -> the words a reader sees. Anchor text survives; URLs do not."""
    text = _LINK.sub(r"\1", text or "")
    text = re.sub(r"[*_`>#]", " ", text)
    text = re.sub(r"^\s*([-+*]|\d+\.)\s+", " ", text, flags=re.M)   # list markers
    text = text.replace("|", " ")                                    # table pipes
    return re.sub(r"[ \t]+", " ", text)


def split_zones(markdown: str) -> dict:
    """Separate the places a keyword can sit, because they are not the same signal."""
    md = _FENCE.sub(" ", _COMMENT.sub(" ", _FM.sub("", markdown or "")))

    alt: list[str] = []
    md = _IMG.sub(lambda m: (alt.append(m.group(1)), "\n")[1], md)

    h1, headings, body = "", [], []
    for line in md.split("\n"):
        m = _HEADING.match(line)
        if m:
            level, text = len(m.group(1)), _plain(m.group(2)).strip()
            if level == 1 and not h1:
                h1 = text
            else:
                headings.append((level, text))
            continue
        if _ITALIC_LINE.match(line.strip()):     # an image caption line
            alt.append(_plain(line).strip())
            continue
        body.append(line)

    return {
        "h1": h1,
        "headings": headings,
        "headings_text": " \n ".join(t for _, t in headings),
        "body": _plain("\n".join(body)).strip(),
        "body_units": units("\n".join(body)),
        "alt": " \n ".join(a for a in alt if a),
    }


def count_phrase(hay_tokens: list[str], needle: list[str]) -> int:
    """Contiguous occurrences of the phrase (plural-folded) — SEO's "phrase match"."""
    n = len(needle)
    if not n or len(hay_tokens) < n:
        return 0
    return sum(1 for i in range(len(hay_tokens) - n + 1) if hay_tokens[i:i + n] == needle)


# the small words a sentence may need between a keyword's words ("video remote interpreting in
# healthcare" for "video remote interpreting healthcare"), and the "s" of a possessive
SMALL_WORDS = frozenset({"a", "an", "the", "in", "for", "of", "on", "to", "with", "and", "at", "by", "from",
                         "your", "our", "their", "its", "into", "via", "as", "s"})
CLOSE_GAP = 2                         # small words allowed between two of the keyword's words
_SMALL = frozenset(_norm(w) for w in SMALL_WORDS)   # as `tokens` gives them ("its" folds to "it", RE56-03)


def count_close(hay_tokens: list[str], needle: list[str]) -> int:
    """Occurrences of the phrase as a person writes it: its words in order, with at most
    CLOSE_GAP small words between neighbours, the exact phrase included. The exact-phrase rule
    made the machine write "means translation in real time:" and "video remote interpreting
    healthcare deployments" (2026-10-05); a reader and a search engine read the variant as the
    same words."""
    n = len(needle)
    if not n:
        return 0
    hits, i = 0, 0
    while i < len(hay_tokens):
        if hay_tokens[i] != needle[0]:
            i += 1
            continue
        j, k = i + 1, 1
        while k < n and j < len(hay_tokens):
            gap = 0
            while j < len(hay_tokens) and hay_tokens[j] != needle[k] and hay_tokens[j] in _SMALL and gap < CLOSE_GAP:
                j, gap = j + 1, gap + 1
            if j < len(hay_tokens) and hay_tokens[j] == needle[k]:
                j, k = j + 1, k + 1
            else:
                break
        if k == n:
            hits += 1
            i = j
        else:
            i += 1
    return hits


_SENTENCE_END = re.compile(r"(?<=[.!?;:])\s+")
_BLOCK_START = re.compile(r"\s*(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|>)")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")


def units(markdown: str) -> list[str]:
    """The text in the units a phrase can live in, as a reader sees them: each sentence of a
    paragraph (its soft line breaks joined, RD45-01), each heading and list item, each table cell.
    Run together, "We provide remote interpreting. For healthcare, …" said "remote interpreting
    for healthcare" (RE56-02)."""
    out: list[str] = []

    def sentences(text: str) -> None:
        out.extend(u for u in _SENTENCE_END.split(_plain(text)) if u.strip())

    for block in re.split(r"\n[ \t]*\n", markdown or ""):
        cur: list[str] = []
        lines = block.split("\n")
        table = False                   # inside a table: from its header row to the block's end
        for i, line in enumerate(lines):
            # a table as the renderer reads it: a header row with a pipe over a delimiter row
            # ("--- | ---"); the outer pipes are optional (R597-01)
            if not table and "|" in line and i + 1 < len(lines) and "|" in lines[i + 1] and _TABLE_RULE.match(lines[i + 1]):
                table = True
            if table or line.strip().startswith("|"):              # a table row: its cells apart
                sentences(" ".join(cur)) if cur else None
                cur = []
                if not _TABLE_RULE.match(line):
                    for cell in line.split("|"):
                        sentences(cell)
            elif re.match(r"\s*#{1,6}\s", line):                  # a heading: a unit of its own
                sentences(" ".join(cur)) if cur else None
                sentences(line)
                cur = []
            elif _BLOCK_START.match(line):                         # a list item, a quote: its lines join
                sentences(" ".join(cur)) if cur else None
                cur = [line]
            else:
                cur.append(line)                                   # a soft break: the same sentence
        if cur:
            sentences(" ".join(cur))
    return out


def close_count(text_units: list[str], needle: list[str]) -> tuple[int, int]:
    """(exact, close) occurrences of `needle` over `text_units`, each unit on its own: `close`
    counts the natural variants that are not the exact phrase."""
    exact = close = 0
    for u in text_units:
        toks = tokens(u)
        e = count_phrase(toks, needle)
        exact += e
        close += count_close(toks, needle) - e
    return exact, close


def phrase_count(text: str, phrase: str) -> int:
    """How many times `text` says `phrase`, exactly or as a natural close variant (`count_close`),
    never across a sentence, a line or a table cell."""
    exact, close = close_count(units(text), tokens(phrase))
    return exact + close


def count_scattered(text: str, needle: list[str]) -> int:
    """Sentences containing every word of the phrase but NOT as the phrase itself.

    Worth reporting separately: it is real topical coverage, and it is often why a keyword
    looks absent when the ground is in fact covered — but it is not the phrase ranking for it.
    """
    if len(needle) < 2:
        return 0
    hits = 0
    for sentence in _SENTENCE.split(text or ""):
        toks = tokens(sentence)
        if not toks or count_phrase(toks, needle):
            continue
        if all(w in set(toks) for w in needle):
            hits += 1
    return hits


def measure(keyword: str, zones: dict, zone_tokens: dict) -> dict:
    """Where and how often one keyword appears."""
    needle = tokens(keyword)
    per_zone = {z: count_phrase(zone_tokens[z], needle)
                for z in ("h1", "headings", "body", "alt", "meta")}
    headings_hit = [t for _, t in zones["headings"] if count_phrase(tokens(t), needle)]
    total = per_zone["h1"] + per_zone["headings"] + per_zone["body"]
    return {
        "keyword": keyword,
        "words": len(needle),
        **per_zone,
        "total": total,
        # written as a person writes it ("… interpreting in healthcare"), within one sentence, line
        # or table cell (RE56-02): covered for a section or secondary keyword; the primary keyword's
        # and the mandatory ones' own checks count the exact phrase
        "close": close_count([zones["h1"], *(t for _, t in zones["headings"]), *zones.get("body_units", [])],
                             needle)[1],
        "scattered_body": count_scattered(zones["body"], needle),
        "headings_hit": headings_hit[:6],
    }


def _pct(part: int, whole: int) -> float:
    return round(part / whole * 100, 1) if whole else 0.0


def analyze(markdown: str, kr: dict | None = None, brief: dict | None = None,
            thresholds: dict | None = None) -> dict:
    """The full coverage picture for one article. Everything here is counted, nothing judged."""
    t = {**DEFAULTS, **(thresholds or {})}
    kr, brief = kr or {}, brief or {}
    zones = split_zones(markdown)

    meta_text = " ".join(str(brief.get(k) or "") for k in ("meta_title", "meta_description"))
    zone_tokens = {
        "h1": tokens(zones["h1"]),
        "headings": tokens(zones["headings_text"]),
        "body": tokens(zones["body"]),
        "alt": tokens(zones["alt"]),
        "meta": tokens(meta_text),
    }
    # density is measured against the words a reader reads: H1 + headings + body.
    # Alt text and the meta tags are page metadata, counted but kept out of the denominator.
    article_words = len(zone_tokens["h1"]) + len(zone_tokens["headings"]) + len(zone_tokens["body"])

    primary = (kr.get("primary_keyword") or brief.get("title") or "").strip()
    analysis = kr.get("analysis") or {}
    secondaries = [s for s in (analysis.get("secondary_keywords") or []) if s]
    pool = [r.get("keyword") for r in (kr.get("related") or []) if r.get("keyword")]
    lsi = [x.get("keyword") for x in (kr.get("lsi") or []) if x.get("keyword")]
    # keep the section each brief keyword was assigned to: a fix that says WHERE to work a
    # keyword in is actionable, "add this keyword somewhere" is not
    section_kws, section_of = [], {}
    for sec in (brief.get("sections") or []):
        for k in (sec.get("keywords") or []):
            section_kws.append(k)
            section_of.setdefault(" ".join(tokens(k)), sec.get("h2"))

    def rows(keywords):
        seen, out = set(), []
        for kw in keywords:
            key = " ".join(tokens(kw))
            if not key or key in seen:
                continue
            seen.add(key)
            row = measure(kw, zones, zone_tokens)
            if key in section_of:
                row["section"] = section_of[key]
            out.append(row)
        return out

    p = measure(primary, zones, zone_tokens) if primary else None
    if p:
        # phrase density: multiword phrases occupy more of the text per occurrence
        p["density"] = round(p["total"] * p["words"] / article_words * 100, 2) if article_words else 0.0
        opening = " ".join(zones["body"].split()[: t["primary_in_first_words"]])
        p["in_opening"] = bool(count_phrase(tokens(opening), tokens(primary)))

    sec_rows, pool_rows, lsi_rows, sect_rows = rows(secondaries), rows(pool), rows(lsi), rows(section_kws)
    # SEO-mandatory keywords (beyond the primary): per article from the keyword research, plus any
    # project-wide list in seo.keywords.mandatory. Every other keyword is desired.
    mandatory = [k for k in dict.fromkeys(list(kr.get("mandatory_keywords") or []) + list(t.get("mandatory") or []))
                 if k and " ".join(tokens(k)) != " ".join(tokens(primary))]
    mand_rows = rows(mandatory)
    mand_keys = {" ".join(tokens(r["keyword"])) for r in mand_rows}
    for r in sec_rows + pool_rows + lsi_rows + sect_rows + mand_rows:
        r["priority"] = "mandatory" if " ".join(tokens(r["keyword"])) in mand_keys else "desired"

    def covered(rs):
        return sum(1 for r in rs if present(r))

    data = {
        "article_words": article_words,
        "primary": p,
        "thresholds": t,
        "zones": {"h1_words": len(zone_tokens["h1"]), "heading_count": len(zones["headings"]),
                  "body_words": len(zone_tokens["body"]), "alt_words": len(zone_tokens["alt"]),
                  "meta_words": len(zone_tokens["meta"])},
        "groups": {
            "mandatory": {"label": "Mandatory keywords (set by SEO)", "rows": mand_rows,
                          "covered": covered(mand_rows), "total": len(mand_rows),
                          "coverage": _pct(covered(mand_rows), len(mand_rows)), "min": 100},
            "secondary": {"label": "Secondary targets", "rows": sec_rows,
                          "covered": covered(sec_rows), "total": len(sec_rows),
                          "coverage": _pct(covered(sec_rows), len(sec_rows)),
                          "min": t["secondary_coverage_min"]},
            "sections": {"label": "Brief section keywords", "rows": sect_rows,
                         "covered": covered(sect_rows), "total": len(sect_rows),
                         "coverage": _pct(covered(sect_rows), len(sect_rows)),
                         "min": t["section_keyword_coverage_min"]},
            "lsi": {"label": "LSI vocabulary", "rows": lsi_rows,
                    "covered": covered(lsi_rows), "total": len(lsi_rows),
                    "coverage": _pct(covered(lsi_rows), len(lsi_rows)),
                    "min": t["lsi_coverage_min"]},
            "pool": {"label": "Ranked keyword pool", "rows": pool_rows,
                     "covered": covered(pool_rows), "total": len(pool_rows),
                     "coverage": _pct(covered(pool_rows), len(pool_rows)),
                     "min": None, "informational": True},
        },
    }
    data["checks"] = _checks(data)
    fails = [c for c in data["checks"] if c["status"] == "fail"]
    warns = [c for c in data["checks"] if c["status"] == "warn"]
    data["verdict"] = "fail" if fails else ("warn" if warns else "ok")
    data["failed"], data["warned"] = len(fails), len(warns)
    return data


def present(row: dict) -> bool:
    """A keyword row counts as covered: the exact phrase, or (except for a mandatory keyword,
    which SEO set word for word) the phrase as a person writes it."""
    return row["total"] > 0 or (row.get("priority") != "mandatory" and row.get("close", 0) > 0)


def _unique(keywords) -> list[str]:
    seen, out = set(), []
    for kw in keywords:
        key = " ".join(tokens(kw))
        if key and key not in seen:
            seen.add(key)
            out.append(kw)
    return out


def _checks(d: dict) -> list[dict]:
    """Each threshold as a pass/warn/fail line, with the number that decided it."""
    t, p = d["thresholds"], d["primary"]
    out: list[dict] = []

    def add(name, status, detail):
        out.append({"name": name, "status": status, "detail": detail})

    if not p:
        add("Primary keyword", "fail",
            "no primary keyword recorded for this article — was it generated with Ahrefs off?")
        return out

    kw = p["keyword"]
    add("Primary keyword in the H1",
        "ok" if p["h1"] else ("fail" if t["primary_in_h1"] else "warn"),
        f"'{kw}' appears {p['h1']}× in the H1")
    add("Primary keyword in H2/H3",
        "ok" if len(p["headings_hit"]) >= t["primary_in_headings_min"] else "warn",
        f"{len(p['headings_hit'])} heading(s) carry it (want ≥{t['primary_in_headings_min']})"
        + (": " + "; ".join(f"'{h}'" for h in p["headings_hit"]) if p["headings_hit"] else ""))
    add("Primary keyword in the opening",
        "ok" if p["in_opening"] else "warn",
        f"{'found' if p['in_opening'] else 'absent'} in the first "
        f"{t['primary_in_first_words']} words of body prose")
    mg = (d.get("groups") or {}).get("mandatory") or {}
    if mg.get("total"):
        missing = [r["keyword"] for r in mg["rows"] if r["total"] == 0]
        add("Mandatory keywords",
            "fail" if missing else "ok",
            f"{mg['covered']}/{mg['total']} present" + (": missing " + ", ".join(f"'{k}'" for k in missing)
                                                          if missing else ""))
    add("Primary keyword count",
        "ok" if p["total"] >= t["primary_min_total"] else "fail",
        f"{p['total']} occurrence(s) across H1, headings and body (want ≥{t['primary_min_total']})")

    # Density is enforced as a CEILING only. A three-word primary keyword that lands five
    # times in 3 400 words is 0.45% and is perfectly well optimised; warning on it just
    # trains people to ignore the report. Too FEW occurrences is already caught above.
    dens = p["density"]
    if dens > t["primary_density_max"]:
        add("Primary keyword density", "fail",
            f"{dens}% — above {t['primary_density_max']}%, which reads as stuffing")
    else:
        add("Primary keyword density", "ok",
            f"{dens}% (ceiling {t['primary_density_max']}%; "
            f"for reference our comfortable floor is {t['primary_density_min']}%)")

    for g in d["groups"].values():
        if not g["total"] or g.get("informational"):
            continue
        add(g["label"] + " coverage",
            "ok" if g["coverage"] >= g["min"] else "warn",
            f"{g['covered']}/{g['total']} present ({g['coverage']}%, want ≥{g['min']}%)")

    # a target that appears ONLY scattered is a specific, fixable finding: the ground is
    # covered but the phrase itself never lands
    # the same keyword can sit in several groups (a secondary target is usually also in the
    # pool), so findings are deduped by normalised phrase before being reported
    scattered_only = _unique(r["keyword"] for g in d["groups"].values() for r in g["rows"]
                             if not present(r) and r["scattered_body"] > 0)
    if scattered_only:
        add("Targets covered but never phrased", "warn",
            f"{len(scattered_only)} keyword(s) have all their words in a sentence but never as "
            f"the phrase: " + ", ".join(f"'{k}'" for k in scattered_only[:8])
            + (f" (+{len(scattered_only) - 8} more)" if len(scattered_only) > 8 else ""))

    over = _unique(r["keyword"] for g in d["groups"].values() for r in g["rows"]
                   if r["words"] > 1 and r["total"] >= 8)
    if over:
        add("Possible over-use", "warn",
            "used 8+ times: " + ", ".join(f"'{k}'" for k in over[:8]))
    return out


# ---- rendering ---------------------------------------------------------------------
_MARK = {"ok": "✅", "warn": "⚠️", "fail": "❌"}


def render(data: dict, topic: str = "") -> str:
    p, z = data["primary"], data["zones"]
    L = [f"# Keyword coverage — {topic}".rstrip(), "",
         f"**Verdict: {_MARK[data['verdict']]} {data['verdict'].upper()}** · "
         f"{data['failed']} failed, {data['warned']} to review · "
         f"{data['article_words']} words counted", "",
         "_Counted from the finished article, not the brief. Anchor text counts; the URLs "
         "behind links do not. Plurals fold together (\"apps\" = \"app\"); word families do "
         "not (\"invoicing\" is not \"invoice\"). The thresholds below are our house "
         "settings, editable in the Studio — not an external standard._", ""]

    L += ["## Checks", "", "| | Check | Measured |", "|---|---|---|"]
    for c in data["checks"]:
        L.append(f"| {_MARK[c['status']]} | {c['name']} | {c['detail']} |")
    L.append("")

    if p:
        L += ["## Primary keyword", "", f"### `{p['keyword']}`", "",
              "| Zone | Occurrences |", "|---|---|",
              f"| H1 | {p['h1']} |",
              f"| H2/H3/H4 | {p['headings']} |",
              f"| Body prose | {p['body']} |",
              f"| Image alt text and captions | {p['alt']} |",
              f"| Meta title and description | {p['meta']} |",
              f"| **Total (H1 + headings + body)** | **{p['total']}** |",
              f"| Density | {p['density']}% |",
              f"| Words all present but not as the phrase | {p['scattered_body']} sentence(s) |", ""]
        if p["headings_hit"]:
            L += ["Headings carrying it:", ""] + [f"- {h}" for h in p["headings_hit"]] + [""]

    L += [f"## Where the words are", "",
          f"- H1: {z['h1_words']} words · headings: {z['heading_count']} · "
          f"body: {z['body_words']} words",
          f"- image alt text and captions: {z['alt_words']} words "
          f"(counted, but kept out of the density denominator)",
          f"- meta title and description: {z['meta_words']} words", ""]

    for g in data["groups"].values():
        if not g["total"]:
            continue
        L += [f"## {g['label']} — {g['covered']}/{g['total']} present ({g['coverage']}%)", "",
              "| Keyword | H1 | Headings | Body | Alt | Meta | Total | Scattered |",
              "|---|---|---|---|---|---|---|---|"]
        for r in sorted(g["rows"], key=lambda r: (-r["total"], r["keyword"])):
            L.append(f"| {r['keyword']} | {r['h1'] or ''} | {r['headings'] or ''} | "
                     f"{r['body'] or ''} | {r['alt'] or ''} | {r['meta'] or ''} | "
                     f"**{r['total']}** | {r['scattered_body'] or ''} |")
        missing = [r["keyword"] for r in g["rows"] if not present(r)]
        if missing:
            L += ["", f"**Absent ({len(missing)}):** " + ", ".join(missing), ""]
        L.append("")
    return "\n".join(L)


# ---- for prompts -------------------------------------------------------------------
# A target only becomes a PROPOSED FIX if requesting it could not be wrong on its face. The
# keyword pool is full of raw Ahrefs query strings, and asking the writer to insert one verbatim
# produces garbage — sometimes carrying a year our own freshness rule bans.
#
# This filter stays deliberately CONSERVATIVE. A word-count cap cannot tell "voice cloning api
# for video production" (perfectly writable) from "elevenlabs voice cloning api python" (nobody
# writes that), and guessing wrong in either direction is worse than not guessing: too strict
# drops real coverage, too loose demands mangled sentences. So code removes only what is
# unambiguously wrong to ask for, and the Editor prompt carries the judgement call about
# naturalness — it can read the sentence, a regex cannot.
#
# Excluded targets stay in the full 06_keywords.md: they are real search demand and the SEO team
# should see them. They are simply never handed over as an instruction.
MAX_FIXABLE_WORDS = 8

# Tails that mark a SEARCH QUERY rather than a phrase anyone writes. Narrow on purpose: the
# first draft of this list also held "app", "tools", "software" and "options", and validating it
# against 114 real targets showed it throwing away "medical translation app", "ai audio tools"
# and "hipaa compliant translation app" — those words are the HEAD NOUN of perfectly writable
# phrases, not junk. The list below blocks 12 of 108 targets, every one a genuine query string,
# and spares all the legitimate ones.
#
# Why this exists at all: with coverage feeding the Editor, a raw query got demanded and inserted,
# and the Editor then scored the result as stuffing ("'AI audio tools 2026 free paid options'
# reads slightly stuffed") — the reviser adding it and the Editor punishing it, two agents at
# cross purposes.
_QUERY_TAILS = {"python", "pricing", "price", "cost", "free", "paid", "download", "reddit",
                "github", "login", "vs", "alternative", "alternatives", "review", "reviews",
                "tutorial", "docs", "documentation"}


def is_fixable(keyword: str, current_year: int | None = None) -> bool:
    kw = (keyword or "").strip()
    if not kw or kw.endswith("?"):
        return False                        # questions ship in the FAQ
    if len(kw.split()) > MAX_FIXABLE_WORDS:
        return False                        # past this it is a query string, not a phrase
    year = current_year or date.today().year
    for found in re.findall(r"\b(20[1-3][0-9])\b", kw):
        if int(found) != year:
            return False                    # a stale year; our own rule bans writing one
    if kw[:1].isupper() and sum(1 for w in kw.split() if w[:1].isupper()) >= 3:
        return False                        # Title Case with several caps — a heading, not a keyword
    last = kw.split()[-1].lower().strip(",.")
    if last in _QUERY_TAILS or re.fullmatch(r"20[1-3][0-9]", last):
        return False                        # "… api python", "… pricing 2026", "… free"
    return True


def format_coverage_report(d: dict, max_items: int = 10) -> str:
    """The measured coverage, phrased so the Editor can turn it into concrete fixes.

    Deliberately opinionated about ORDER: keywords that are already covered in prose but never
    written as the phrase come first, because rewording one sentence fixes them without adding
    a word. Keywords with a known target section come next, because the fix can name where the
    phrase belongs. Everything else is a genuine gap in coverage and may not be worth forcing.
    """
    p = d.get("primary")
    if not p:
        return "- No primary keyword recorded (the article was generated with Ahrefs off)."
    t = d["thresholds"]
    tick = lambda ok: "OK" if ok else "MISSING"  # noqa: E731
    L = [f"- Primary keyword '{p['keyword']}': {p['total']} occurrence(s) "
         f"(H1 {p['h1']}, headings {p['headings']}, body {p['body']}) at {p['density']}% density "
         f"— floor is {t['primary_min_total']} occurrences, stuffing ceiling {t['primary_density_max']}%",
         f"  H1: {tick(p['h1'])} · headings carrying it: {len(p['headings_hit'])} "
         f"(want >={t['primary_in_headings_min']}) · in the first {t['primary_in_first_words']} "
         f"words: {tick(p['in_opening'])}"]

    def _rank(row):
        # scattered first (cheapest fix), then ones with a target section, then the rest
        return (0 if row.get("scattered_body") else 1, 0 if row.get("section") else 1,
                -row.get("words", 0))

    listed: set[str] = set()
    for key in ("mandatory", "secondary", "sections", "lsi"):
        g = d["groups"].get(key) or {}
        if not g.get("total"):
            continue
        missing = []
        for r in sorted((r for r in g["rows"] if not present(r)), key=_rank):
            norm = " ".join(tokens(r["keyword"]))
            # a secondary target is usually also a section keyword: naming it twice just eats
            # prompt budget and makes the gap look bigger than it is
            if norm in listed:
                continue
            # SEO decided a mandatory keyword must appear: the raw-query filter does not apply
            if key != "mandatory" and not is_fixable(r["keyword"]):
                continue
            listed.add(norm)
            missing.append(r)
        L.append(f"- {g['label']}: {g['covered']}/{g['total']} present ({g['coverage']}%, "
                 f"want >={g['min']}%)")
        for r in missing[:max_items]:
            where = f' -> intended section: "{r["section"]}"' if r.get("section") else ""
            how = (" (already covered in prose but never written as this phrase — rewording one "
                   "sentence fixes it)" if r.get("scattered_body") else "")
            tag = "MANDATORY MISSING" if key == "mandatory" else "MISSING"
            L.append(f'    {tag}: "{r["keyword"]}"{where}{how}')
        absent = sum(1 for r in g["rows"] if not present(r))
        if len(missing) > max_items:
            L.append(f"    ... and {len(missing) - max_items} more that could be worked in")
        skipped = absent - len(missing)
        if skipped > 0:
            L.append(f"    ({skipped} other absent target(s) are raw search queries, headings, "
                     f"questions or carry a stale year — real demand, but not text anyone would "
                     f"write, so do NOT ask for them)")
    return "\n".join(L)


# ---- entry points -----------------------------------------------------------------
def thresholds_from(seo: dict | None) -> dict:
    return {**DEFAULTS, **((seo or {}).get("keywords") or {})}


def write_report(article_dir, data: dict, topic: str = "") -> None:
    """Persist both faces of the report: `06_keywords.md` to read, `_keywords.json` to query.
    The JSON records the article version it measured (`revision`), so the scorecard can tell a
    report older than the text and measure again."""
    from .store import Article, atomic_write_text, write_json

    d = Path(article_dir)
    data["revision"] = Article(d.parent, d.name).revision()
    atomic_write_text(d / "06_keywords.md", render(data, topic or d.name))
    write_json(d / "_keywords.json", data)


def build(article_dir, seo: dict | None = None, write: bool = True) -> dict:
    """Compute (and by default write) the report for one article directory.

    Reads `_run.json` for the targets, so nothing extra has to be persisted during a run.
    """
    from .store import Article, read_json_lenient

    d = Path(article_dir)
    src = Article(d.parent, d.name).source
    if src is None:
        raise FileNotFoundError(f"no article.md or 04_draft.md in {d}")
    run = read_json_lenient(d / "_run.json", {}) or {}
    kr = run.get("keyword_research") or {}
    brief = run.get("brief") or {}
    data = analyze(src.read_text(encoding="utf-8"), kr, brief, thresholds_from(seo))
    data["slug"] = d.name
    data["source"] = src.name
    if write:
        write_report(d, data, run.get("topic") or kr.get("topic") or d.name)
    return data


def summary(article_dir) -> dict | None:
    """The cached headline numbers, for the dashboard. None if never computed."""
    from .store import read_json_lenient

    d = read_json_lenient(Path(article_dir) / "_keywords.json")
    if not isinstance(d, dict):
        return None
    pk = d.get("primary") or {}
    return {"verdict": d.get("verdict"), "failed": d.get("failed", 0),
            "warned": d.get("warned", 0), "primary": pk.get("keyword"),
            "total": pk.get("total"), "density": pk.get("density")}
