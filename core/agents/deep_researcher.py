"""Agent 3 — Deep Researcher (parallel.ai Task API).

Runs ONE deep-research task for ALL of the brief's research questions (we pay per task
run, so never one-per-question), with a structured output schema. Returns a JSON of
findings — each question answered with CITED facts (source_url + as_of) — plus the
article's key statistics and a source list. This JSON travels with the brief to the
Copywriter, who may only cite facts that appear here.

Output: structured `state.research` + a readable `03_research.md`.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from .base import BaseAgent
from ..tools.parallel_client import ParallelClient
from ..validators import _ROUNDUP
from ..store import atomic_write_text

# --- the top-up (R3) -------------------------------------------------------------------------------
# The truth-check traced most false third-party claims to products the article was told to name
# (a section keyword, a roundup entry) that the one research run never looked up, so the writer
# filled the gap from memory. After the run, each such product gets ONE Search call, its own
# website first. An excerpt becomes a fact only when it literally carries the fact's numbers, and
# a product nothing reliable is found for gets a definitional-mention gap - never a dropped keyword.
_GENERIC = {"ai", "app", "apps", "audio", "voice", "tool", "tools", "software", "pro", "free", "online",
            "api", "the", "and", "for", "news", "podcast", "music", "speech", "text", "translation",
            "translator", "generator", "maker", "studio", "labs", "platform", "live", "real", "time"}
_NUM = re.compile(r"\d[\d,.]*")


def _product_tokens(name: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", (name or "").lower()) if len(t) >= 3 and t not in _GENERIC]


def _main_label(host: str) -> str:
    """The registrable domain's own label: docs.deepl.com -> deepl, bbc.co.uk -> bbc."""
    parts = [p for p in (host or "").lower().split(".") if p and p != "www"]
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "ac", "gov") and len(parts[-1]) == 2:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else (parts[0] if parts else "")


def _own_site(url: str | None, tokens: list[str]) -> bool:
    """Is this page on the product's own site? The registrable domain's main label must BE a
    product (or maker) token, or start with one of five or more characters: fish.audio for Fish
    Audio, minimaxi.com for MiniMax, docs.deepl.com for DeepL - but not play.google.com for
    Play.ht, zoominfo.com for Zoom, reviews.com for Rev.ai or metacritic.com for Meta AI."""
    main = _main_label(urlparse(url or "").hostname or "")
    return bool(main) and any(main == t or (len(t) >= 5 and main.startswith(t)) for t in tokens)


# 2. a fact's words, not only its numbers, must be on the page it cites
_STOP = {"that", "this", "with", "from", "their", "they", "them", "have", "been", "which", "into",
         "also", "more", "than", "such", "each", "about", "over", "only", "other", "when", "where",
         "what", "while", "your", "will", "within", "without", "across", "including", "offers",
         "provides", "supports", "includes", "allows", "lets", "using", "used", "uses", "makes"}


def _content_words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z]+", (text or "").lower()) if len(w) > 3 and w not in _STOP]


def _evidence(claim: str, src: str, value: str = "") -> str | None:
    """The excerpt sentence that carries the claim, or None when the page does not carry it:
    every capitalised word of the claim and at least 60% of its content words must occur in
    the cited page's excerpts. Shared numbers alone are not evidence for a capability.

    The sentence kept as evidence is the one holding the most of the fact's numbers (claim and
    value), then the most of its words: in the paid run a "$10 Standard plan" fact kept the
    free plan's "10 credits per day" line, a real page line that shows the wrong thing."""
    words = set(re.findall(r"[a-z0-9]+", (src or "").lower()))
    if any(c.lower() not in words for c in re.findall(r"\b[A-Z][A-Za-z0-9]*\b", claim)):
        return None
    cw = _content_words(claim)
    if cw and sum(w in words for w in cw) < 0.6 * len(cw):
        return None
    nums = [n.rstrip(".,") for n in _NUM.findall(f"{claim} {value}")]
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n", src or "") if s.strip()]

    def score(s: str) -> tuple[int, int]:
        return (sum(1 for n in nums if n and n in s),
                sum(w in set(re.findall(r"[a-z]+", s.lower())) for w in cw))

    best = max(sents, key=score, default="")
    return best[:300]


_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "summary": {"type": "string"},
                    "facts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "claim": {"type": "string"},
                                "value": {"type": "string"},
                                "source_url": {"type": "string"},
                                "as_of": {"type": "string"},
                            },
                            "required": ["claim", "source_url"],
                        },
                    },
                    "confidence": {"type": "string"},
                },
                "required": ["question", "summary"],
            },
        },
        "key_statistics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"stat": {"type": "string"}, "value": {"type": "string"}, "source_url": {"type": "string"}},
                "required": ["stat", "source_url"],
            },
        },
        "sources": {
            "type": "array",
            "items": {"type": "object", "properties": {"title": {"type": "string"}, "url": {"type": "string"}}},
        },
        "gaps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["findings", "key_statistics", "sources"],
}


class DeepResearcher(BaseAgent):
    name = "deep_researcher"

    def run(self, state):
        state.current_agent = "deep_researcher"
        out = Path(state.output_dir)
        brief = state.brief or {}
        rqs = brief.get("research_questions") or [f"key facts, statistics and comparisons about {state.topic}"]

        pc = ParallelClient()
        if not pc.configured:
            state.research = {"findings": [], "key_statistics": [], "sources": [], "note": "parallel.ai not configured"}
            atomic_write_text(out / "03_research.md", "# Deep research\n\n_parallel.ai not configured._\n")
            state.note("[research] parallel.ai not configured")
            return state

        # one product scan serves both: the in-scope products go into the objective (researched
        # as products, not only as their companies), and the top-up gets the whole list
        try:
            named = self._named_products(state)
        except Exception as e:  # noqa: BLE001 — a failed scan leaves the objective as it was
            named = []
            state.note(f"[research] product scan skipped: {e}")
        objective = self._objective(state, brief, rqs, [p for p in named if p.get("in_scope")])
        try:
            res = pc.deep_research(objective, _SCHEMA, processor=state.processor or "core")
            content = res.get("content") or {}
            content["_basis"] = res.get("basis")
            content["_run_id"] = res.get("run_id")
        except Exception as e:  # noqa: BLE001
            state.research = {"findings": [], "key_statistics": [], "sources": [], "error": str(e)}
            atomic_write_text(out / "03_research.md", f"# Deep research\n\n**ERROR:** {e}\n")
            state.note(f"[research] error: {e}")
            return state

        topup_max = int(((self.config or {}).get("pipeline") or {}).get("research_topup_max", 8) or 0)
        if topup_max:
            try:
                self._top_up(state, content, pc, topup_max, products=named)
            except Exception as e:  # noqa: BLE001 — never fail a paid research run over the top-up
                state.note(f"[research] top-up skipped: {e}")
        if named:
            content["_products"] = named   # product, maker, domain: the claim verifier reuses them
        state.research = content
        atomic_write_text(out / "03_research.md", self._render(state, content))
        nfacts = sum(len(f.get("facts", [])) for f in content.get("findings", []))
        state.note(
            f"[research] 1 task-run ({state.processor}) · findings={len(content.get('findings', []))} "
            f"facts={nfacts} key_stats={len(content.get('key_statistics', []))} sources={len(content.get('sources', []))}"
        )
        return state

    def _objective(self, state, brief, rqs, products: list[dict] | None = None) -> str:
        qs = "\n".join(f"{i+1}. {q}" for i, q in enumerate(rqs))
        kr = state.keyword_research or {}
        mand = list(brief.get("mandatory_keywords") or kr.get("mandatory_keywords") or [])
        extra = []
        if products:
            names = "\n".join(
                f"- {p['product']}" + (f" (by {p['maker']})" if p.get("maker")
                                       and p["maker"].lower() not in p["product"].lower() else "")
                for p in products)
            extra.append(f"""PRODUCTS IN SCOPE (the article names each of these; research each one as a product, not only
as its company):
{names}
For each product, report as a finding of its own: its current price and free tier; the languages it
supports; its key capabilities; its integrations; the compliance programs or certifications that
cover THAT product (not only its parent company); notable partners and customers. When you cannot
find one of these, write 'not found' in that finding's summary and leave it out of the facts - a
search that finds nothing is not evidence that the product lacks it; never state that the product
does not offer it.""")
        if mand:
            extra.append(f"TERMS SEO REQUIRES THE ARTICLE TO COVER: {', '.join(mand)}")
        extra.append("""Treat every question as open: when a question names an example or a case, check it as one
possibility, never assume it. Beyond dry facts, also bring back concrete examples, surprising
findings and vivid details a reader would remember, each with its source.""")
        return f"""Research task for an article titled "{brief.get('title', state.topic)}".

Answer EACH of the following questions with SPECIFIC, CURRENT (prefer 2024-2026), CITED facts and statistics.
For every fact include a real source_url and, when possible, an as_of date. Prefer authoritative sources
(industry reports, government/regulatory bodies, peer-reviewed studies, primary vendor docs). If a question
cannot be answered from credible sources, say so in that finding's summary — DO NOT invent numbers.

Also extract the handful of "key_statistics" that would headline the article, and list your top sources.

QUESTIONS:
{qs}

""" + "\n\n".join(extra)

    # ---- the top-up (R3) ----
    def _named_products(self, state) -> list[dict]:
        """The products the article's keywords and roundup entries name, in term order, ours
        excluded. `in_scope` marks the ones the objective researches by name: roundup entries,
        SEO-mandatory keywords and section keywords - not a brand that only appears in the LSI
        or related lists ("real time" and "languages" never reach the classifier's answer)."""
        terms = self._topup_terms(state)
        if not terms:
            return []
        own = (self.project.name or "").strip().lower()
        rank = {t.lower(): i for i, t in enumerate(terms)}
        scope = self._scope_terms(state)
        products = [p for p in self._classify_products(terms) if p["product"].lower() != own]
        if not products:
            state.note(f"[research] the product scan found no products in {len(terms)} terms")
        for p in products:
            # a product named by a section keyword is in scope even when the classifier answered
            # with its secondary-keyword spelling first; it ranks by its best-placed keyword
            ts = p.get("terms") or [p["term"]]
            p["term"] = min(ts, key=lambda t: rank.get(t.lower(), len(rank)))
            p["in_scope"] = any(" ".join(t.lower().split()) in scope for t in ts)
        return sorted(products, key=lambda p: rank.get(p["term"].lower(), len(rank)))

    @staticmethod
    def _scope_terms(state) -> set[str]:
        kr = state.keyword_research or {}
        b = state.brief or {}
        terms = list(kr.get("mandatory_keywords") or []) + list(b.get("mandatory_keywords") or [])
        terms += [k for s in (b.get("sections") or []) for k in (s.get("keywords") or [])]
        for s in b.get("sections") or []:
            if _ROUNDUP.search(str(s.get("h2") or "")):
                for h in s.get("h3") or []:
                    name = re.sub(r"^\s*\d+[.)]\s*", "", str(h))
                    terms.append(re.split(r"\s*(?::|—|–|\s-\s)\s*", name)[0])
        return {" ".join(str(t).lower().split()) for t in terms if t}

    def _top_up(self, state, content: dict, pc, limit: int, products: list[dict] | None = None) -> None:
        """Facts for the products the article must name and the research has none about.

        Mutates `content`: a finding per product that got facts (marked `topup`), a gap per
        product that did not. `products` is the scan `run()` already made; without it the
        scan runs here.
        """
        products = self._named_products(state) if products is None else products
        if not products:
            return
        missing = [p for p in products if not self._has_facts(content, p["product"])]
        got, gaps = [], []
        for p in missing[:limit]:
            pages = self._vendor_pages(state, pc, p)
            facts = self._facts_from_pages(p, pages) if pages else []
            if facts:
                content.setdefault("findings", []).append({
                    "question": f"{p['product']}: what it is, its price and key capabilities "
                                f"(top-up for the keyword '{p['term']}')",
                    "summary": f"Facts from {p['product']}'s own pages.",
                    "confidence": "medium", "facts": facts, "topup": True})
                got.append(p["product"])
            else:
                content.setdefault("gaps", []).append(
                    f"{p['product']}: no sourced facts found on its own site - mention it only by what "
                    f"it is and who makes it; no prices, features or other claims.")
                gaps.append(p["product"])
        state.note(f"[research] top-up: {len(products)} product(s) named, {len(missing)} without facts; "
                   f"facts added for {got or 'none'}; definitional only: {gaps or 'none'}"
                   + (f"; {len(missing) - limit} over the limit of {limit}" if len(missing) > limit else ""))

    @staticmethod
    def _topup_terms(state) -> list[str]:
        """Every keyword the article may use plus the brief's roundup entries, once each."""
        kr = state.keyword_research or {}
        b = state.brief or {}
        # the products the article reviews come first, so a limit never cuts them before a
        # brand that only appears in the LSI list
        terms = []
        for s in b.get("sections") or []:
            if _ROUNDUP.search(str(s.get("h2") or "")):
                for h in s.get("h3") or []:
                    name = re.sub(r"^\s*\d+[.)]\s*", "", str(h))
                    terms.append(re.split(r"\s*(?::|—|–|\s-\s)\s*", name)[0])
        terms += list(kr.get("mandatory_keywords") or [])
        terms += list((kr.get("analysis") or {}).get("secondary_keywords") or [])
        terms += [k for s in (b.get("sections") or []) for k in (s.get("keywords") or [])]
        terms += [r.get("keyword") for r in (kr.get("related") or []) if isinstance(r, dict)]
        terms += [r.get("keyword") for r in (kr.get("lsi") or []) if isinstance(r, dict)]
        out, seen = [], set()
        for t in terms:
            key = " ".join(str(t or "").lower().split())
            if key and key not in seen:
                seen.add(key)
                out.append(str(t).strip())
        return out[:250]

    def _classify_products(self, terms: list[str]) -> list[dict]:
        listed = "\n".join(f"- {t}" for t in terms)
        prompt = f"""Below are search keywords and headings planned for one article. Pick out the ones that name
a specific product, company, brand or tool (for example "notion ai", "hubspot", "canva"),
not a generic category, feature or phrase ("ai writing assistant", "free plan", "integrations").

For each, give the product's usual written name, the company that makes it, and, only if you
are sure of it, its official website domain.

TERMS:
{listed}

Return ONLY JSON: {{"products": [{{"term": "...", "product": "...", "maker": "...", "domain": "example.com or empty"}}]}}"""
        data, _ = self.call_claude_json(prompt, max_tokens=8000)
        out, at = [], {}
        for p in (data or {}).get("products") or []:
            name = " ".join(str(p.get("product") or "").split())
            if not name or not _product_tokens(name):
                continue
            term = str(p.get("term") or name)
            if name.lower() in at:              # another keyword naming the same product
                out[at[name.lower()]]["terms"].append(term)
                continue
            at[name.lower()] = len(out)
            dom = re.sub(r"^(https?://)?(www\.)?", "", str(p.get("domain") or "").strip().lower()).split("/")[0]
            out.append({"term": term, "terms": [term], "product": name,
                        "maker": " ".join(str(p.get("maker") or "").split()), "domain": dom})
        return out

    def _has_facts(self, content: dict, product: str) -> bool:
        name = re.escape(product.lower())
        toks = _product_tokens(product)
        for f in self.citable_facts(content)[0]:
            text = f"{f.get('claim') or ''} {f.get('value') or ''}".lower()
            if re.search(rf"(?<![a-z0-9]){name}(?![a-z0-9])", text) or _own_site(f.get("source_url"), toks):
                return True
        return False

    @staticmethod
    def _vendor_pages(state, pc, p: dict) -> list[dict]:
        """Pages of the product's own site with excerpts: its domain first, then the open web
        filtered to its own site. Anything else is not the vendor speaking for itself."""
        toks = list(dict.fromkeys(_product_tokens(p["product"]) + _product_tokens(p.get("maker") or "")))
        objective = (f"{p['product']}: what it is, who makes it, its current pricing and free tier, "
                     f"and its key features, from its official website")
        queries = [f"{p['product']} pricing", f"{p['product']} features", f"what is {p['product']}"]
        tries = ([[p["domain"]]] if p.get("domain") else []) + [None]
        for inc in tries:
            try:
                hits = pc.search(objective, queries, max_results=6, include_domains=inc)
            except Exception as e:  # noqa: BLE001 — one failed search costs one product
                state.note(f"[research] top-up search failed for {p['product']}: {str(e)[:120]}")
                continue
            own = [h for h in hits if _own_site(h.get("url"), toks) and h.get("excerpts")]
            if own:
                return own
        return []

    def _facts_from_pages(self, p: dict, pages: list[dict]) -> list[dict]:
        """Facts extracted from the excerpts, kept only when the page they cite carries them: every
        number literally, and the claim's words (see `_evidence`) - no auto-citation without
        containment. Each kept fact carries the excerpt sentence that supports it."""
        blob = "\n\n".join(f"SOURCE {h['url']}\n" + "\n".join(h["excerpts"])[:3000] for h in pages[:4])
        prompt = f"""These are excerpts from {p['product']}'s own web pages. Extract up to 5 facts a writer may state
about {p['product']}: what it is and who makes it, its price or free tier, and its key capabilities.
Copy numbers, plan names and product names exactly as the excerpt writes them. Only what an
excerpt states outright: nothing from memory, nothing inferred.

{blob}

Return ONLY JSON: {{"facts": [{{"claim": "one sentence", "value": "short value", "source_url": "the SOURCE url"}}]}}"""
        data, _ = self.call_claude_json(prompt, max_tokens=4000)
        by_url = {h["url"]: " ".join(h["excerpts"]) for h in pages[:4]}
        kept = []
        for f in (data or {}).get("facts") or []:
            src = by_url.get(f.get("source_url"))
            claim = " ".join(str(f.get("claim") or "").split())
            if not src or not claim:
                continue
            value = " ".join(str(f.get("value") or "").split())
            nums = [n.rstrip(".,") for n in _NUM.findall(f"{claim} {value}")]
            if any(n and n not in src for n in nums):
                continue
            ev = _evidence(claim, src, value)
            if ev is None:
                continue
            kept.append({"claim": claim, "value": value, "source_url": f["source_url"],
                         "as_of": date.today().isoformat(), "excerpt": ev})
        return kept

    def _render(self, state, c) -> str:
        L = [f"# Deep research — {state.topic}", "",
             f"_One parallel.ai task-run (processor={state.processor}, id={c.get('_run_id')}). "
             f"findings={len(c.get('findings', []))}, key stats={len(c.get('key_statistics', []))}, sources={len(c.get('sources', []))}._", ""]
        ks = c.get("key_statistics") or []
        if ks:
            L += ["## Key statistics (article headline numbers)", ""]
            for s in ks:
                L.append(f"- **{s.get('stat')}** — {s.get('value','')} · [source]({s.get('source_url')})")
            L.append("")
        L += ["## Findings by question", ""]
        for f in c.get("findings", []):
            L.append(f"### {f.get('question')}")
            L.append(f"{f.get('summary','')}  _(confidence: {f.get('confidence','—')})_")
            for fact in f.get("facts", []) or []:
                val = f" — {fact['value']}" if fact.get("value") else ""
                as_of = f" _(as of {fact['as_of']})_" if fact.get("as_of") else ""
                L.append(f"- {fact.get('claim','')}{val} · [source]({fact.get('source_url')}){as_of}")
            L.append("")
        if c.get("gaps"):
            L += ["## Gaps (unanswered)", ""] + [f"- {g}" for g in c["gaps"]] + [""]
        srcs = c.get("sources") or []
        if srcs:
            L += ["## Sources", ""] + [f"- [{s.get('title') or s.get('url')}]({s.get('url')})" for s in srcs] + [""]
        return "\n".join(L)
