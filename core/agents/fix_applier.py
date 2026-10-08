"""Apply the Editor's required_fixes as TARGETED EDITS, one fix at a time.

Why this exists. Both paths that acted on a QA verdict — the Copywriter's revision mode and the
QA Fixer's polish pass — handed the model the whole article and took a whole article back. Every
recurring defect we chased traced to that one shape:

  - images deleted (a run shipped articles with zero visuals from drafts that had ten)
  - internal links falling from eleven to five
  - target length collapsing by a third
  - keyword phrases jammed in until the Editor scored the result as stuffing
  - a cited source lost while two sentences were merged

None of these were asked for. They are what a full rewrite does to the parts nobody mentioned.

So a fix is now a **string swap**: the model returns `{find, replace}` — a short distinctive
anchor from the article and what it should become — and the swap is applied by EXACT match. Two
consequences make this safe in a way a rewrite can never be:

  1. **An anchor that does not match verbatim is skipped, never guessed at.** No fuzzy matching,
     no "closest paragraph". A miss costs one unapplied fix and is reported; a fuzzy hit would
     silently corrupt a sentence nobody was looking at.
  2. **Everything outside the matched span is byte-identical**, so images, links, citations,
     tables and length cannot drift as a side effect. The verifier only has to check the swap
     itself.

Whitespace is normalised for MATCHING only (models reflow line breaks when quoting), never in
the output.
"""
from __future__ import annotations

import re

from .base import BaseAgent

_WS = re.compile(r"\s+")
_IMG = re.compile(r"!\[([^\]]*)\]\((assets/[^)\s]+)\)")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")

# A possessive apostrophe is not an opening quote: "the table's 'Best fit' column" used to
# yield the label `s ` (from `table's 'Best`), so the table locator matched nothing and a fix
# that only wanted one column reordered was reported as impossible. Require the opening quote
# not to follow a letter. A quote is paired with its own closing mark (one not followed by a
# letter), whatever its length, with apostrophes inside words kept: an example sentence over 80
# characters was skipped, its closing mark opened the next "quote", and the guard demanded
# ") to use the phrase, e.g." and "Work the phrase" (2026-10-06). Length rules are the callers'.
_QUOTED = re.compile(r"(?<![A-Za-z])['\"“”‘’]((?:[^'\"“”‘’]|(?<=[A-Za-z])['’](?=[A-Za-z])){2,600}?)['\"“”‘’](?![A-Za-z])")
# The Editor's own templates for "this exact wording must appear in the text". Taken from real
# verdicts: "use the exact phrase 'text to voice'", "Add the tool 'Murf AI' as the exact phrase",
# "Work 'medical translation company' into the ... section opener".
_WANTS_PHRASE = re.compile(r"(?i)\bexact phrase\b|\bwork\b[^.]{0,80}\binto\b"
                           r"|\bto use\b|\bmust (?:appear|contain|include)\b")
_REMOVAL = re.compile(r"(?i)\b(remove|delete|drop|strip|unlink|cut|take out)\b")
# a quoted phrase that names what the text says NOW, not what it must say: "(variant of
# already-present 'Google Translate')", "currently 'language translation app' appears",
# "instead of 'translator app'". The guard demanded these too, refused good edits for lacking
# them, and keyword fixes failed on prod for that reason (2026-10-01)
_A_REFERENCE = re.compile(r"(?i)(?:\bcurrently|\balready(?:-present|\s+present)?|\bvariant\s+of|\binstead\s+of"
                          r"|\brather\s+than|\breplac(?:e|es|ing)|\bexisting|\bpresent)\s*(?:the\s+)?"
                          r"(?:phrase\s+|wording\s+|word\s+)?$")


# A fix quotes two different kinds of thing: the wording to insert, and the section to insert it
# into. Requiring the SECTION name to appear in a replacement sentence would reject every correct
# edit, so a quote introduced as a location is dropped.
_A_LOCATION = re.compile(r"(?i)(?:\bin|\binto|\bwithin|\bunder|\bfrom|\bto|\bof"
                         r"|\bsection|\bheading|\bopener|\btitled)"
                         r"(?:\s+the)?\s*$")


_EXACT = re.compile(r"(?i)\bexact (?:phrase|wording)\b")


def asks_exact(fix: str | None) -> bool:
    """Whether a fix asks for its phrase word for word ("the exact phrase"): then no natural
    variant of it counts, here or in the engine (qa_changes._word_for_word, R597-04)."""
    return bool(_EXACT.search(fix or ""))

def demanded_phrases(fix: str) -> list[str]:
    """Quoted wording the fix asks to be PRESENT. Empty when the fix asks for a removal."""
    text = fix or ""
    if not _WANTS_PHRASE.search(text) or _REMOVAL.search(text):
        return []
    out = []
    for m in _QUOTED.finditer(text):
        if _A_LOCATION.search(text[max(0, m.start() - 14):m.start()]):
            continue                      # "... in 'Key evaluation criteria'" — a place, not wording
        if _A_REFERENCE.search(text[max(0, m.start() - 30):m.start()]):
            continue                      # "... currently 'X'" — what is there now, not wording
        q = m.group(1).strip()
        if not q:
            continue
        # An EXAMPLE rewrite is not demanded wording: editors quote suggestions ("e.g. reword
        # to: 'A simultaneous interpretation booth provides acoustic isolation...'") and an
        # equally valid different rewrite must not fail the check (it stuck a repaired Critical
        # in the field test). A demanded phrase is keyword-shaped: short, no trailing ellipsis.
        if q.endswith(("...", "…")) or len(q.split()) > 6:
            continue
        out.append(q)
    return out


def _host_tokens(url: str) -> set[str]:
    host = re.sub(r"^https?://", "", url or "").split("/")[0].lower()
    host = host[4:] if host.startswith("www.") else host
    parts = host.split(".")
    return {host} | ({parts[-2]} if len(parts) >= 2 else set())


def names_source(fix: str, url: str) -> bool:
    """Does the fix name the site whose link an edit is dropping?

    A fix can legitimately ASK for a link to go — "the external link to lilt.com/blog is not part
    of the research corpus". The dropped-link guard refused exactly those edits, so the Editor
    demanded a removal, the fixer performed it, the guard threw it away, and after two attempts
    the finding was recorded as stuck. A guard against ACCIDENTAL loss must not block a requested
    one, and the fix naming the site is the signal that it was requested.
    """
    low = (fix or "").lower()
    return any(tok in low for tok in _host_tokens(url))


# A single fix should not restructure the article. Past this the model has stopped editing and
# started rewriting, which is the thing this module exists to prevent.
MAX_GROWTH_RATIO = 3.0
MAX_ANCHOR_WORDS = 60
# a section rewrite is allowed; a redraft of the article is not
MAX_SECTION_WORDS = 700

_HEAD_LINE = re.compile(r"^#{2,4}\s+.+$", re.M)


_TABLE_BLOCK = re.compile(r"(?:^\|.*\|[ \t]*\n)+", re.M)


def _table_span(markdown: str, fix: str) -> tuple[int, int] | None:
    """The (start, end) of the markdown TABLE a fix is about.

    A table is neither a sentence nor a section: too big for one anchored swap, and handing
    over the whole section around it invites the model to reflow prose nobody asked about.
    Rebuilding a column is genuinely easy — it only ever failed because nothing told the
    fixer WHERE the table was (measured: "the model did not return a usable find/replace" on
    a fix that just wanted one column re-ordered).
    """
    if not re.search(r"(?i)\btable\b|\bcolumn\b|\brow\b", fix or ""):
        return None
    # quoted column/row labels the fix names, e.g. "Best fit"
    labels = [m.group(1).strip() for m in _QUOTED.finditer(fix or "")]
    blocks = [(m.start(), m.end(), m.group(0)) for m in _TABLE_BLOCK.finditer(markdown or "")]
    if not blocks:
        return None
    for start, end, block in blocks:
        if any(lab and lab.lower() in block.lower() for lab in labels):
            return start, end
    return None if len(blocks) != 1 else (blocks[0][0], blocks[0][1])


def _section_span(markdown: str, anchor_text: str, fix: str) -> tuple[int, int] | None:
    """The (start, end) of the H2/H3 block a fix is about — by quoted text, else by heading."""
    heads = [(m.start(), m.group(0)) for m in _HEAD_LINE.finditer(markdown or "")]
    if not heads:
        return None
    pos = -1
    if anchor_text:
        pos = markdown.find(anchor_text)
    if pos < 0:                                   # fall back to a heading the fix names
        low = (fix or "").lower()
        for start, line in heads:
            title = line.lstrip("#").strip()
            if len(title) >= 8 and title.lower() in low:
                pos = start
                break
    if pos < 0:
        return None
    start = max([h for h, _ in heads if h <= pos], default=None)
    if start is None:
        return None
    later = [h for h, _ in heads if h > start]
    return start, (later[0] if later else len(markdown))


def _norm(t: str) -> str:
    return _WS.sub(" ", (t or "").strip())


# "Write the missing 'Getting started: call a TTS API in under 10 lines' section" is the one
# shape of finding no span edit can reach: there is no anchor, because the text does not exist.
# It reached a human as a paragraph of perfectly followable instructions, purely because the
# applier only knew how to REPLACE. Detect it narrowly — a create verb, an explicit statement
# that the thing is absent, a quoted title, and no heading by that name in the article — so a
# fix about an EXISTING section never takes this path.
_MISSING_SECTION = re.compile(
    r"\b(?:write|add|create)\b[^.]{0,120}?"
    r"(?:\bmissing\b|\bnot (?:present|there)\b|\babsent\b)[^.]{0,120}?"
    r"['\"\u201c\u201d\u2018\u2019]([^'\"\u201c\u201d\u2018\u2019]{4,90})['\"\u201c\u201d\u2018\u2019]",
    re.I | re.S)
_MISSING_SECTION_ALT = re.compile(
    r"\b(?:write|add|create)\b[^.]{0,120}?"
    r"['\"\u201c\u201d\u2018\u2019]([^'\"\u201c\u201d\u2018\u2019]{4,90})['\"\u201c\u201d\u2018\u2019]\s+section\b",
    re.I | re.S)


# an asset name slips past the word-boundary test ("chart_2" has no boundary before the "_"),
# and it is never a section title
_ASSET_NAME = re.compile(
    r"^(?:chart|screenshot|image|figure|diagram|cover|graphic)[_\-\s]?\d*$"
    r"|\.(?:png|jpe?g|svg|webp)$", re.I)

_VISUAL_WORD = re.compile(
    r"\b(?:charts?|screenshots?|images?|diagrams?|figures?|covers?|graphics?)\b", re.I)


def _missing_section_request(fix: str, markdown: str) -> str | None:
    """The title of a section the fix asks to CREATE, or None."""
    # a visual is not a section: "add the missing 'pricing' chart" goes to the producer.
    # Whole words only — a substring test read "cover" inside "covering refunds" and threw
    # away a legitimate section request.
    if _VISUAL_WORD.search(fix or ""):
        return None
    for rx in (_MISSING_SECTION, _MISSING_SECTION_ALT):
        m = rx.search(fix or "")
        if not m:
            continue
        title = _norm(m.group(1))
        if not title or len(title.split()) > 14 or _ASSET_NAME.search(title):
            continue
        have = {_norm(h.group(1)).lower() for h in _HEADING_LINE.finditer(markdown or "")}
        if title.lower() in have:
            continue                       # it exists — this is a repair, not a creation
        return title
    return None


def find_span(text: str, anchor: str) -> tuple[int, int] | None:
    """Locate `anchor` in `text`, tolerating reflowed whitespace. Unique matches only.

    Returns (start, end) into the ORIGINAL text, or None when the anchor is absent or appears
    more than once — an ambiguous anchor is as dangerous as a missing one, because the model
    meant a specific occurrence and we cannot know which.
    """
    if not anchor or not anchor.strip():
        return None
    if text.count(anchor) == 1:
        i = text.index(anchor)
        return i, i + len(anchor)

    # whitespace-insensitive pass: walk the text building a normalised index map
    norm_anchor = _norm(anchor)
    if not norm_anchor:
        return None
    idx, norm_chars = [], []
    prev_ws = False
    for i, ch in enumerate(text):
        if ch.isspace():
            if not prev_ws and norm_chars:
                norm_chars.append(" ")
                idx.append(i)
            prev_ws = True
            continue
        prev_ws = False
        norm_chars.append(ch)
        idx.append(i)
    hay = "".join(norm_chars)
    first = hay.find(norm_anchor)
    if first < 0 or hay.find(norm_anchor, first + 1) >= 0:
        return None                      # absent, or ambiguous
    return idx[first], idx[first + len(norm_anchor) - 1] + 1


_PROPER_SEQ = re.compile(r"\b([A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+)*)\b")
_CODE_BLOCK = re.compile(r"```.*?```|~~~.*?~~~", re.S)
_CODE_SPAN = re.compile(r"`[^`\n]+`")


def _prose_only(text: str) -> str:
    """The text with code removed. Identifiers are not claims, and a fix that asks for a code
    example cannot be judged by whether it introduced the word `Authorization`."""
    return _CODE_SPAN.sub(" ", _CODE_BLOCK.sub(" ", text or ""))


def _new_proper_nouns(before: str, after: str, exempt_text: str) -> list[str]:
    """Proper nouns the replacement INTRODUCES that nothing sanctioned.

    An edit that "softens" one claim can assert another ("…as several publishers including
    TIME have found") — link-free, numeral-free, invisible to every other guard. A proper noun
    is sanctioned when it appears in the original span, the fix text, the article, the corpus,
    or the product facts (all concatenated into `exempt_text` by the caller). The fix-text
    exemption is the `names_source` rule again: the editor legitimately demands edits about
    entities the corpus lacks (the Udio roundup entry), and a guard against accidental harm
    must not block a requested change.
    """
    hay = before + "\n" + exempt_text
    # Judge PROSE. A section written to a fix that demanded a Python example was rejected for
    # "smuggling" HTTP, JSON, Bearer, Authorization and Hello — every one of them from inside
    # the code block the fix asked for, and no claim among them.
    after = _prose_only(after)

    def _tok_sanctioned(tok: str) -> bool:
        # exact surface form anywhere in the sanctioned texts
        if re.search(r"(?<![A-Za-z0-9])" + re.escape(tok) + r"(?![A-Za-z0-9])", hay):
            return True
        # a capitalised form of an ordinary word ("Modern" at a sentence start) is prose, not
        # an entity — but ONLY for titlecase: an all-caps token must match exactly, because
        # "TIME" the magazine and "time" the noun are different things (the gap-entity lesson)
        if not tok.isupper() and re.search(
                r"(?<![A-Za-z0-9])" + re.escape(tok.lower()) + r"(?![A-Za-z0-9])",
                hay + "\n" + after):
            return True
        # A "capitalised only where English capitalises anyway" exemption was tried here and
        # removed: it let a brand through whenever every mention happened to open a sentence
        # ("**Udio.** A rival shipped. Udio prices per second."), which is exactly the claim
        # this guard exists to stop. Sentence-opening common words are instead handled where
        # they belong — the corpus and product facts sanction them, and a rejected section
        # gets one informed retry that says which words were the problem.
        return False

    out = []
    for m in _PROPER_SEQ.finditer(after):
        seq = m.group(1).strip()
        if len(seq) < 3 or seq in before:
            continue
        toks = seq.split()
        # A sentence-opening word in an inflected English form ("Providing", "Following",
        # "Supported", "Usually") is prose: the guard refused a sourcing fix twice over
        # "Providing" (webinar replay, 2026-09-28). The stem must be 4+ letters, so short
        # brand-like words ("Kling", "Udio") still need the corpus to sanction them.
        opener = (_opens_sentence(after, m.start(1)) and not toks[0].isupper()
                  and bool(_INFLECTED.match(toks[0].lower())))
        # a sequence is sanctioned when EVERY token is individually known — a new combination
        # of known words ("Modern SI") is prose; a new NAME has at least one unknown token.
        # The field test's smuggling guard rejected the repair of a garbled Critical over
        # exactly this false positive, sticking the article.
        if all((k == 0 and opener) or _tok_sanctioned(t) for k, t in enumerate(toks)):
            continue
        if seq not in out:
            out.append(seq)
    return out


_INFLECTED = re.compile(r"^[a-z]{4,}(?:ing|ed|ly)$")


def _opens_sentence(text: str, i: int) -> bool:
    """Whether position `i` starts a sentence or a link's text: the text's start, after end
    punctuation or a line break, or right after the "[" of a markdown link (a page title used as
    the anchor), allowing quotes and emphasis marks in between."""
    j = i
    while j > 0 and text[j - 1] in " \t\"'“‘(*_":
        j -= 1
    return j == 0 or text[j - 1] in ".!?:;\n["


_HEADING_LINE = re.compile(r"^#{1,4}\s+(.+?)\s*$", re.M)


def _edit_problems(before: str, after: str, fix: str,
                   allowed_urls: set[str] | None = None,
                   exempt_text: str = "",
                   headings: set[str] = frozenset(),
                   existing_assets: set[str] | None = None,
                   section_rewrite: bool = False, keep_images: bool = False) -> list[str]:
    """Reject an edit that damages something the fix did not mention.

    Checked PER EDIT rather than over the combined result, which is the whole reason for going
    one fix at a time: a run was observed discarding two good edits because a third tried to
    cite the EU AI Act, a URL absent from the research corpus. Per-edit, that one is skipped and
    the other two land.
    """
    problems = []
    b_imgs, a_imgs = set(_IMG.findall(before)), set(_IMG.findall(after))
    lost = b_imgs - a_imgs
    if keep_images:
        # a picture is the file the page shows, counted: a new alt text is not a lost picture,
        # and a second use of a file taken out is (reviews R829-01, R0B9-01)
        from collections import Counter

        from ..store import shown_images

        gone = Counter(shown_images(before)) - Counter(shown_images(after))
        if gone:
            problems.append(f"dropped image(s): {sorted(gone)}")
        lost = set()
    if lost:
        # a fix may legitimately ask for a visual to GO ("remove Chart 2", "drop the
        # screenshot") — the same names-it rule as link removals: a guard against accidental
        # loss must not block a requested one. Match by asset filename or its chart_N stem.
        low_fix = (fix or "").lower()
        unasked = []
        for _, path in lost:
            stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
            if stem in low_fix or path.lower() in low_fix or stem.replace("_", " ") in low_fix:
                continue
            unasked.append(path)
        if unasked:
            problems.append(f"dropped image(s): {sorted(unasked)}")
    b_urls = {u for _, u in _LINK.findall(before)}
    a_urls = {u for _, u in _LINK.findall(after)}
    unasked = {u for u in b_urls - a_urls if not names_source(fix, u)}
    if unasked:
        problems.append(f"dropped link(s): {sorted(unasked)}")
    # A URL the research never supplied is an invented source even when the page is real. The
    # Editor sometimes asks for a citation on a claim nothing in the corpus supports; the right
    # answer there is to soften or drop the claim, not to attach a link, so the edit is refused.
    if allowed_urls is not None:
        # EXACT urls, not domains. Comparing domains was too lenient and disagreed with the
        # outer verifier, which compares exact urls: an article already citing one EU AI Act
        # page let an edit attach a DIFFERENT page on eur-lex.europa.eu, the per-edit check
        # waved it through, and the coarse net then discarded the whole pass. Citing another
        # page of the same site is still a source the research never supplied.
        introduced = a_urls - b_urls - set(allowed_urls)
        if introduced:
            problems.append(f"introduces external source(s) not in the research corpus: "
                            f"{sorted(introduced)}")
    # An edit that satisfies every safety check and still does not do the job was the single
    # biggest source of wasted cycles: three of three open findings on one article were "use the
    # exact phrase X", each recorded as APPLIED, and none of the three phrases was anywhere in the
    # finished text. The applier only ever checked that an edit broke nothing.
    # A quoted string that IS one of the article's headings is a location, not wording — the
    # lookbehind heuristic misses some phrasings ("to the 'Section name'"), and demanding a
    # heading verbatim in a body sentence stuck two findings in the field test.
    # a phrase may land as a person writes it ("… interpreting in healthcare", which the keyword
    # instruction asks for, keyword_report.count_close), unless the fix asks for the exact phrase
    from ..keyword_report import phrase_count

    exact = asks_exact(fix)
    missing = [ph for ph in demanded_phrases(fix)
               if ph.lower() not in after.lower() and (exact or not phrase_count(after, ph))
               and ph.lower() not in headings]
    if missing:
        problems.append(f"the replacement does not contain the wording the fix asked for: "
                        f"{missing} — reported as applied, this is what made the Editor raise "
                        f"the same finding again next cycle")

    # an edit may point an image only at a file that EXISTS: a fix asked to "replace the
    # marker" and the model swapped in assets/diagram_x.png that nothing ever generated —
    # every guard passed and the article gained a broken image (found via the rejected-pass
    # postmortem dump)
    if existing_assets is not None:
        new_paths = {p for _, p in a_imgs} - {p for _, p in b_imgs}
        ghost = [p for p in new_paths if p not in existing_assets]
        if ghost:
            problems.append(f"references image file(s) that do not exist: {sorted(ghost)}")

    smuggled = _new_proper_nouns(before, after, (fix or "") + "\n" + exempt_text)
    if smuggled:
        problems.append(f"introduces proper noun(s) sanctioned by neither the fix, the article, "
                        f"the corpus nor the product facts: {smuggled} — a swap must not "
                        f"smuggle in new named claims")

    nb, na = len(before.split()), len(after.split())
    if nb and not section_rewrite and na > nb * MAX_GROWTH_RATIO:
        problems.append(f"replacement is {na} words against an anchor of {nb} — that is a "
                        f"rewrite, not a fix")
    return problems


class FixApplier(BaseAgent):
    """Turns one required_fix into one verified string swap."""

    name = "fix_applier"
    model_role = "editor"       # judging which sentence to touch needs the stronger model

    def _rules_block(self) -> str:
        """The project's editorial rules, for every call that writes text: a swap, a section
        rewritten, a section added (and their retries). "" when the project has none."""
        rules = (getattr(self.project, "editorial_rules", "") or "").strip()
        return f"\n\nEDITORIAL RULES (the text you write keeps every one):\n{rules}\n" if rules else ""

    def apply_all(self, markdown: str, fixes: list[str], context: str = "",
                  allowed_urls: set[str] | None = None,
                  exempt_entity_text: str = "",
                  existing_assets: set[str] | None = None, keep_images: bool = False) -> dict:
        """Apply each fix in turn. Returns a report; `markdown` is never partially mutated.

        `keep_images`: no edit may take out a picture, even one the fix names (the QA model's
        changes: a picture is removed only by the person's choice of `remove_picture`).

        `exempt_entity_text` sanctions proper nouns for the smuggling guard (corpus + product
        facts, supplied by the caller); the current article text is always added to it, so an
        entity the article already discusses is never "new".
        """
        current = markdown
        applied, skipped, satisfied = [], [], []
        headings = {m.group(1).strip().lower() for m in _HEADING_LINE.finditer(markdown)}
        for fix in fixes:
            anchor_hint = next((m.group(1) for m in _QUOTED.finditer(fix)
                                if len(m.group(1)) >= 12 and m.group(1) in current), None)

            # a section that does not exist yet has no span to anchor to — write it instead.
            # `before` is empty, so every guard below still runs and only the growth rule,
            # which is meaningless for an insertion, drops out.
            if (want := _missing_section_request(fix, current)):
                made = self.write_section(current, fix, context, want)
                if made:
                    def _section_problems(sec):
                        return _edit_problems(
                            "", sec, fix, allowed_urls,
                            exempt_text=current + "\n" + exempt_entity_text, headings=headings,
                            existing_assets=existing_assets, keep_images=keep_images)

                    problems = _section_problems(made["section"])
                    if problems:
                        # same informed-retry rule as the anchored path: a second identical
                        # prompt is a coin flip, a second prompt that says what was wrong is
                        # a retry
                        again = self.write_section(current, fix, context, want,
                                                   rejected="; ".join(problems))
                        if again and not (p2 := _section_problems(again["section"])):
                            made, problems = again, []
                        elif again:
                            problems = p2
                    if problems:
                        skipped.append({"fix": fix, "why": "; ".join(problems)})
                    else:
                        at = made["at"]
                        current = current[:at] + made["section"] + current[at:]
                        headings.add(want.lower())
                        applied.append({"fix": fix, "find": "", "replace": made["section"],
                                        "new_section": True})
                    continue
                # could not write or place it — fall through and let the ordinary path try

            edit = self._locate(current, fix, context)
            if edit is not None and edit.get("already_satisfied"):
                satisfied.append({"fix": fix,
                                  "why": edit.get("note") or "the fix is already met in the text"})
                continue
            if edit is not None and edit.get("declined"):
                # a phrase no sentence takes without a reader noticing it: left to the person, with
                # the reason, not forced in by a section rewrite (2026-10-06)
                skipped.append({"fix": fix, "why": edit["note"], "declined": True})
                continue
            if not edit:
                # the remedy exists, it just does not fit one span: try the section
                edit = self.rewrite_section(current, fix, context, anchor_hint)
            if not edit:
                skipped.append({"fix": fix, "why": "the model did not return a usable find/replace"})
                continue
            span = find_span(current, edit["find"])
            if span is None:
                edit = self.rewrite_section(current, fix, context, anchor_hint)
                span = find_span(current, edit["find"]) if edit else None
            if span is None:
                skipped.append({"fix": fix,
                                "why": "anchor not found verbatim, or matched more than once — "
                                       "skipped rather than guessed",
                                "anchor": (edit or {}).get("find", "")[:120]})
                continue
            start, end = span
            problems = _edit_problems(current[start:end], edit["replace"], fix, allowed_urls,
                                      exempt_text=current + "\n" + exempt_entity_text,
                                      headings=headings, existing_assets=existing_assets, keep_images=keep_images,
                                      section_rewrite=bool(edit.get("section_rewrite")))
            if problems and not edit.get("section_rewrite"):
                # a guard tripped on the narrow edit — the section path may satisfy it
                wider = self.rewrite_section(current, fix, context, anchor_hint)
                if wider and (sp := find_span(current, wider["find"])):
                    w_problems = _edit_problems(
                        current[sp[0]:sp[1]], wider["replace"], fix, allowed_urls,
                        exempt_text=current + "\n" + exempt_entity_text, headings=headings,
                        existing_assets=existing_assets, section_rewrite=True, keep_images=keep_images)
                    if not w_problems:
                        edit, start, end, problems = wider, sp[0], sp[1], []
            if problems:
                # informed retry: the model gets the rejection reason, not another blind roll
                retry = self._locate(current, fix, context, rejected="; ".join(problems))
                r_span = find_span(current, retry["find"]) if retry else None
                if r_span:
                    r_problems = _edit_problems(
                        current[r_span[0]:r_span[1]], retry["replace"], fix, allowed_urls,
                        exempt_text=current + "\n" + exempt_entity_text, headings=headings,
                        existing_assets=existing_assets, keep_images=keep_images,
                        section_rewrite=bool(retry.get("section_rewrite")))
                    if not r_problems:
                        edit, start, end, problems = retry, r_span[0], r_span[1], []
            if problems:
                skipped.append({"fix": fix, "why": "; ".join(problems),
                                "anchor": edit["find"][:120]})
                continue
            # the text is built from the edit that passed the guards: it used to be built once,
            # from the first attempt, before the guards ran, so when the first was refused and
            # the wider rewrite or the informed retry passed, the REFUSED edit was written and
            # the accepted one recorded (a dropped picture landed; the checklist showed an edit
            # the article did not have, real-time-translation-app, 2026-10-01)
            current = current[:start] + edit["replace"] + current[end:]
            applied.append({"fix": fix, "find": edit["find"], "replace": edit["replace"]})
        return {"markdown": current, "applied": applied, "skipped": skipped,
                "already_satisfied": satisfied, "changed": current != markdown}

    # A section the brief demanded and the draft never contained cannot be repaired by swapping
    # a span — there is nothing to swap, so both the anchored edit and the section rewrite come
    # back empty and the finding lands in the human queue as "write the missing X section":
    # a paragraph of instructions the machine was perfectly able to follow. Writing it is the
    # same job with an empty `before`, and every existing guard still applies against an empty
    # original — invented sources, ghost images and smuggled proper nouns are all still caught.
    MAX_NEW_SECTION_WORDS = 400

    def write_section(self, markdown: str, fix: str, context: str, title: str,
                      rejected: str | None = None) -> dict | None:
        """Write the section the article is missing, and say where it goes. Returns
        {"section": <markdown>, "at": <offset>, "note": ...} or None."""
        heads = [(m.start(), m.group(0).strip()) for m in _HEAD_LINE.finditer(markdown or "")]
        if not heads:
            return None
        head_list = "\n".join(f"{i}. {line}" for i, (_, line) in enumerate(heads, 1))

        prompt = f"""Write ONE new section for an article that is missing it.

{context}

WHAT THE EDITOR ASKED FOR:
{fix}
{self._rules_block()}
THE ARTICLE'S HEADINGS, in order:
{head_list}

Rules:
- Write ONLY the new section: its own heading line first, then its body. Nothing before or after.
- The heading text must be exactly: {title}
- Use the same heading level as the sections it will sit among.
- Use only facts already in the article, in the context above, or named in the fix itself.
  Invent no numbers, product names, prices, sources or links.
- Name no company, product, protocol, format or standard that the article, the context or the
  fix does not already name. This is enforced in code and it is the usual reason a section is
  thrown away: one draft reached for HTTP, JSON and WebRTC in an article that mentioned none
  of the three. Write around the gap — "the streaming connection", "the response payload".
- If the fix asks for a code example, write plain illustrative code, and no endpoint, header
  or key name you have not been given.
- Add no links and no images.
- No em dashes in prose — a comma, a colon or a full stop instead. The pipeline strips them
  afterwards either way; writing them just churns the text.
- At most {self.MAX_NEW_SECTION_WORDS} words. This section has to sit naturally among its
  neighbours, not dominate them.

Also place it: `after_heading` is one heading line copied VERBATIM from the list above — the
section your new one should follow. Choose the position the article's own logic implies.

Return JSON: {{"after_heading": "...", "section": "...", "note": "what you wrote and why there"}}"""
        if rejected:
            prompt += f"""

YOUR PREVIOUS ATTEMPT WAS REJECTED: {rejected}
Write the section again without that problem. Do not simply resubmit the same text."""

        from .base import parse_json
        raw = self.call_claude_cached(
            self.model, prompt, "",
            system="You are a precise staff writer adding one missing section to an article.",
            max_tokens=4000, agent_key="fix_applier", cache_base=False)
        data = parse_json(raw)
        if not isinstance(data, dict):
            return None

        section = str(data.get("section") or "").strip()
        if not section or not section.lstrip().startswith("#"):
            return None
        if len(section.split()) > self.MAX_NEW_SECTION_WORDS * 1.3:
            return None
        first_line = section.lstrip().splitlines()[0]
        if _norm(title).lower() not in _norm(first_line).lower():
            return None                       # it wrote some other section
        if _HEAD_LINE.search(markdown) and _norm(first_line).lower() in {
                _norm(l).lower() for _, l in heads}:
            return None                       # it already exists after all

        want = _norm(str(data.get("after_heading") or ""))
        at = None
        for start, line in heads:
            if _norm(line).lower() == want.lower():
                later = [h for h, _ in heads if h > start]
                at = later[0] if later else len(markdown)
                break
        if at is None:                        # unusable placement: put it before the last section
            at = heads[-1][0] if len(heads) > 1 else len(markdown)

        block = section.rstrip() + "\n\n"
        if at < len(markdown) and not markdown[:at].endswith("\n\n"):
            block = "\n" + block
        return {"section": block, "at": at, "note": data.get("note")}

    def rewrite_section(self, markdown: str, fix: str, context: str,
                        anchor_text: str | None = None) -> dict | None:
        """Fallback when one anchored swap cannot carry the fix: rewrite ONE SECTION.

        The measured reason serious findings reached a human was never "the model does not
        know the remedy" — it was that the remedy did not fit the format: 22 ran out of the
        two attempts, 12 could not be expressed as a single exact-match span, 8 were half
        done. A claim that must go from two places, a garbled sentence that needs its
        neighbours reflowed, a table row with a wrong mark — all trivially fixable, all
        forbidden by a one-span budget.

        A SECTION is the middle ground the design was missing: big enough to carry a real
        edit, small enough that every existing guard still applies to it (images, links,
        smuggled entities, growth). Whole-article rewrites stay banned — they are what
        deleted images and halved link counts before this module existed.
        """
        # a table gets addressed as a table: precise, and it keeps the prose around it out
        # of the model's hands entirely
        sec = _table_span(markdown, fix) or _section_span(markdown, anchor_text or "", fix)
        if not sec:
            return None
        start, end = sec
        original = markdown[start:end]
        if len(original.split()) > MAX_SECTION_WORDS:
            return None

        prompt = f"""Apply ONE fix by rewriting ONE SECTION of an article.

{context}

THE FIX TO APPLY:
{fix}
{self._rules_block()}
Rules:
- Return the block REWRITTEN and complete. A section starts with its own heading line; a
  markdown table starts with its header row and keeps every row it had unless the fix says
  otherwise.
- Change only what the fix requires. Every other sentence stays as it is, word for word.
- Keep every `![...](assets/...)` image line and every `[text](url)` link exactly as they
  are, unless the fix explicitly asks to remove one.
- Do not add facts, names, products or sources that are not already in the section or named
  in the fix. Removing an unsupported claim is a valid fix; inventing a replacement is not.
- Keep the length within about a quarter of the original — this is a repair, not a redraft.

Return JSON: {{"section": "<the full rewritten section>", "note": "what you changed"}}

THE SECTION:
{original}"""
        from .base import parse_json

        raw = self.call_claude_cached(
            self.model, prompt, "",
            system="You are a precise copy editor repairing one section of an article.",
            max_tokens=4000, agent_key="fix_applier", cache_base=False)
        data = parse_json(raw)
        if not isinstance(data, dict):
            return None
        new_sec = str(data.get("section") or "").strip()
        is_table = original.lstrip().startswith("|")
        if not new_sec or (not is_table and not new_sec.lstrip().startswith("#")):
            return None
        if is_table and not new_sec.lstrip().startswith("|"):
            return None
        n_old, n_new = len(original.split()), len(new_sec.split())
        if n_old and not (0.6 * n_old <= n_new <= 1.4 * n_old):
            return None
        return {"find": original, "replace": new_sec + ("\n" if original.endswith("\n") else ""),
                "note": data.get("note"), "section_rewrite": True}

    def _locate(self, markdown: str, fix: str, context: str,
                rejected: str | None = None) -> dict | None:
        # Prompt is split for caching: everything stable within a fix pass (rules + context +
        # the article) is the cacheable base; the fix itself is the tail. The fixer was the
        # QA phase's biggest UNCACHED input (346k tokens / 23 calls on one measured run) —
        # consecutive calls repay the prefix at 0.1× until an applied edit changes the article.
        base = f"""Apply ONE fix to the article below by replacing ONE span of text.

Return JSON: {{"find": "...", "replace": "...", "note": "..."}}

Rules that make this work:
- `find` is copied from the article VERBATIM — one sentence, or at most a short paragraph, and
  it must be UNIQUE in the article. If the text you would change appears more than once, extend
  `find` until it is unique. An anchor that does not match exactly is discarded and the fix is
  lost, so copy, do not retype.
- `find` must be at most {MAX_ANCHOR_WORDS} words. If the fix genuinely cannot be made in one
  span that small, return {{"find": "", "replace": "", "note": "why this needs a wider edit"}}
  instead of anchoring half the article.
- `replace` is the corrected text for exactly that span, and nothing else. Everything outside
  it stays byte-identical, which is the point: no other sentence, image, link or heading may
  change as a side effect of this fix.
- Keep every `![...](assets/...)` image line and every `[text](url)` link that falls inside
  `find` — dropping one is detected and the fix is thrown away.
- Do not lengthen the span materially. A replacement several times the size of the anchor is a
  rewrite and is rejected.
- If the fix is already satisfied in the current text, return an empty `find` and a `note`
  that BEGINS with the words "already satisfied", quoting the sentence that satisfies it.
  Say this only when it is literally true — the claim is recorded and later verified.
- If the fix asks you to work a phrase in and no sentence can take it without a reader noticing
  it as a keyword (bolted on, a product name used as a search query, a sentence bent around
  it), return an empty `find` and a `note` that BEGINS with the words "cannot read naturally",
  saying why in one sentence. A person then decides. Never force the phrase in.

{context}

ARTICLE:
{markdown}"""
        tail = f"\n\nTHE FIX TO APPLY (return ONLY the JSON):\n{fix}"
        name = (self.project.name or "").strip()
        if name and name.lower() in (fix or "").lower() and self.project.product_facts:
            # A fix about our own product is written against the product facts, not memory. Only
            # for such fixes, so the other locate calls do not carry the whole product sheet.
            tail += (f"\n\nPRODUCT FACTS about {name} (the only authority for any claim about our "
                     f"own product in the replacement):\n{self.project.product_facts}")
        tail += self._rules_block()
        if rejected:
            # Retrying the identical prompt and hoping for a better roll is not a retry, it is
            # a coin flip — and it was the top reason a Critical reached a human ("ran out of
            # attempts"). Tell the model what was wrong with its last edit; one informed try
            # beats four blind ones, and costs a quarter as much.
            tail += (f"\n\nYOUR PREVIOUS ATTEMPT WAS REJECTED: {rejected}\n"
                     f"Do not repeat it. Fix that specific problem in this attempt: copy the "
                     f"anchor from the article character for character, keep every image and "
                     f"link that falls inside it, and introduce no name that is not already in "
                     f"the article, the corpus or the fix itself.")
        from .base import parse_json

        raw = self.call_claude_cached(
            self.model, base, tail,
            system="You are a precise copy editor who applies exactly one targeted fix.",
            max_tokens=3000, agent_key="fix_applier", cache_base=False)
        data = parse_json(raw)
        if not isinstance(data, dict):
            return None
        find, replace = str(data.get("find") or ""), str(data.get("replace") or "")
        note = str(data.get("note") or "")
        if not find.strip():
            # An empty find is TWO different outcomes and they must not be conflated: "the fix
            # is already satisfied" is a positive attestation (the change is reported as not
            # needed, and its check says whether that holds), while anything else is a miss. The
            # attestation is only believed when the note says so — a model that loves declaring
            # work done gets no benefit of the doubt (measured: three "applied" phrase fixes
            # with zero phrases present).
            if re.search(r"(?i)\balready\s+(satisfied|met|present|correct|done|addressed)\b"
                         r"|\bno\s+(change|edit)\s+(is\s+)?(needed|required)\b", note):
                return {"already_satisfied": True, "note": note}
            if re.match(r"(?i)\s*cannot read naturally\b", note):
                return {"declined": True, "note": note.strip()}
            return None
        if len(find.split()) > MAX_ANCHOR_WORDS * 2:   # blatantly over the brief
            return None
        return {"find": find, "replace": replace, "note": note}
