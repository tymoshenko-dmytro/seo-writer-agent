"""Agent 5 — Editor: the panel review's reads.

`collect()` is one review: N independent reads of the same checklist by the panel
(pipeline.qa.panel, cross-vendor; sampled normally, the diversity IS the mechanism), unioned, plus
the code's own findings (keyword blockers, failed visuals, the picture check). No single read's
decision or score is consumed: measured on a frozen defective article, 4 of 4 valid reads rejected
it and each caught all three known defects, while about one read in five is anomalously lenient;
the union suppresses exactly that outlier. `core/qa_review.review` records what it returns as the
review's findings. A read that does not parse is kept as
`05_qa.parse-failure.r<n>.read<i>.txt` for inspection; nothing else is written here.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .base import BaseAgent
from ..qa_model import as_finding, norm_severity, structured_answer
from ..keyword_report import analyze as analyze_keywords
from ..keyword_report import format_coverage_report, thresholds_from
from ..validators import (analyze_structure, attested_numbers_from, format_structure_report,
                          gap_entities, uncited_numeral_paragraphs, validate_meta)

import re as _re

from ..store import atomic_write_text

# the textual shape of a keyword-placement demand, for the severity clamp (the check label
# alone is unreliable — reads name it "keywords", "seo", "keyword_coverage", or nothing)
_HEADING = _re.compile(r"^#{2,4}\s+(.+?)\s*$", _re.M)
_KW_PLACEMENT = _re.compile(
    r"(?i)\buse the (exact )?phrase\b|\bwork\b.{0,60}\binto\b"
    r"|\breword\b.{0,90}\bto (use|include|surface)\b|\binclude the (exact )?phrase\b"
    r"|\bkeyword\b.{0,40}\b(once|into|in the)\b")
# A finding that calls links unverified, invented or broken and asks to take them out: when every
# URL it names is a page of the site index, the index is the fact and the finding is wrong.
_URL_IN_FIX = _re.compile(r"https?://[^\s\"'“”‘’<>()\[\]`]+")
_DOUBTS_LINK = _re.compile(r"(?i)not (?:been )?(?:supplied|verified|provided|in the (?:index|list|research))"
                           r"|not in the (?:provided |supplied )?(?:url |link )?(?:inventory|index|list)"
                           r"|\bunsupplied\b|unverified|invented|fabricated|does(?:n't| not) exist|non-?existent"
                           r"|broken|made[- ]up")
_LINK_IN_QUOTE = _re.compile(r"\]\((https?://[^\s)]+)")
_UNLINK = _re.compile(r"(?i)\b(?:remove|unlink|drop|delete|strip|replace)\b")


def verified_link_doubts(fixes: list[dict], index_urls) -> list[dict]:
    """The findings that doubt internal links the site index holds (every URL they name is in
    it). One model asked to unlink four real blog posts as 'not supplied or verified', and as a
    Critical it blocked the article (interpreter replay, 2026-09-28)."""
    known = {str(u).rstrip("/").lower() for u in index_urls or ()}
    out = []
    for f in fixes:
        text = str(f.get("fix") or "")
        named = _URL_IN_FIX.findall(text)
        if not named:
            # the read's structured target holds the link it doubts; its prose says "this link" or
            # names a path ("the unsupplied '/contact-sales' link"): three Critical findings about
            # real pages of the site, 2026-10-05
            quote = str((((f.get("structured") or {}).get("target")) or {}).get("quote") or "")
            named = _LINK_IN_QUOTE.findall(quote)
        urls = [u.rstrip(".,;:!?”’'\"").rstrip("/").lower() for u in named]
        if urls and known and all(u in known for u in urls) and _DOUBTS_LINK.search(text) and _UNLINK.search(text):
            out.append(f)
    return out


def _section_title(md: str, needle: str) -> str | None:
    """The H2-H4 a text sits under, or None before the first heading."""
    pos = md.find(needle)
    if pos < 0:
        return None
    heads = [h for h in _HEADING.finditer(md) if h.start() < pos]
    return heads[-1].group(1).strip() if heads else None


class Editor(BaseAgent):
    name = "editor"
    model_role = "editor"  # stronger model

    READS = 3               # N reads per round (config: pipeline.qa.reads)
    MIN_VALID_READS = 2     # fewer parsed reads than this = invalid round, fail closed
    READ_MAX_TOKENS = 16000  # 4000 truncated verdicts (3/7 parse failures); 8000 lost one to a
                             # <think> preamble; on Claude 5 adaptive thinking bills as OUTPUT,
                             # so the budget covers thinking + the JSON (effort capped at medium)

    def collect(self, state) -> dict:
        """The review's raw findings, before anything decides what they are: the panel's reads
        (each finding with its structured answer), the code-originated keyword and visual
        findings, and the picture check. {"union", "reads", "n_reads", "valid", "round",
        "qa_cfg"}. core/qa_review records them as the review's findings."""
        state.current_agent = "editor"
        out = Path(state.output_dir)
        qa_cfg = (self.config.get("pipeline") or {}).get("qa") or {}
        n_reads = int(qa_cfg.get("reads", self.READS))

        state.qa_rounds = (state.qa_rounds or 0) + 1
        rnd = state.qa_rounds

        # Keyword coverage is measured HERE, before the reads, so a thin primary keyword becomes
        # a scored defect with a concrete fix — and (new) so the code-blocking keyword checks can
        # be injected as serious findings the loop actually works on.
        try:
            state.keywords = analyze_keywords(state.draft_markdown, state.keyword_research,
                                              state.brief, thresholds_from(self.project.seo))
        except Exception as e:  # noqa: BLE001
            state.keywords = {}
            state.note(f"[editor] keyword coverage unavailable: {e}")

        user = self._user_message(state)
        hints = self._hint_block(state)

        # --- N reads by the PANEL. Read 1 carries the code-generated attention hints; the
        # rest stay un-anchored so the hints cannot correlate every read's attention. The
        # panel is cross-vendor by config (pipeline.qa.panel): different models have
        # different blind spots, which is the one thing more reads of the same model cannot
        # buy. Claude reads go through the cached path (system + user base are stable within
        # a round — reads 2..N pay 0.1× for most of their input); the OpenAI read relies on
        # that vendor's automatic prefix caching. A read that fails (parse OR transport) is
        # dropped; the round fails closed below MIN_VALID_READS.
        panel = [str(p) for p in (qa_cfg.get("panel") or []) if str(p).strip()] or [self.model]
        reads: list[dict] = []
        from .base import parse_json

        # Caching the volatile base pays only when the SAME model sees it again: cache is
        # per-model, so a panel of three distinct models reads each article version once and
        # a breakpoint there buys nothing but the write premium. Measured on one run: 278k
        # written, 0 read, per model.
        schedule = [panel[i % len(panel)] for i in range(n_reads)]
        reuses = {spec: schedule.count(spec) > 1 for spec in set(schedule)}

        for i in range(n_reads):
            spec = panel[i % len(panel)]
            tail = hints if (i == 0 and hints) else ""
            data, raw = None, ""
            try:
                raw = self._panel_read(spec, user, tail, reuses_base=reuses[spec])
                data = parse_json(raw)
                if not data:
                    raw = self._panel_read(
                        spec, user,
                        tail + "\n\nReturn ONLY the JSON object, with no prose before or "
                               "after it.", reuses_base=reuses[spec])
                    data = parse_json(raw)
            except Exception as e:  # noqa: BLE001 — one lost voice must not kill the round
                state.note(f"[editor] round {rnd}: read {i + 1} ({spec}) errored — {e}")
            if isinstance(data, dict) and (data.get("required_fixes") is not None
                                           or data.get("decision")):
                data["_hinted"] = (i == 0 and bool(hints))
                data["_model"] = spec
                data["_read"] = i
                reads.append(data)
            else:
                atomic_write_text(out / f"05_qa.parse-failure.r{rnd}.read{i + 1}.txt", raw or "(empty)")
                state.note(f"[editor] round {rnd}: read {i + 1} ({spec}) did not parse — "
                           f"dropped (raw saved)." + self.edited_prompt_hint())
        valid_round = len(reads) >= self.MIN_VALID_READS

        # --- union the reads' findings + inject the code-originated keyword blockers
        primary_kw = str((state.keyword_research or {}).get("primary_keyword") or "").lower()
        max_per_read = int(qa_cfg.get("max_fixes_per_read", 12))
        union: list[dict] = []
        for r_ in reads:
            # Per-read cap: one hyper-strict voice must not flood the fix queue (measured:
            # a cross-vendor read returned 24 Critical citation demands in one pass — models
            # list their most important findings first, so the tail is the cheapest to drop).
            fixes_list = (r_.get("required_fixes") or [])[:max_per_read]
            if len(r_.get("required_fixes") or []) > max_per_read:
                state.note(f"[editor] round {rnd}: read [{r_.get('_model')}] returned "
                           f"{len(r_['required_fixes'])} fixes — capped at {max_per_read}")
            for raw_fix in fixes_list:
                f = as_finding(raw_fix)
                # the read's structured answer (prompts/editor.md: kind, action, target, phrase),
                # which the review routes by (core/qa_model.route)
                f["structured"] = structured_answer(raw_fix)
                f["voice"] = f"read{r_.get('_read')}:{r_.get('_model')}"   # one read = one voice
                f["hinted"] = bool(r_.get("_hinted"))
                # Deterministic severity clamp: secondary/LSI keyword-placement fixes are
                # Minor by the prompt's own rule, but reads still inflate them often enough to
                # stall convergence (measured: two no_converge exits driven by them). Matched
                # by check name OR by the fix's own shape — reads label the check field
                # inconsistently ("keywords", "seo", "keyword_coverage"), and a clamp that
                # trusts the label alone missed half the class (clean run 2). The PRIMARY
                # keyword keeps whatever severity the read gave it — its floors are real
                # blockers (and are code-injected as Major anyway).
                kw_shaped = (str(f.get("check") or "").lower().startswith("keyword")
                             or _KW_PLACEMENT.search(f["fix"]))
                if (kw_shaped and norm_severity(f.get("severity")) in ("major", "critical")
                        and (not primary_kw or primary_kw not in f["fix"].lower())):
                    f["severity"] = "minor"
                union.append(f)
        doubts = verified_link_doubts(union, self._index_urls(state))
        if doubts:
            union = [f for f in union if f not in doubts]
            state.note(f"[editor] round {rnd}: dropped {len(doubts)} finding(s) calling internal links unverified: "
                       f"every URL they name is in the site index")
        union.extend(self._keyword_blocker_findings(state))
        union.extend(self._visual_failure_findings(state))
        union.extend(self._picture_findings(state))
        return {"union": union, "reads": reads, "n_reads": n_reads, "valid": valid_round,
                "round": rnd, "qa_cfg": qa_cfg}

    # ---- round helpers ----
    def _panel_read(self, spec: str, user_base: str, user_tail: str,
                    reuses_base: bool = False) -> str:
        """One panel voice: 'claude-*' via the cached Anthropic path, 'openai:*' cross-vendor."""
        if spec.startswith("openai:") or spec.startswith("gpt-"):
            model = spec.split(":", 1)[-1]
            return self.call_openai(model, user_base + user_tail,
                                    max_tokens=self.READ_MAX_TOKENS,
                                    agent_key=f"editor[{model}]")
        return self.call_claude_cached(spec, user_base, user_tail,
                                       max_tokens=self.READ_MAX_TOKENS,
                                       agent_key=f"editor[{spec}]",
                                       cache_base=reuses_base)

    def _keyword_blocker_findings(self, state) -> list[dict]:
        """The three code-blocking keyword checks, injected as serious findings.

        A read may label a failed keyword threshold Minor while the keyword report fails it
        (measured: the threshold failed in 4 of 5 test runs), so the gap would sit unworked.
        Injected here, the review lists it as serious, routed by its structured answer like any
        finding (qa_model.route). Only the fail-status checks are injected; secondary/LSI
        coverage stays warn-only.
        """
        kw = state.keywords or {}
        p = kw.get("primary") or {}
        t = kw.get("thresholds") or {}
        pk = p.get("keyword")
        if not pk:
            return []
        out = []
        for c in (kw.get("checks") or []):
            if c.get("status") != "fail":
                continue
            name = c.get("name") or ""
            if name == "Primary keyword in the H1":
                fix = (f"Reword the H1 to contain the exact phrase '{pk}' naturally "
                       f"(keep it under 10 words; do not bolt the phrase on).")
                h1 = _re.search(r"^#\s+(.+?)\s*$", state.draft_markdown or "", _re.M)
                shape = {"kind": "keyword", "action": "edit", "target": h1.group(1) if h1 else None}
            elif name == "Primary keyword count":
                fix = (f"The primary keyword '{pk}' appears {p.get('total')} time(s); the floor "
                       f"is {t.get('primary_min_total')}. Reword existing sentences so the "
                       f"exact phrase '{pk}' appears at least {t.get('primary_min_total')} "
                       f"times across H1, headings and body — reword, never add new sentences.")
                # no one section to work it into: a proposal, never a machine change
                shape = {"kind": "keyword", "action": "insert_keyword", "target": None, "phrase": pk}
            elif name == "Primary keyword density":
                fix = (f"Primary keyword density is {p.get('density')}%, above the "
                       f"{t.get('primary_density_max')}% stuffing ceiling. Remove or reword "
                       f"occurrences of '{pk}' until it reads naturally.")
                shape = {"kind": "keyword", "action": "edit", "target": None}
            elif name == "Mandatory keywords":
                grp = (kw.get("groups") or {}).get("mandatory") or {}
                for r in grp.get("rows") or []:
                    if r.get("total"):
                        continue
                    where = f" in the section '{r['section']}'" if r.get("section") else ""
                    out.append({"fix": (f"The SEO-mandatory keyword '{r['keyword']}' is missing{where}. "
                                        f"Reword an existing sentence so the exact phrase appears naturally. "
                                        f"If it names a product the research has no facts for, a definitional "
                                        f"mention is enough (what it is and who makes it) — no prices or features."),
                                "severity": "major", "check": name, "source": "code",
                                "kind": "keyword", "action": "insert_keyword",
                                "target": r.get("section") or None, "phrase": r["keyword"]})
                continue
            else:
                continue    # "Primary keyword" (missing entirely) has no textual fix
            out.append({"fix": fix, "severity": "major", "check": name, "source": "code", **shape})
        return out

    def _picture_findings(self, state) -> list[dict]:
        """What a vision model sees in each picture, with its caption and the text around it
        (agents/visual_checker): until 2026-09-30 nothing looked at a rendered picture, and an
        empty chart, a page 11 000 px tall and a testimonial block under a product caption all
        passed. pipeline.qa.visual_check turns it off; a picture the check could not look at is
        noted and finds nothing."""
        # what the check did, for the page: off, could not run, or which pictures it looked at
        # (a picture with no finding is "no problem found" only when it was looked at; RB32-04)
        if not (((self.config.get("pipeline") or {}).get("qa") or {}).get("visual_check", True)):
            state.picture_check = {"ran": False, "off": True}
            return []
        from .visual_checker import VisualChecker

        cover = (state.frontmatter or {}).get("cover")
        try:
            results = VisualChecker(self.project, self.config).check(state.output_dir, state.draft_markdown or "", cover)
        except Exception as e:  # noqa: BLE001 — the review goes on without it
            state.note(f"[editor] the picture check did not run: {e}")
            state.picture_check = {"ran": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
            return []
        failed = [r["path"] for r in results if r["error"]]
        state.picture_check = {"ran": True, "looked_at": [r["path"] for r in results if not r["error"]],
                               "could_not": failed,
                               # the words each was looked at with: its word holds for them (RA3C-02)
                               "words": {r["path"]: r["words"] for r in results if not r["error"] and r.get("words")}}
        bad = [r for r in results if r["problems"]]
        state.note(f"[editor] picture check: {len(results)} looked at, {len(bad)} with problems"
                   + (f", {len(failed)} could not be checked: {', '.join(failed[:4])}" if failed else ""))
        md = state.draft_markdown or ""
        sections = {r["path"]: _section_title(md, r["path"]) for r in results}
        return VisualChecker.as_findings(results, sections)

    def _visual_failure_findings(self, state) -> list[dict]:
        """Refused screenshots, injected as serious findings while their section has no image.

        A refused capture used to vanish: the marker was deleted and the reason printed to
        stdout, so nobody learned that a briefed per-product screenshot was missing. It is raised
        by every review while its section is still empty, as a proposal (adding a picture is not
        one of the machine's actions); once any image sits in the section it is not raised
        again. Known limits: a section that already held another image (a chart) is taken
        as filled, and a refused screenshot placed before the first heading has no section, so it
        reaches only the run log.
        """
        md = state.draft_markdown or ""
        heads = [(h.start(), h.group(1).strip()) for h in _HEADING.finditer(md)]
        out = []
        for v in (state.visuals or []):
            if v.get("type") != "screenshot" or v.get("status") != "skipped" or not v.get("reason"):
                continue
            section = (v.get("section") or "").strip()
            start = next((pos for pos, title in heads if title == section), None) if section else None
            if start is None:
                continue            # no section to fill (or it was rewritten away)
            end = next((pos for pos, _ in heads if pos > start), len(md))
            if "](assets/" in md[start:end]:
                continue            # a visual already sits in that section
            fix = (f"The screenshot for the section '{section}' could not be captured from "
                   f"{v.get('url')} ({v.get('reason')}: {v.get('detail') or 'no detail'}). Add a "
                   f"screenshot of the product's own page from another official page of the same "
                   f"vendor (docs, pricing, app-store listing), or a branded card built only from "
                   f"this section's sourced facts.")
            # a picture to add is not one of the model's actions: a proposal
            out.append({"fix": fix, "severity": "major", "check": "visual", "source": "code",
                        "kind": "picture", "action": None, "target": None})
        return out

    def _hint_block(self, state) -> str:
        """Code-generated attention routing for ONE read per round. Hints, never verdicts."""
        try:
            ents = gap_entities(state.research, state.draft_markdown)
            attested = attested_numbers_from(self.project.product_facts)
            paras = uncited_numeral_paragraphs(state.draft_markdown, attested)
        except Exception as e:  # noqa: BLE001 — hints must never fail a round
            state.note(f"[editor] hint block unavailable: {e}")
            return ""
        if not ents and not paras:
            return ""
        L = ["\n\nATTENTION ROUTING (code-generated candidates — CHECK THESE FIRST. They are "
             "hints, not verdicts: judge each one against the sources of truth above):"]
        if ents:
            L.append("- The research corpus mentions these entities ONLY in unanswered "
                     "questions or gaps, and the article discusses them. Any factual claim "
                     "the article makes about them must trace to the researched facts — if it "
                     "does not, that is an unsourced claim: " + ", ".join(ents))
        for p in paras:
            L.append(f"- Paragraph with numbers and no inline citation: \"{p}\"")
        return "\n".join(L)

    def _visuals_block(self, state) -> str:
        """Every visual that SHIPS, including the ones the body markdown does not show.

        The cover lives in the frontmatter, so an editor reading only the body concluded the
        article had no hero image and demanded one — on an article that has had a cover all
        along. Same two-views-of-one-fact shape as the benchmark and FAQ cases.
        """
        from pathlib import Path as _P

        cover = (state.frontmatter or {}).get("cover")
        L = []
        if cover:
            L.append(f"- COVER / hero image: {cover} — it ships with the article as the "
                     f"featured image. It is NOT in the body markdown, so do not report the "
                     f"article as lacking a cover or hero image.")
        # every image the body embeds, each checked on disk. The list used to be the first 12 asset
        # files in name order: on an article with 15 files, screenshot_5..7 fell off it, and all
        # three reads raised "broken image reference" Criticals about files that were there
        # (webinar-translation trial, 2026-09-28)
        refs = list(dict.fromkeys(_re.findall(r"\]\((assets/[^)\s]+)\)", state.draft_markdown or "")))
        missing = [r for r in refs if not (_P(state.output_dir) / r).is_file()]   # any image type
        L.append(f"- {len(refs)} image(s) embedded in the body: "
                 + (", ".join(r.split("/", 1)[1] for r in refs) or "none") + ". "
                 + (f"MISSING on disk: {', '.join(missing)}." if missing
                    else "Every one of them exists on disk."))
        return "\n".join(L)

    def _faq_block(self, state) -> str:
        faq = (state.frontmatter or {}).get("faq") or []
        if not faq:
            return "(no FAQ attached)"
        L = []
        for item in faq:
            if isinstance(item, dict):
                L.append(f"Q: {item.get('question')}\nA: {item.get('answer')}")
            else:
                L.append(f"Q: {item}")
        return "\n".join(L)

    def _index_urls(self, state) -> list[str]:
        """Every page we actually have: the WHOLE site index plus this article's shortlist.
        "Invented" means "not one of these" — linking another real page is fine."""
        allowed = []
        try:
            from ..tools.link_index import LinkIndex

            idx = LinkIndex.load(self.project.root)
            allowed = [t.url for t in idx.targets] if idx else []
            p = Path(state.output_dir) / "_links.json"
            if p.exists():
                allowed += [c["url"] for c in json.loads(p.read_text()).get("candidates", [])]
        except Exception:  # noqa: BLE001 — the report is still useful without it
            pass
        return allowed

    def _structure_block(self, state) -> str:
        """Measured, not estimated — the model must not count words itself."""
        allowed = self._index_urls(state)
        r_ = state.research or {}
        srcs = {x["source_url"] for x in (r_.get("key_statistics") or []) if x.get("source_url")}
        for f_ in (r_.get("findings") or []):
            for fact in (f_.get("facts") or []):
                if fact.get("source_url"):
                    srcs.add(fact["source_url"])
        rep = analyze_structure(
            state.draft_markdown,
            target_words=state.target_words or (state.brief or {}).get("target_words") or 0,
            allowed_urls=allowed,
            words_per_link=(self.project.links or {}).get("words_per_link", 350),
            sources_available=len(srcs),
            competitor_hosts=(self.project.links or {}).get("competitor_hosts") or (),
            competitor_doc_paths=(self.project.links or {}).get("competitor_doc_paths") or (),
            article_dir=getattr(state, "output_dir", "") or None,
        )
        state.structure = rep
        return format_structure_report(rep)

    def _length_band(self, state) -> str:
        """The competitive length range, not a single number.

        `target_words` is the MEDIAN of three prototype competitors, and that median is noisy:
        one measured SERP ran 1 400 / 2 760 / 3 681 words. Judging +/-10% around the median of
        three samples with that spread produces a Major the revision loop cannot clear — twice
        in a row an article was rejected purely on length (+12% against a 3 071 target, then
        +29% against a 1 400 one), because closing the gap means deleting a third of the piece.
        "No longer than anyone who actually ranks" is a defensible rule; "within 10% of a noisy
        median" is not. So the Editor gets the band and judges against its ceiling.
        """
        bench = self.length_sample((state.keyword_research or {}).get("content_benchmark"))
        target = state.target_words or (state.brief or {}).get("target_words") or 0
        lo, med, hi = bench.get("words_min"), bench.get("words_median"), bench.get("words_max")
        # A quartile over one or two pages is not a measurement. Three articles were cut roughly
        # in half by a target derived from two thin pages, so below the floor the band is not
        # reported as evidence at all.
        if (bench.get("sample") or 0) < 3:
            hi = None
            read = bench.get("sample") or 0
            return (f"- Target {target} words — but only {read} page(s) from THIS keyword's own "
                    f"SERP could be measured, which is too few to define a range, so this "
                    f"number is the project's standard depth, not evidence about the field.\n"
                    f"- **Do NOT score length as a defect.** With no measured competitor range "
                    f"there is nothing to be over or under. Note the word count in `feedback` "
                    f"and move on. Enforcing a tolerance around a guessed target is the least "
                    f"defensible thing you could do here.")
        # 2% headroom: one run generated a required_fix for being 16 words over a 2 934-word
        # ceiling, which is measurement noise, not bloat.
        ceiling = int(max(target * 1.10, hi) * 1.02)
        return (f"- Aim: {target} words (median of {bench.get('sample')} ranking competitors)\n"
                f"- Competitors measured: {lo}-{hi} words\n"
                f"- HARD CEILING: {ceiling} words — the longer of the target +10% and the "
                f"longest competitor. Over this is a Major; under it is fine even if it is well "
                f"above the median.")

    def _meta_block(self, state) -> str:
        b = state.brief or {}
        kw = state.keyword_research or {}
        spec = (kw.get("meta") or {}).get("spec") or {
            "title_min": 50, "title_max": 70, "description_min": 130, "description_max": 150}
        t, d = b.get("meta_title") or "", b.get("meta_description") or ""
        problems = validate_meta(b, kw.get("primary_keyword", ""), b.get("title", ""),
                                 spec["title_min"], spec["title_max"],
                                 spec["description_min"], spec["description_max"])
        L = [f"- H1: \"{b.get('title', '')}\" ({len(str(b.get('title', '')).split())} words)",
             f"- meta_title: {len(t)} chars (spec {spec['title_min']}-{spec['title_max']}) — \"{t}\"",
             f"- meta_description: {len(d)} chars (spec {spec['description_min']}-"
             f"{spec['description_max']}) — \"{d}\""]
        L.append("- verdict: " + ("within spec" if not problems
                                  else "OFF SPEC — " + "; ".join(problems)))
        return "\n".join(L)

    @staticmethod
    def _coverage_block(state) -> str:
        kw = state.keywords or {}
        if not kw:
            return "(keyword coverage could not be measured)"
        return format_coverage_report(kw, max_items=8)

    @staticmethod
    def _content_block(state) -> str:
        rows = [f"- {s.get('h2')}: {'; '.join(s['content_types'])}"
                for s in (state.brief or {}).get("sections", []) if s.get("content_types")]
        return "\n".join(rows) or "(the brief specified no per-section content types)"

    def _user_message(self, state) -> str:
        kw = state.keyword_research or {}
        b = state.brief or {}
        related = ", ".join([state.topic] + [r["keyword"] for r in (kw.get("related") or [])[:10]])
        sections = "; ".join(s.get("h2", "") for s in b.get("sections", []))
        return f"""Review this article for {self.project.name} and return a QA verdict as JSON.
TODAY: {date.today().isoformat()} — every year in the article must be {date.today().year}.

PRIMARY KEYWORD: {kw.get('primary_keyword') or state.topic}
KEYWORDS TO USE NATURALLY: {related}
BRIEF SECTIONS (article should cover these): {sections}

MEASURED STRUCTURE (counted programmatically — trust these numbers over your own counting,
and apply checklist items 5 and 6 against them):
{self._structure_block(state)}

MEASURED META TAGS (character counts done in code — do not re-count):
{self._meta_block(state)}

MEASURED LENGTH BAND (judge checklist item 5's length rule against THIS, not against the
target alone):
{self._length_band(state)}

MEASURED KEYWORD COVERAGE (counted in the finished text — apply checklist item 7a against it,
and turn each MISSING line into a concrete required_fix):
{self._coverage_block(state)}

CONTENT THE BRIEF ORDERED PER SECTION (check each was actually built and fully populated):
{self._content_block(state)}

RESEARCHED FACTS — the view the writer was given, byte for byte. Any statistic or third-party
claim in the article MUST trace here (with an inline link); anything under GAPS must NOT be
presented as fact:
{self.render_corpus(state.research)}

VISUALS THAT SHIP WITH THIS ARTICLE (the body markdown is not the whole picture):
{self._visuals_block(state)}

FAQ THAT SHIPS WITH THE ARTICLE — customer-facing copy, so the same factual-accuracy and
brand rules apply. It is PLAIN TEXT structured data: it cannot contain links, so never ask
for a citation here; an unsupported claim in an answer is reworded or dropped instead:
{self._faq_block(state)}

Apply the full checklist and scoring from your instructions. Name the exact location of every
issue in required_fixes (e.g. "empty Rationale cell in the decision table, 'Quarterly
planning' row").

ARTICLE (Markdown):
{state.draft_markdown}"""
