"""Agent 4 — Copywriter.

Executes the product-aware SEO brief (Agent 2) using ONLY the cited facts from the Deep
Researcher (Agent 3). Writes Markdown in the brand voice, features the product exactly
where the brief says (honestly), cites every stat inline as a link to its source, hedges
low-confidence facts, and never asserts anything in the research "gaps". Writes the first
draft, `04_draft.md`; every later change is a targeted edit by the QA fixer.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .base import BaseAgent
from ..keyword_report import mark_mandatory
from ..tools.link_index import LinkIndex
from ..validators import (canonicalise_internal_links, dedupe_links,
                          strip_horizontal_rules)
from ..store import atomic_write_text


def _strip_fence(md: str) -> str:
    m = re.match(r"^\s*```(?:markdown|md)?\s*(.+?)```\s*$", md or "", re.DOTALL)
    return m.group(1).strip() if m else (md or "").strip()


class Copywriter(BaseAgent):
    name = "copywriter"
    # Writer defects are the expensive kind (each one costs fix passes at editor prices), so
    # the writer gets the top model (models.writer -> Opus 5; falls back to worker if unset).
    # Self-review correlation is held down by the QA panel being cross-vendor: the writer's
    # model is only one voice of three.
    model_role = "writer"

    def run(self, state):
        state.current_agent = "copywriter"
        out = Path(state.output_dir)
        user = self._draft_message(state)
        md = _strip_fence(self.call_claude(user, max_tokens=16000))  # long articles must not truncate mid-table
        # enforce "each URL linked once" here rather than at publish time, so the editor, the
        # Studio preview and WordPress all read the same text (the prompt rule alone does not
        # hold — a revision pass took one article from 24 to 33 links)
        md, dropped = dedupe_links(md)
        md, rules = strip_horizontal_rules(md)
        from ..validators import normalize_em_dashes

        md, dashes = normalize_em_dashes(md)
        if dashes:
            state.note(f"[copywriter] normalized {dashes} em dash(es) to commas — banned as "
                       f"sentence punctuation, and 'replace every em dash' is unfixable as an "
                       f"anchored swap downstream")
        idx_for_urls = LinkIndex.load(self.project.root)
        if idx_for_urls and idx_for_urls.targets:
            md, fixed_urls = canonicalise_internal_links(
                md, [t.url for t in idx_for_urls.targets])
            if fixed_urls:
                state.note(f"[copywriter] canonicalised {len(fixed_urls)} internal URL(s) that "
                           f"named a real page with the wrong scheme or host: "
                           + "; ".join(f"{b} -> {a}" for b, a in fixed_urls[:4]))
        if rules:
            state.note(f"[copywriter] stripped {rules} horizontal rule(s) — banned, and letting "
                       f"them through costs the Editor a required_fix")
        state.draft_markdown = md

        b = state.brief or {}
        state.frontmatter = {
            "title": b.get("title", state.topic),
            "meta_title": b.get("meta_title") or b.get("title", state.topic)[:70],
            "meta_description": (b.get("meta_description") or b.get("angle") or state.topic)[:160],
            "description": (b.get("meta_description") or b.get("angle") or state.topic)[:160],
            "slug": state.slug,
            "tags": [r["keyword"] for r in (state.keyword_research.get("related") or [])[:6]],
        }
        # FAQ ships as structured data (schema.org), never as body H3s
        state.frontmatter["faq"] = self._answer_faq(state, b.get("faq") or [])
        atomic_write_text(out / "04_draft.md", md)
        if dropped:
            state.note(f"[copywriter] unlinked {len(dropped)} repeated URL(s): "
                       + ", ".join(sorted(set(dropped))[:5]))
        links = md.count("](http")
        state.note(f"[copywriter] draft ~{len(md.split())} words, "
                   f"{links} cited links, faq={len(state.frontmatter.get('faq') or [])}")
        return state

    # ---- internal links: our own pages, matched to this article ----
    def _link_inventory(self, state) -> tuple[str, int]:
        """Pick the internal targets for THIS article. Returns (prompt block, how many to place).

        Deterministic keyword scoring against the sitemap-built index — no LLM call. The
        writer chooses which of the offered pages genuinely fit; it may never invent a URL.
        """
        cfg = self.project.links or {}
        target_words = state.target_words or (state.brief or {}).get("target_words") or 2000
        want = max(3, round(target_words / (cfg.get("words_per_link") or 350)))
        if not cfg:
            return "(no internal link inventory configured)", 0

        idx = LinkIndex.load(self.project.root)
        if idx is None or idx.stale(cfg.get("ttl_days", 7)):
            try:
                idx = LinkIndex.build(cfg, log=lambda m: state.note(m))
                idx.save(self.project.root)
            except Exception as e:  # noqa: BLE001 — never fail a run over the link index
                state.note(f"[copywriter] link index refresh failed ({e}); using what we have")
                idx = idx or LinkIndex.load(self.project.root)
        if idx is None or not idx.targets:
            state.note("[copywriter] NO internal link index — article will ship without internal links")
            return "(no internal link inventory available)", 0

        b = state.brief or {}
        query = " ".join(filter(None, [
            state.topic,
            (state.keyword_research or {}).get("primary_keyword", ""),
            b.get("title", ""), b.get("angle", ""),
            " ".join(s.get("h2", "") for s in b.get("sections", [])),
            " ".join(k for s in b.get("sections", []) for k in (s.get("keywords") or [])),
        ]))
        picked = idx.select(
            query,
            n=cfg.get("per_article", 12),
            min_product=cfg.get("min_product_links", 2),
            exclude_urls={state.frontmatter.get("canonical", "")} if state.frontmatter else None,
        )
        atomic_write_text(Path(state.output_dir, "_links.json"), json.dumps({"query_terms": query[:400], "want_placed": want,
                        "candidates": [t.to_dict() for t in picked]}, indent=2, ensure_ascii=False))
        state.note(f"[copywriter] internal link candidates: {len(picked)} "
                   f"({sum(1 for t in picked if t.is_product_page)} product pages), target ~{want} placed")

        lines = []
        for t in picked:
            tag = "PRODUCT PAGE" if t.is_product_page else f"blog/{t.category or t.kind}"
            desc = f" — {t.description[:130]}" if t.description else ""
            lines.append(f"- [{tag}] {t.title}{desc}\n  {t.url}")
        return "\n".join(lines), want

    def _answer_faq(self, state, questions: list) -> list:
        """Answer the brief's FAQ questions as plain structured data (for schema.org markup)."""
        if not questions:
            return []
        qs = "\n".join(f"- {q}" for q in questions[:10])
        # the WHOLE article: a 12 000-character slice hid 23-72% of the live articles, so an
        # answer about a tool reviewed in the second half had only memory to go on
        user = f"""Answer each FAQ question below in 2-4 plain sentences, based ONLY on the article
you just wrote and its researched facts. Plain text answers: no markdown, no links, no long
dashes. Confident, on-brand, factual.

If a question cannot be answered from the article and its facts, OMIT it from the JSON
entirely — never write "the article does not cover X", "this cannot be answered", or any
other sentence about the article or its sources: the reader sees these answers on the page
and has no idea what "the article's research" is. Talk about the subject or say nothing.

QUESTIONS:
{qs}

ARTICLE (for grounding):
{state.draft_markdown}

RESEARCHED FACTS (the only sourced material; a claim about a third party must come from here):
{self.render_corpus(state.research)}

Return ONLY JSON: {{"faq": [{{"question": "...", "answer": "..."}}]}}"""
        data, _ = self.call_claude_json(user, max_tokens=3000)
        faq = (data or {}).get("faq") or []
        # deterministic net over the instruction: an answer that talks about the writing
        # process is dropped, not published (the FAQ is customer-facing frontmatter; the
        # fixer's swaps cannot reach it, so a bad answer would be a permanent gate blocker —
        # measured on the live-captions run: 4 such answers, all "cannot be answered here")
        from ..validators import meta_commentary

        kept = []
        for item in faq:
            bad = meta_commentary(str(item.get("answer") or ""))
            if bad:
                state.note(f"[copywriter] dropped FAQ answer with process commentary: "
                           f"{str(item.get('question'))[:60]} — {bad[0][:80]}")
                continue
            kept.append(item)
        return kept

    # ---- facts the writer is allowed to use ----
    def _facts_block(self, state) -> str:
        # the shared view: the editor, the fixer's re-check and the FAQ answerer see these bytes
        return self.render_corpus(state.research)

    @staticmethod
    def _citation_target(state, target_words: int) -> tuple[int, int]:
        """(distinct sources available, how many to cite). Measured, not exhorted.

        Internal links have had a stated numeric target for a while; citations never did — the
        prompt only ever said "cite ONLY researched facts", a restriction rather than an
        obligation. With four more instruction blocks added around it, the obligation lost.
        Result: research supplied 27 distinct sources and the article cited 2, where the earlier
        hand-tuned version carried 15.
        """
        r = state.research or {}
        urls = {s_["source_url"] for s_ in (r.get("key_statistics") or []) if s_.get("source_url")}
        for f in (r.get("findings") or []):
            for fact in (f.get("facts") or []):
                if fact.get("source_url"):
                    urls.add(fact["source_url"])
        # the accepted articles ran about one citation per 250 words
        want = min(len(urls), max(4, round((target_words or 2000) / 250)))
        return len(urls), want

    def _keyword_contract(self, state) -> str:
        """The primary-keyword floor, in the writer's own brief.

        These thresholds live in `seo.keywords` and are enforced twice — by the coverage report
        and by the Editor, which raises the shortfall as a MAJOR. The writer was never shown
        them. Across five runs of `audio ai news` that Major was the ONLY blocking finding every
        single time ("1 of 3 required, missing from the opening and every H2"), while everything
        else was a Minor. Grading a writer against a numeric contract it has not been given is
        the third instance of the dilution problem, and the most costly, because it blocks.

        Read from config rather than restated here, so the number the writer is told is by
        construction the number it is judged against.
        """
        cfg = ((self.config.get("seo") or {}).get("keywords") or {})
        kr = state.keyword_research or {}
        primary = kr.get("primary_keyword") or ""
        if not primary:
            return ""
        total = cfg.get("primary_min_total", 3)
        heads = cfg.get("primary_in_headings_min", 1)
        first = cfg.get("primary_in_first_words", 100)
        dens_max = cfg.get("primary_density_max", 2.5)
        return f"""PRIMARY KEYWORD — `{primary}` — this is a COUNTED requirement, not a preference:
- at least {total} occurrences of that EXACT wording across the H1, the headings and the body;
- it must appear within the first {first} words of the body, not only in the H1;
- at least {heads} H2 or H3 must carry it;
- stay under {dens_max}% density — above that it reads as stuffing and is penalised.
Use the exact phrase even when a smoother word order exists: a permutation of it ('AI audio
news' for 'audio ai news') does NOT count, and the shortfall is measured in code, so it cannot
be argued away in prose."""

    def _visual_target(self, state, target_words: int) -> int:
        """How many body visuals this article should carry. Measured, not exhorted.

        The prompt said "target roughly ONE visual per 350 words" in prose and passed no number,
        while every other budget that matters — words, citations, internal links — arrives as a
        figure in the task message. One run then emitted ZERO visual markers, so the only image
        produced was the cover and the article shipped with nothing in the body: the same
        dilution that took citations from 15 to 2. The enrichment pass exists to top visuals up,
        not to supply all of them from scratch.

        The competitor median wins over our words-per-visual constant where it is measured, for
        the same reason it does in the visual director: image count is a format expectation of
        the SERP, not a function of our word count.
        """
        cfg = (self.config.get("pipeline") or {}).get("visuals") or {}
        per_words = int(cfg.get("words_per_visual") or 350)
        cap = int(cfg.get("max_per_article") or 10)
        want = max(3, round((target_words or 2000) / max(per_words, 1)))
        measured = ((state.keyword_research or {}).get("content_benchmark") or {}).get("images_median")
        if isinstance(measured, int) and measured > 0:
            want = max(want, measured)
        return min(cap, want)

    def _draft_message(self, state) -> str:
        b = state.brief or {}
        target = state.target_words or b.get("target_words") or 0

        sections = []
        for s in b.get("sections", []):
            line = f"- H2: {s.get('h2')}"
            if s.get("words"):
                line += f" [BUDGET: {s['words']} words]"
            line += f" | intent: {s.get('intent','')}"
            if s.get("keywords"):
                # the brief carries SEO priority; runs from before it have it only in keyword research
                mand = ((state.brief or {}).get("mandatory_keywords")
                        or (state.keyword_research or {}).get("mandatory_keywords") or [])
                line += f" | keywords: {mark_mandatory(s['keywords'], mand)}"
            if s.get("content_types"):
                line += f" | content: {'; '.join(s['content_types'])}"
            if s.get("h3"):
                line += f" | H3s: {', '.join(s['h3'])}"
            sections.append(line)
        outline = "\n".join(sections) or "(structure it sensibly from the brief)"
        faq = "\n".join(f"- {q}" for q in b.get("faq", [])) or "(none)"
        inventory, want_links = self._link_inventory(state)
        n_sources, want = self._citation_target(state, target)
        want_visuals = self._visual_target(state, target)
        keyword_contract = self._keyword_contract(state)
        lsi = ", ".join(x["keyword"] for x in ((state.keyword_research or {}).get("lsi") or [])[:40])

        return f"""Write the complete article in MARKDOWN, executing this brief exactly.

TITLE: {b.get('title', state.topic)}
SEARCH INTENT: {b.get('search_intent','')}
TARGET LENGTH: {target} words — this is a CONTRACT, not a suggestion. Landing within
10% of it is a hard requirement; the per-section budgets below add up to it.
AUDIENCE: {self.project.audience}

INTERNAL LINK INVENTORY — our own pages. Place ~{want_links} of these as inline links
where they genuinely help the reader, at least 2 of them PRODUCT PAGES. Use ONLY URLs
from this list (never invent one), each URL at most once, anchor text = a natural
descriptive phrase inside the sentence. Skip any page that does not truly fit:
{inventory}

WHAT THE RANKING PAGES COVER (from the SEO brief): {b.get('serp_notes') or '(not recorded)'}

OUTLINE (follow it exactly — do NOT add sections of your own; use the keywords naturally,
2-3 per section, no stuffing. Respect each section's word budget):
{outline}

FAQ — answered separately as structured data. Do NOT write an FAQ section, and do NOT
turn these into H3s. They are here only so the article's prose covers the ground they ask about:
{faq}

LSI VOCABULARY — terms the pages that already rank talk about. Weave the ones that genuinely
fit into your prose so the article reads as on-topic; never force one, never build a heading
from one, never list them:
{lsi or "(none)"}

RESEARCHED FACTS — you may ONLY cite statistics/claims from this list, linked inline to the source_url.
This is an OBLIGATION as well as a restriction: {n_sources} distinct sources were gathered for this
article and a piece of {target} words should carry roughly {want} of them as inline citations. An
article that cites two of twenty-seven available sources has wasted the research and reads as
unsupported, however clean its prose. Spread them across sections rather than clustering them:
{self._facts_block(state)}

{keyword_contract}

FAQ — every answer must ANSWER. If the research does not support an answer, drop the question
entirely; never write that the article, the research or the sources do not cover something. A
reader has no idea what our research corpus is, and "the article does not include specific
research on X" tells them only that we published a gap. Observed three times, twice in one
article.

VISUALS — this article must carry {want_visuals} visual markers in the BODY, on top of the one
cover marker. That number is what the ranking competitors carry, and it is a requirement, not an
aspiration: a draft with no markers ships with no images, because nothing downstream invents them
for you. Use `<!-- visual:chart data="..." caption="..." -->` wherever the researched statistics
above give three or more comparable numbers in the same unit, and
`<!-- visual:screenshot url="..." caption="..." -->` for a product you discuss in a SENTENCE (a
table row is not enough — the screenshot is anchored to the prose mention). Place each one under
the section it illustrates, never two in a row.

Now write the article. Follow the brand voice and product facts from your system prompt.
Start with the H1. Return MARKDOWN only."""
