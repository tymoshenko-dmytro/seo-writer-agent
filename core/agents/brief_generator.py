"""Agent 2 — the SEO brief.

Consumes the keyword research (Agent 1) and turns it into the article's SEO brief: the outline
built from the search results (what the ranking pages cover, merged and with the gap added), the
keywords each section carries, the format each section needs, and the research questions that
guide the Deep Researcher. How the article presents the product is the writer's, from its own
product brief (prompts/copywriter.md and products.md); the brief planned it too until
2026-10-01, and its "when to choose X vs us" section repeated the article's own decision guide.
Output: JSON brief + `02_brief.md`.
"""
from __future__ import annotations

from pathlib import Path

from .base import BaseAgent
from ..keyword_report import mark_mandatory
from ..validators import normalize_brief_structure, validate_brief
from ..store import atomic_write_text


class BriefGenerator(BaseAgent):
    name = "brief_generator"

    def run(self, state):
        state.current_agent = "brief_generator"
        out = Path(state.output_dir)
        kr = state.keyword_research or {}

        data, raw = self.call_claude_json(self._user_message(state, kr), max_tokens=8000)
        if not isinstance(data, dict) or not data:
            # a list or a bare value is not a brief either (RF-22); the orchestrator stops the run
            # before the paid deep research when the brief has no sections (RF-28)
            data = {"title": state.topic, "sections": [], "research_questions": [], "_raw": raw}
            state.note("[brief] the brief did not come back as a JSON object — stored the raw output")
        else:
            data = self._enforce_structure(state, data, kr.get('primary_keyword', ''))
            self._enforce_meta(state, data, kr)
        if kr.get("mandatory_keywords"):
            data["mandatory_keywords"] = list(kr["mandatory_keywords"])   # the brief carries SEO priority
        state.brief = data

        tw = data.get("target_words")
        if isinstance(tw, int) and tw > 0:
            state.target_words = tw

        atomic_write_text(out / "02_brief.md", self._render(state, data))
        state.note(
            f"[brief] sections={len(data.get('sections', []))} target_words={state.target_words} "
            f"research_questions={len(data.get('research_questions', []))}"
        )
        return state

    def _enforce_meta(self, state, data: dict, kr: dict) -> None:
        """The brief may reword the research stage's meta tags — so re-check and repair them.

        If the repair still misses the spec, fall back to the researched pair (already
        validated in code) rather than shipping something off spec.
        """
        spec = (kr.get("meta") or {}).get("spec") or self.META_SPEC
        primary = kr.get("primary_keyword", "")
        res = self.ensure_meta(data, primary, data.get("title", ""), spec)
        if not res["problems"]:
            data["meta_title"], data["meta_description"] = res["meta_title"], res["meta_description"]
            state.note(f"[brief] meta OK ({len(res['meta_title'])}/"
                       f"{len(res['meta_description'])} ch, {res['rounds']} repair round(s))")
            return
        researched = kr.get("meta") or {}
        if researched.get("meta_title") and not researched.get("problems"):
            data["meta_title"] = researched["meta_title"]
            data["meta_description"] = researched["meta_description"]
            state.note(f"[brief] meta still off spec after {res['rounds']} round(s) — restored "
                       f"the validated pair from keyword research")
        else:
            data["meta_title"], data["meta_description"] = res["meta_title"], res["meta_description"]
            state.note(f"[brief] meta off spec and no validated fallback: "
                       + " | ".join(res["problems"][:3]))

    def _enforce_structure(self, state, data: dict, primary: str = "") -> dict:
        """The outline is where article length is decided, so it gets a hard gate.

        Structural violations (too many sections, H3 sprawl, budgets that don't add up,
        two sections covering the same ground) are checked in code and sent back for ONE
        repair pass. Cheap here; expensive after research and writing.
        """
        # absolute rules first, enforced in code rather than asked for again
        for change in normalize_brief_structure(data):
            state.note(f"[brief] {change}")

        kr = state.keyword_research or {}
        problems = validate_brief(data, primary_keyword=primary,
                                  mandatory=kr.get("mandatory_keywords") or [])
        if not problems:
            state.note(f"[brief] structure OK: {len(data.get('sections', []))} H2, "
                       f"{sum(len(s.get('h3') or []) for s in data.get('sections', []))} H3")
            return data

        state.note(f"[brief] structure violations ({len(problems)}) — requesting a repair pass: "
                   + " | ".join(problems[:4]))
        import json as _json

        repair = f"""Your outline breaks the structure budget. Fix it and return the COMPLETE
brief JSON again (same schema, all other fields preserved).

VIOLATIONS:
{chr(10).join('- ' + p for p in problems)}

Fix them by MERGING overlapping sections and folding thin H3s into their parent's prose —
not by deleting coverage. Keep the merged section's keywords. Re-balance the `words`
budgets so they sum to target_words.

CURRENT BRIEF:
{_json.dumps(data, ensure_ascii=False)[:12000]}

Return ONLY the corrected JSON."""
        fixed, _ = self.call_claude_json(repair, max_tokens=8000)
        if not fixed or not fixed.get("sections"):
            state.note("[brief] repair pass failed — keeping the original outline")
            return data

        left = validate_brief(fixed, primary_keyword=primary,
                              mandatory=kr.get("mandatory_keywords") or [])
        state.note(f"[brief] repaired: {len(data.get('sections', []))} -> {len(fixed['sections'])} H2, "
                   f"{sum(len(s.get('h3') or []) for s in data.get('sections', []))} -> "
                   f"{sum(len(s.get('h3') or []) for s in fixed['sections'])} H3, "
                   f"{len(problems)} -> {len(left)} violations")
        return fixed

    def _user_message(self, state, kr) -> str:
        a = kr.get("analysis", {})
        sl = a.get("serp_landscape", {})
        ps = kr.get("primary_stats") or {}
        ps_kd = ps.get("kd", ps.get("difficulty"))
        bench = kr.get("content_benchmark") or {}
        meta = kr.get("meta") or {}
        clusters = "\n".join(
            f"- {c['theme']}: vol {c['total_volume']}, KD~{c['avg_kd']} — {', '.join(c['keywords'][:14])}"
            for c in kr.get("clusters_ranked", [])
        ) or "(none)"
        lsi = ", ".join(x["keyword"] for x in (kr.get("lsi") or [])) or "(none)"
        serp_outline = "\n".join(f"- {h}" for h in a.get("recommended_sections", [])) or "(none)"
        content_plan = "\n".join(f"- {c}" for c in a.get("content_plan", [])) or "(none specified)"
        qs = "\n".join(f"- {q}" for q in kr.get("questions", [])[:30]) or "(none)"
        protos = self._prototype_block(kr)
        bench_block = self._bench_block(bench)
        target_hint, length_rule = self._length_instruction(bench, sl)

        return f"""TOPIC: {state.topic}
PRIMARY KEYWORD: {kr.get('primary_keyword')} (vol {ps.get('volume')}, KD {ps_kd}) — intent {a.get('search_intent')}
AUDIENCE: {self.project.audience}
MARKET: {kr.get('country', 'us')}

== META TAGS ALREADY DRAFTED AND LENGTH-VALIDATED IN THE RESEARCH STAGE ==
Reuse these verbatim unless they are factually wrong for the article you are briefing. They
were checked in code against the character spec, so rewriting them means re-earning that.
- title (H1 candidate): {meta.get('h1_suggestion') or a.get('h1_suggestion') or '(none)'}
- meta_title ({meta.get('title_len')} ch): {meta.get('meta_title') or '(none)'}
- meta_description ({meta.get('description_len')} ch): {meta.get('meta_description') or '(none)'}

== SERP RESEARCH (what it takes to rank) ==
Dominant content type: {sl.get('dominant_content_type')}
Competitiveness: {sl.get('competitiveness')}
What competitors cover: {sl.get('what_they_cover')}
CONTENT GAP: {sl.get('content_gap')}

== PROTOTYPE COMPETITORS — the three articles we are planning against ==
{protos}

== MEASURED FORMAT BENCHMARK (counted in code from those pages) ==
{bench_block}
{length_rule}

CONTENT TYPES THE FORMAT NEEDS (assign each to a section via `content_types`):
{content_plan}

KEYWORD CLUSTERS (ranked by volume — assign these to sections):
{clusters}

LSI VOCABULARY — terms the ranking pages talk about. These are words and entities for the
PROSE, so the article reads as on-topic. They are NOT section ideas and NOT keywords to rank
for; never build a heading out of one:
{lsi}

SERP-DERIVED OUTLINE (from search: keep what the ranking pages all cover, merge what overlaps,
add the gap; do not copy):
{serp_outline}

QUESTIONS PEOPLE ASK (these feed the `faq` field, NOT body headings):
{qs}

SECONDARY TARGETS: {', '.join(a.get('secondary_keywords', []))}

MANDATORY KEYWORDS (set by SEO; assign each to the section where it fits — the article must carry
all of them): {', '.join(kr.get('mandatory_keywords') or []) or '(none)'}

== TASK ==
Produce the SEO brief JSON per your instructions: an outline that satisfies the search intent
so it can rank, every keyword cluster placed in one section, the format each section needs, the
gap sections, and the research questions the sections need answered. Return ONLY JSON."""

    @staticmethod
    def _prototype_block(kr) -> str:
        """Prototype teardowns: their real structure, and what content sits inside it."""
        protos = kr.get("prototypes") or []
        if not protos:
            return ("(no article competitors in this SERP — it is not a listicle SERP; plan the "
                    "format from the search intent instead)")
        L = []
        for p in protos:
            au = p.get("audit") or {}
            imgs = au.get("images") or {}
            tables = au.get("tables") or []
            faq = au.get("faq") or {}
            L.append(
                f"[{p['role'].upper()}] {(p.get('title') or p['url'])[:80]} — {p['url']}\n"
                f"  {au.get('words')} words · {au.get('h2')} H2 / {au.get('h3')} H3 · "
                f"{imgs.get('total', 0)} images {imgs.get('kinds') or ''} · {len(tables)} tables · "
                f"{au.get('videos', 0)} video · FAQ: {faq.get('how') or 'none'}\n"
                + ("".join(f"  table: {', '.join(t['headers'][:7])}\n" for t in tables[:3]))
                + (f"  template junk in their outline — do NOT copy: "
                   f"{'; '.join((au.get('template_junk') or [])[:8])}\n" if au.get("template_junk") else "")
                + "  their structure:\n"
                + ("\n".join(
                    f"    {h['lvl']} [{h['words']}w"
                    + (", " + ", ".join(f"{k}×{v}" for k, v in h["has"].items()) if h.get("has") else "")
                    + f"] {h['text']}" for h in (au.get("outline") or [])[:22])
                   or "    (unavailable)"))
        return "\n".join(L)

    # A benchmark built from one or two pages is not a benchmark, and it must not be allowed to
    # set the length. Measured: `audio ai news` qualified a single prototype, so words_min,
    # words_target and words_max were all 1581 — one page became the aim AND the hard ceiling.
    # The target fell 2800 -> 1581, and because the writer's other budgets are derived from the
    # target, the article came out with half the words, half the images (10 -> 5), two thirds of
    # the citations (18 -> 12) and 5 internal links instead of 8. The keyword stage had already
    # printed "the format benchmark is weak; length should not be scored against it" — but that
    # note only ever reached the Editor, while the brief was still told to obey the number.
    MIN_BENCH_SAMPLE = 3

    def _length_instruction(self, bench_all, sl) -> tuple[int | None, str]:
        """The aim, and the paragraph that explains how much to trust it."""
        bench = self.length_sample(bench_all)
        # `pipeline.target_words` — the same figure the orchestrator seeds the run with, so the
        # fallback is the project's own configured depth rather than a number invented here.
        house = (getattr(self.project, "defaults", None) or {}).get("target_words") or 2000
        sample = bench.get("sample") or 0
        measured = bench.get("words_target") or bench.get("words_median")

        if sample >= self.MIN_BENCH_SAMPLE and measured:
            return measured, (
                f"Set `target_words` from this benchmark: aim for **{measured} words**, which is "
                f"the UPPER part of what the ranking pages carry, not their middle. The ceiling "
                f"is the longest competitor ({bench.get('words_max') or 'unknown'} words) — do "
                f"not exceed it. Do not undercut the aim by more than ~15%: a page thinner than "
                f"the field is unlikely to win.")

        # too few pages read: our own default leads, and the one page we did read can only raise
        # it, never lower it. No ceiling — there is nothing credible to cap against.
        aim = max(house, measured or 0, sl.get("typical_length_words") or 0)
        detail = (f"only {sample} page(s) from this keyword's own SERP could be measured"
                  if sample else "no page from this keyword's own SERP could be measured")
        return aim, (
            f"Set `target_words` to **{aim} words**. NOTE: {detail}, so there is no reliable "
            f"format benchmark for this SERP — this figure is our standard depth for the "
            f"audience, not a measurement of the field. Treat the numbers above as evidence "
            f"about individual pages, not as a target or a ceiling; in particular do NOT shorten "
            f"the article towards them.")

    @staticmethod
    def _bench_block(b) -> str:
        if not b:
            return "(prototypes unreadable — use the SERP word counts)"
        return (f"- length: median {b.get('words_median')} words "
                f"(range {b.get('words_min')}-{b.get('words_max')}, n={b.get('sample')})\n"
                f"- headings: median {b.get('h2_median')} H2 / {b.get('h3_median')} H3 "
                f"(their H3 counts are inflated by per-product Pros/Cons sprawl — do NOT match that)\n"
                f"- images: median {b.get('images_median')} per article; kinds across the set: "
                f"{b.get('image_kinds')}\n"
                f"- tables: median {b.get('tables_median')} "
                f"({b.get('comparison_tables')} of {b.get('sample')} use at least one)\n"
                f"- video: median {b.get('videos_median')}\n"
                f"- FAQ: {b.get('with_faq')} of {b.get('sample')} ship one"
                + (f" (as {', '.join(b.get('faq_how') or [])})" if b.get("faq_how") else ""))

    def _render(self, state, data) -> str:
        L = [f"# Brief — {data.get('title', state.topic)}", ""]
        L.append(f"- **Search intent:** {data.get('search_intent', '—')}")
        L.append(f"- **Target length:** {data.get('target_words', state.target_words)} words")
        L.append(f"- **meta_title** ({len(data.get('meta_title') or '')} ch): {data.get('meta_title', '—')}")
        L.append(f"- **meta_description** ({len(data.get('meta_description') or '')} ch): "
                 f"{data.get('meta_description', '—')}")
        if data.get("mandatory_keywords"):
            L.append(f"- **Mandatory keywords (set by SEO):** {', '.join(data['mandatory_keywords'])}")
        if data.get("serp_notes"):
            L += ["", "## What the ranking pages cover", "", data["serp_notes"]]
        L += ["", "## Outline", ""]
        for s in data.get("sections", []):
            L.append(f"### {s.get('h2', '?')}" + (f"  _({s['words']} words)_" if s.get("words") else ""))
            L.append(f"- intent: {s.get('intent', '')}")
            if s.get("keywords"):
                L.append(f"- keywords: {mark_mandatory(s['keywords'], data.get('mandatory_keywords'))}")
            if s.get("content_types"):
                L.append(f"- content: {'; '.join(s['content_types'])}")
            for h3 in s.get("h3", []) or []:
                L.append(f"  - H3: {h3}")
            L.append("")
        if data.get("faq"):
            L += ["## FAQ", ""] + [f"- {q}" for q in data["faq"]] + [""]
        if data.get("research_questions"):
            L += ["## Research questions (-> Deep Researcher)", ""] + [f"- {q}" for q in data["research_questions"]] + [""]
        if data.get("_raw"):
            L += ["## Raw model output (JSON parse failed)", "", "```", data["_raw"][:4000], "```"]
        return "\n".join(L)
