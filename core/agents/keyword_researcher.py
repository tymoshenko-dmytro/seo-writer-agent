"""Agent 1 — Keyword Researcher (SEO).

Produces an analytical, brief-ready research file — not a keyword dump. The flow mirrors
the SEO team's own prototype workflow (2026-08-21 review):

  1. Strategist (LLM): infer article intent; propose candidate primary keywords and the
     "best/comparison" keywords whose SERP is worth studying.
  2. Ahrefs: candidate metrics; a WIDE keyword pool (matching terms for the head term and
     the strongest candidate + `also_rank_for` siblings); search suggestions.
  3. LSI vocabulary from `also_talk_about` — the terms the ranking pages MENTION. Kept
     separate from the keyword pool: it is vocabulary to weave into prose, never sections.
  4. SERP: probe each candidate cheaply (no billable columns) and keep the most
     ARTICLE-rich; buy the competitiveness metrics for that one SERP only.
  5. PROTOTYPES: pick three competitor articles the way the SEO team does — the top-ranked
     one as the benchmark, plus the two with the FEWEST direct links to the page (referring
     domains, then backlinks), i.e. the realistic targets. Tear all three down in full
     (length, images by kind, tables and their headers, video, FAQ, H* structure, meta tags,
     and which content types sit under which heading), and record the template junk.
  6. Content benchmark computed IN CODE from the prototypes (medians) — target length,
     how many images/tables/videos the format actually needs. Models count badly.
  7. Questions merged from Ahrefs questions + suggestions + competitor question headings.
  8. Analyst (LLM): choose the primary keyword from real keywords, cluster the pool, read
     the SERP landscape and the gap, and draft the meta tags.
  9. Meta tags validated in CODE against the length spec, with one repair pass.

Writes `01_keyword_research.md`.
"""
from __future__ import annotations

import re
import statistics
from datetime import date
from pathlib import Path

from .base import BaseAgent
from ..tools.ahrefs_client import AhrefsClient
from ..tools import scrapfly
from ..tools.page_audit import audit_page
from ..validators import validate_meta
from ..store import atomic_write_text

_QWORDS = ("how", "what", "why", "when", "where", "who", "which", "can", "does", "do", "is",
           "are", "should", "will", "vs")

SEO_DEFAULTS = {
    "country": "us", "serp_top": 20, "audit_max_pages": 8,
    "prototypes_min": 3, "prototypes_max": 7, "prototype_max_position": 10,
    "prototype_min_h2": 2, "prototype_min_words": 700, "prototype_borrow_keywords": 6,
    "lsi": {"also_talk_about": 60, "also_rank_for": 40, "keep": 40},
    "meta": {"title_min": 50, "title_max": 70, "description_min": 130, "description_max": 150},
}

# Ahrefs' AI-predicted page_type is far better than guessing from the URL, but it is null for
# a good share of results, so the URL heuristic stays as the fallback.
_PAGE_TYPE_MAP = {
    "article": "article", "listing": "store listing", "commerce": "product page",
    "product": "product page", "forum": "forum", "qa": "forum", "video": "video",
    "documentation": "docs", "home": "homepage", "organization": "page", "people": "page",
}


def _url_type(u: str) -> str:
    ul = (u or "").lower()
    if "apps.apple.com" in ul or "play.google.com" in ul:
        return "store listing"
    if "youtube.com" in ul or "youtu.be" in ul or "vimeo.com" in ul:
        return "video"
    if "reddit.com" in ul or "quora.com" in ul:
        return "forum"
    if any(x in ul for x in ["/blog", "/guide", "best-", "/post/", "medium.com", "/articles/", "/tips"]):
        return "article"
    return "page"


def result_kind(url: str, page_type: str | None = None) -> str:
    if page_type:
        top = str(page_type).split(",")[0].strip().lstrip("/").split("/")[0].lower()
        if top:
            return _PAGE_TYPE_MAP.get(top, top.replace("_", " "))
    return _url_type(url)


def _is_question(text: str) -> bool:
    t = re.sub(r"\s+", " ", (text or "")).strip().lower()
    if not t:
        return False
    if "?" in t:
        return True
    return t.split()[0] in _QWORDS and len(t.split()) >= 4


# ---- LSI filtering -----------------------------------------------------------------
# `also_talk_about` is noisy by nature: it returns whatever the ranking pages say a lot,
# which includes the target keyword's own words and editorial filler.
_FILLER = {"bottom line", "everyday use", "key takeaways", "final thoughts", "learn more",
           "read more", "in conclusion", "things to consider", "our verdict", "get started",
           "frequently asked questions", "table of contents", "pros cons", "sign up"}
_GENERIC_SINGLES = {"free", "new", "good", "great", "easy", "fast", "top", "best", "cheap",
                    "app", "apps", "tool", "tools", "online", "review", "reviews"}


def _stem(tok: str) -> str:
    return tok.rstrip("s")[:6]


def filter_lsi(terms, target_phrases, keep: int = 40) -> list[dict]:
    """Keep the LSI terms that add vocabulary we do not already have.

    Drops (a) terms whose every word is already in the target keyword — "translate",
    "voice", "best translation app" tell us nothing new; (b) editorial filler; (c) bare
    generic adjectives. Crude stemming, because "translator"/"translation"/"translate" must
    collapse to one thing.
    """
    target = {_stem(t) for phrase in target_phrases
              for t in re.findall(r"[a-z0-9]+", (phrase or "").lower())}
    out, seen = [], set()
    for t in terms:
        kw = (t.get("keyword") or "").strip().lower()
        if not kw or kw in seen or kw in _FILLER or len(kw) < 4:
            continue
        toks = re.findall(r"[a-z0-9]+", kw)
        if not toks:
            continue
        if len(toks) == 1 and kw in _GENERIC_SINGLES:
            continue
        if all(_stem(x) in target for x in toks):
            continue          # nothing new — it is the target keyword restated
        seen.add(kw)
        out.append({"keyword": kw, "volume": t.get("volume"), "difficulty": t.get("difficulty")})
        if len(out) >= keep:
            break
    return out


# ---- prototype qualification --------------------------------------------------------
# A prototype has to be the KIND OF PAGE WE ARE WRITING. Ahrefs' page_type says "/Article" for
# things that are not articles at all, and one run set its length target from these:
#
#   en.wikipedia.org/wiki/Medical_translation        625 words, 1 H2
#   dev.hume.ai/docs/voice/voice-cloning             486 words, 0 H2
#   platform.minimax.io/docs/guides/speech-voice     966 words, 0 H2
#   ssrs.com/news/ai-audio-could-take-over-radio     390 words, 0 H2
#
# API documentation, an encyclopaedia entry and a news brief. Anchoring length to them cut three
# articles roughly in half, and because the visual and internal-link budgets are derived from
# word count, images and links halved with it. Zero H2s is the giveaway: a page with no
# subheadings is not a structured guide, whatever the classifier says.
_NOT_A_PROTOTYPE = re.compile(
    r"""(?ix)
    ^https?://[^/]*(
        wikipedia\.org | wikimedia\.org | \.gov(/|$) |
        (^|\.)(dev|docs|developer|developers|platform|api|support|help)\.
    )
    | /(docs?|documentation|reference|api|api-reference|changelog|pricing|
        legal|terms|privacy|about|contact)(/|$)
    """)


def excluded_as_prototype(url: str) -> str | None:
    """Cheap pre-audit exclusion by URL. Returns the reason, or None when the page may qualify."""
    return "not an article format (docs, reference or encyclopaedia)" \
        if _NOT_A_PROTOTYPE.search(url or "") else None


def qualifies_as_prototype(audit: dict, min_h2: int = 2, min_words: int = 700) -> str | None:
    """Post-audit qualification. Returns the reason for rejection, or None when it qualifies."""
    if not audit or audit.get("error") and not audit.get("words"):
        return "could not be read"
    words, h2 = audit.get("words") or 0, audit.get("h2") or 0
    if h2 < min_h2:
        return f"only {h2} H2 heading(s) — not a structured article"
    if words < min_words:
        return f"{words} words — too thin to set a format from"
    return None


# ---- prototype selection ------------------------------------------------------------
_BIG = 10 ** 9


def _topical_words(must_contain) -> list[set[str]]:
    """`must_contain` as word sets, because it holds PHRASES and a phrase is a bad substring.

    Measured on the topic `audio ai news`: the strategist returned must_contain=['audio ai'],
    and not one keyword in the pool contains that literal string — the words are all there but
    reordered ('ai audio tools', 'audio ai tools', 'ai news anchor'). A substring test therefore
    matched nothing, the borrow pool came out EMPTY, and the step that exists to rescue a thin
    SERP never ran on the one SERP in the set that needed rescuing.

    So a keyword is on-topic when it carries every WORD of at least one entry, in any order.
    """
    out = []
    for entry in must_contain or []:
        words = {w for w in re.findall(r"[a-z0-9]+", str(entry).lower()) if w}
        if words:
            out.append(words)
    return out


def is_topical(keyword: str, must: list[set[str]]) -> bool:
    if not must:
        return True
    low = {w for w in re.findall(r"[a-z0-9]+", (keyword or "").lower()) if w}
    return any(words <= low for words in must)


def pick_prototypes(organic, n_min: int = 3, n_max: int = 7,
                    max_position: int = 10) -> tuple[list[dict], str | None]:
    """Pick the competitors whose CONTENT is doing the ranking.

    The SEO team's note reads "choose the top 3 articles with the fewest links, but always
    include the #1 article". A first implementation took that literally and sorted by fewest
    referring domains — which selects for WEAKNESS. It picked a page sitting at position 15 on
    five referring domains, a thin page nobody competes with, and anchoring our length to that
    set collapsed three articles from ~3 500 words to ~2 000 and cost them their QA scores.

    The founder's reading is the right one: a page that climbs high WITHOUT a strong link profile
    is ranking on its content, and that is what makes it worth tearing down. So the score is
    rank against link weight — high position, few donors — and anything outside the top
    `max_position` is excluded, because a page nobody competes for teaches nothing.

    The top-ranked article is always included as the benchmark, as the team asked.
    """
    articles = [o for o in organic
                if o.get("kind") == "article" and (o.get("position") or 99) <= max_position]
    if not articles:
        if not organic:
            return [], ("Ahrefs returned NO SERP data for this keyword — this means missing "
                        "data, not an empty market. Plan the format from search intent, the "
                        "keyword pool and the LSI vocabulary instead")
        kinds = ", ".join(dict.fromkeys(o.get("kind") or "page" for o in organic))
        return [], (f"no article-type results inside the top {max_position} — this SERP returns "
                    f"{kinds}, so there is no listicle to prototype against")

    by_rank = sorted(articles, key=lambda o: (o.get("position") or 99))
    picks = [dict(by_rank[0], role="benchmark",
                  why=f"highest-ranking article in the SERP (position {by_rank[0].get('position')})")]
    taken = {by_rank[0]["url"]}

    def punch(o) -> float:
        """How far the page ranks above what its link profile would justify."""
        pos = o.get("position") or max_position
        rd = o.get("refdomains")
        rd = rd if isinstance(rd, int) else None
        if rd is None:
            return -1.0                      # unmeasured: not evidence of anything
        return (max_position + 1 - pos) / (rd + 1)

    ranked = sorted((a for a in articles if a["url"] not in taken), key=punch, reverse=True)
    # Only pages COMPARABLE to the best content-carried example join the set. Taking everything
    # up to n_max would pad it with pages that are not punching above their weight at all — a
    # position-10 page on 55 referring domains scores 0.02 against a leader's 1.0 — and padding
    # with mediocre pages is how the length anchor got dragged down in the first place.
    best_punch = punch(ranked[0]) if ranked else 0.0
    cutoff = best_punch * 0.25
    for o in ranked:
        if len(picks) >= n_max:
            break
        if len(picks) >= n_min and (punch(o) < 0 or punch(o) < cutoff):
            break
        rd, pos = o.get("refdomains"), o.get("position")
        # Only claim the content is carrying the page when the link profile was actually
        # measured. Cheap probes omit the billable columns, so `refdomains` is None for every
        # borrowed SERP; the old wording asserted "the content is carrying it" on all of them
        # and the brief had no way to know the ordering had quietly fallen back to raw position.
        why = (f"ranks {pos} on only {rd} referring domain(s) — the content is carrying it"
               if isinstance(rd, int)
               else f"ranks {pos}; link profile not measured, so it is included on rank alone")
        picks.append(dict(o, role="alternative", why=why))
        taken.add(o["url"])
    note = None if len(picks) >= n_min else (
        f"only {len(picks)} article competitor(s) inside the top {max_position}")
    return picks, note


def _median_int(values):
    vals = [v for v in values if isinstance(v, int) and v > 0]
    return int(statistics.median(vals)) if vals else None


def content_benchmark(audits) -> dict:
    """What this SERP's format actually requires — measured, not estimated.

    Computed from the prototype teardowns in code because the target length and the visual
    budget are contracts downstream, and a model asked to average five numbers will not.
    """
    ok = [a for a in audits if (a.get("words") or 0) > 0]
    if not ok:
        return {}
    words = sorted(a["words"] for a in ok)
    faq = [a for a in ok if (a.get("faq") or {}).get("present")]
    return {
        "sample": len(ok),
        "words_min": min(words), "words_median": _median_int(words), "words_max": max(words),
        # The AIM is the upper part of the field, not the middle: the founder's call is to take
        # the format from the pages that win, and the median of a set that includes thin pages
        # drags the target down. The maximum stays the ceiling.
        "words_target": words[int(len(words) * 0.75)] if words else None,
        "h2_median": _median_int([a.get("h2") for a in ok]),
        "h3_median": _median_int([a.get("h3") for a in ok]),
        "images_median": _median_int([(a.get("images") or {}).get("total") for a in ok]),
        "tables_median": _median_int([len(a.get("tables") or []) for a in ok]),
        "videos_median": _median_int([a.get("videos") for a in ok]),
        "with_faq": len(faq),
        "faq_how": list(dict.fromkeys(f["faq"]["how"] for f in faq if f["faq"].get("how"))),
        "comparison_tables": sum(1 for a in ok if len(a.get("tables") or []) >= 1),
        "image_kinds": _merge_kinds(ok),
    }


def _merge_kinds(audits) -> dict:
    out: dict[str, int] = {}
    for a in audits:
        for k, v in ((a.get("images") or {}).get("kinds") or {}).items():
            out[k] = out.get(k, 0) + v
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


class KeywordResearcher(BaseAgent):
    name = "keyword_researcher"

    # ---- config ----
    def _seo(self) -> dict:
        cfg = dict(SEO_DEFAULTS)
        cfg.update(self.config.get("seo") or {})
        cfg.update(getattr(self.project, "seo", None) or {})
        for k in ("lsi", "meta"):
            merged = dict(SEO_DEFAULTS[k])
            merged.update((self.config.get("seo") or {}).get(k) or {})
            merged.update((getattr(self.project, "seo", None) or {}).get(k) or {})
            cfg[k] = merged
        return cfg

    def run(self, state):
        state.current_agent = "keyword_researcher"
        out = Path(state.output_dir)
        if not state.use_ahrefs:
            atomic_write_text(out / "01_keyword_research.md", "# Keyword research\n\n_Skipped (use_ahrefs=False)._\n")
            state.note("[keyword] skipped (use_ahrefs=False)")
            return state

        cfg = self._seo()
        country = cfg["country"]
        client = AhrefsClient()
        plan = self._strategist(state.topic)
        head = plan.get("head_term") or state.topic
        # The operator's own phrasing is ALWAYS a candidate. They typed it because they
        # chose it — usually off their own keyword pass — and the strategist inventing four
        # neighbouring phrasings must not be able to drop it. On the "zoom translation" run
        # it proposed only variants ("zoom translation feature", "zoom translation app", …),
        # so the 1 100-volume term the operator actually asked for was never even priced and
        # the page got anchored to a 100-volume synonym of it.
        candidates = list(dict.fromkeys(
            [str(state.topic).strip()]
            + [str(c).strip() for c in (plan.get("primary_candidates") or []) if str(c).strip()]
            + ([str(head).strip()] if head else [])))[:5]

        cand_metrics = []
        for kw in candidates:
            m, err = client.get_keyword_metrics(kw, country=country)
            cand_metrics.append(
                {"keyword": kw, "volume": getattr(m, "search_volume", None),
                 "kd": getattr(m, "keyword_difficulty", None), "cpc": getattr(m, "cpc", None),
                 "parent_topic": getattr(m, "parent_topic", None)} if m
                else {"keyword": kw, "volume": None, "kd": None, "error": err}
            )

        related_list, pool_sources = self._keyword_pool(
            client, head, cand_metrics, country, cfg["lsi"]["also_rank_for"])
        # An empty pool is a TRANSPORT failure, not a topic with no demand, and it has to
        # be loud: the Ahrefs client swallows its own errors into an empty list, so for a
        # while every run picked its primary keyword out of the strategist's four invented
        # variants with no volume behind any of them, and the only trace was a "Pool: 0
        # keywords" line in a report nobody re-reads. Whoever sees this note should not
        # trust the run's targeting.
        if not related_list:
            state.note(f"[keywords] WARNING: the Ahrefs keyword pool came back EMPTY "
                       f"({pool_sources}) — the primary keyword is being chosen with no "
                       f"volume data behind it. Check the Ahrefs transport before trusting "
                       f"this run's keyword targeting.")

        # questions: Ahrefs questions (+parent) + search suggestions + competitor headings
        questions, _ = client.get_questions(head, country=country, limit=20)
        parent = next((c.get("parent_topic") for c in cand_metrics
                       if c["keyword"] == head and c.get("parent_topic")), None)
        if parent and parent.lower() != head.lower():
            pq, _ = client.get_questions(parent, country=country, limit=15)
            questions += [q for q in pq if q not in set(questions)]
        q_ahrefs = list(dict.fromkeys(list(questions)
                                      + [r["keyword"] for r in related_list if _is_question(r["keyword"])]))
        suggestions, _ = client.search_suggestions(head, country=country, limit=25)
        q_suggest = [s["keyword"] for s in suggestions if _is_question(s["keyword"])]

        serp_kw, det, attempts, serr = self._study_serp(client, plan, candidates, related_list,
                                                        country, cfg, state.topic)
        organic = det["organic"]
        for o in organic:
            o["kind"] = result_kind(o["url"], o.get("page_type"))

        # ---- prototypes: pick, audit, qualify, and widen if too few survive ----
        cfg["_serp_keyword"] = serp_kw
        protos, rejected, audits, proto_note = self._gather_prototypes(
            client, organic, related_list, plan, country, cfg, log=state.note)
        for o in organic:
            a = audits.get(o["url"])
            if a:
                o["words"] = a.get("words")

        # Format from every prototype; LENGTH from the primary SERP only. Borrowed pages come
        # from a NEIGHBOURING query, and the note on the borrow step says why that is allowed:
        # the format of a good article is stable across close queries, but what an article
        # COVERS is not — and length is a function of coverage. Measured on `audio ai news`: two
        # of four prototypes were borrowed from 'ai audio enhancer' and 'ai audio news', tool
        # pages whose length has nothing to say about a news round-up, and the resulting 1 683
        # word ceiling was the single Major blocking an otherwise 82-point article.
        bench = content_benchmark([p["audit"] for p in protos if p.get("audit")])
        own = [p["audit"] for p in protos if p.get("audit") and not p.get("borrowed")]
        length_bench = content_benchmark(own)
        for k in ("sample", "words_min", "words_median", "words_max", "words_target"):
            bench[f"length_{k}"] = length_bench.get(k)
        q_headings = list(dict.fromkeys(
            [h["text"].rstrip(":") for a in audits.values() for h in (a.get("outline") or [])
             if _is_question(h["text"])]
            + [q for a in audits.values() for q in ((a.get("faq") or {}).get("question_headings") or [])]
        ))
        all_questions = list(dict.fromkeys(q_ahrefs + q_suggest + q_headings))

        lsi = self._lsi(client, head, cand_metrics, country, cfg)

        analysis = self._analyst(state.topic, plan, cand_metrics, related_list, all_questions,
                                 serp_kw, organic, protos, bench, det, lsi)
        primary = analysis.get("primary_keyword") or head
        pstats = (next((c for c in cand_metrics if c["keyword"] == primary), None)
                  or next((r for r in related_list if r["keyword"] == primary), None))
        clusters = self._cluster_metrics(analysis.get("clusters", []), related_list)
        meta = self._finalise_meta(analysis, primary, cfg["meta"], bench)

        # the SERP's own numbers set the length contract, not the model's impression of them
        if bench.get("words_target"):
            analysis.setdefault("serp_landscape", {})["typical_length_words"] = bench["words_target"]

        state.keyword_research = {
            "topic": state.topic, "country": country, "plan": plan, "candidates": cand_metrics,
            "primary_keyword": primary, "primary_stats": pstats, "analysis": analysis,
            "meta": meta,
            # keywords SEO has decided the article must carry, beyond the primary (project-wide
            # from seo.keywords.mandatory today; editable per article once Studio pauses before
            # the brief). Everything else stays desired.
            "mandatory_keywords": list(dict.fromkeys(
                k for k in ((self.project.seo or {}).get("keywords") or {}).get("mandatory") or []
                if k and str(k).strip().lower() != str(primary).strip().lower())),
            "related": related_list, "pool_sources": pool_sources, "clusters_ranked": clusters,
            "lsi": lsi,
            "questions": all_questions,
            "question_sources": {"ahrefs": len(q_ahrefs), "suggestions": len(q_suggest),
                                 "competitor_headings": len(q_headings)},
            "serp_keyword": serp_kw, "serp_paa_count": det.get("paa_count", 0),
            "serp_features": det.get("features", {}),
            "serp_feature_only": det.get("feature_only", []),
            "serp_error": serr,
            "serp_attempts": attempts,
            "serp_organic": organic,
            "prototypes": protos, "prototype_note": proto_note,
            "prototypes_rejected": rejected,
            "competitor_audits": list(audits.values()),
            "content_benchmark": bench,
        }
        atomic_write_text(out / "01_keyword_research.md", self._format(state))
        state.note(
            f"[keyword] primary='{primary}' pool={len(related_list)}{pool_sources} "
            f"lsi={len(lsi)} questions={len(all_questions)} "
            f"serp='{serp_kw}' organic={len(organic)} features={det.get('features')} "
            f"prototypes={len(protos)}({proto_note or 'ok'}) "
            f"bench_words={bench.get('words_median')} clusters={len(clusters)} "
            f"meta={meta.get('status')}"
        )
        return state

    # ---- prototypes ----
    def _gather_prototypes(self, client, organic, related_list, plan, country, cfg,
                           log=None):
        """Pick, audit, QUALIFY, and widen the search if too few pages survive.

        Qualification has to happen after the audit, because the disqualifying facts — no
        subheadings, too thin — are only visible once the page is read. So this is a loop rather
        than a selection: rank candidates, read one, keep it or record why not, and stop when
        enough have qualified or the audit budget runs out.

        When the primary SERP cannot supply enough, it borrows from the SERPs of other
        high-volume keywords in the pool. Borrowed pages count for MEASUREMENT ONLY — the format
        of a good article is stable across closely related queries, but what an article COVERS is
        not, and taking the outline from another query's winners drifts off intent. They are
        tagged so the brief can tell them apart.
        """
        min_h2, min_words = cfg["prototype_min_h2"], cfg["prototype_min_words"]
        budget = cfg["audit_max_pages"]
        primary_serp = cfg.get("_serp_keyword") or ""
        audits: dict[str, dict] = {}
        rejected: list[dict] = []
        qualified: list[dict] = []

        def consider(cands, serp: str, borrowed: bool):
            nonlocal budget
            for o in cands:
                if len(qualified) >= cfg["prototypes_max"] or budget <= 0:
                    return
                url = o.get("url")
                if not url or url in audits:
                    continue
                why = excluded_as_prototype(url)
                if why:
                    rejected.append({"url": url, "why": why, "serp": serp, "read": False})
                    continue
                budget -= 1
                # One line per page read. Reading up to `audit_max_pages` competitors through a
                # real browser is the slowest thing in the pipeline and it used to print nothing
                # until it finished: a run sat for 13 minutes at 0% CPU inside this loop, which
                # is indistinguishable from the hang we actually had once. Progress here is the
                # difference between "working" and "stuck".
                if log:
                    log(f"[keyword] reading competitor {len(audits) + 1} "
                        f"(budget {budget} left): {url}")
                a = audit_page(url, deep=True, use_playwright=True,
                               remote=scrapfly.remote(self.config, country))
                audits[url] = a
                why = qualifies_as_prototype(a, min_h2, min_words)
                if log:
                    log(f"[keyword]   -> {a.get('words') or 0} words, {a.get('h2') or 0} H2 — "
                        + (f"rejected: {why}" if why else "qualifies as a format prototype"))
                if why:
                    rejected.append({"url": url, "why": why, "serp": serp, "read": True,
                                     "words": a.get("words"), "h2": a.get("h2")})
                    continue
                qualified.append(dict(o, audit=a, serp=serp, borrowed=borrowed))

        # 1. the primary SERP, in punch order (highest rank for the fewest links first)
        ranked, note = pick_prototypes(organic, n_min=cfg["prototypes_min"],
                                       n_max=len(organic) or 1,
                                       max_position=cfg["prototype_max_position"])
        consider(ranked, primary_serp, borrowed=False)

        # 2. borrow, for measurement only, from the SERPs of other high-volume pool keywords
        must = _topical_words(plan.get("must_contain"))
        borrowed_from: list[dict] = []
        if len(qualified) < cfg["prototypes_min"] and budget > 0:
            borrow_from = [r["keyword"] for r in related_list
                           if r.get("keyword") and r["keyword"] != primary_serp
                           and is_topical(r["keyword"], must)
                           ][: cfg["prototype_borrow_keywords"]]
            for kw in borrow_from:
                if len(qualified) >= cfg["prototypes_min"] or budget <= 0:
                    break
                pr, _ = client.serp_probe(kw, country=country, top=cfg["serp_top"])
                cands = [dict(o, kind=result_kind(o["url"], o.get("page_type")))
                         for o in pr["organic"]]
                # Ranked by the same standard as the primary SERP rather than by raw position:
                # a borrowed page is only worth reading if IT is punching above its link weight
                # too, and the position cap has to apply here as well.
                picks, why_not = pick_prototypes(cands, n_min=cfg["prototypes_min"],
                                                 n_max=len(cands) or 1,
                                                 max_position=cfg["prototype_max_position"])
                # Only pages we have not already read count as something borrowing DELIVERED.
                # A neighbouring SERP usually overlaps the primary one, and the duplicates are
                # skipped silently inside consider() — reporting them made the rescue look more
                # productive than it was ("offered 1 further candidate" when that candidate was
                # a page already in the set).
                fresh = [o for o in picks if o.get("url") not in audits]
                borrowed_from.append({"keyword": kw, "candidates": len(fresh),
                                      "duplicates": len(picks) - len(fresh), "note": why_not})
                if picks:
                    self_note = f"borrowed from the SERP for '{kw}'"
                    consider([dict(o, why=self_note, role="alternative") for o in picks],
                             kw, borrowed=True)

        # roles: the benchmark is the top-ranked qualified page from the PRIMARY serp
        primary_q = [q for q in qualified if not q["borrowed"]]
        if primary_q:
            top = min(primary_q, key=lambda o: (o.get("position") or 99))
            for q in qualified:
                q["role"] = "alternative"
            top["role"] = "benchmark"
            top["why"] = f"highest-ranking qualified article (position {top.get('position')})"

        if len(qualified) >= cfg["prototypes_min"] and borrowed_from and note:
            # The primary-SERP note still says how thin the primary SERP was, which reads as a
            # weak-benchmark warning once borrowing has actually filled the set. Say that it was
            # filled, so a downstream reader does not discount a benchmark that is now sound.
            note = (f"{note}; filled to {len(qualified)} by borrowing from "
                    + ", ".join(repr(b["keyword"]) for b in borrowed_from if b["candidates"]))
        if len(qualified) < cfg["prototypes_min"]:
            # Say what the RESCUE did too. The first version of this note reported only the read
            # count, so a run in which borrowing silently never fired was indistinguishable from
            # one in which it fired and found nothing — and that is exactly the bug it hid.
            if not borrowed_from:
                widen = ("no on-topic keyword in the pool to borrow a SERP from"
                         if len(qualified) < cfg["prototypes_min"] else "")
            else:
                found = sum(b["candidates"] for b in borrowed_from)
                widen = (f"widened to {len(borrowed_from)} neighbouring SERP(s) "
                         f"({', '.join(repr(b['keyword']) for b in borrowed_from)}) "
                         f"which offered {found} further candidate(s)")
            read_out = sum(1 for r in rejected if r.get("read"))
            skipped = len(rejected) - read_out
            note = (f"only {len(qualified)} page(s) qualified as a format prototype out of "
                    f"{len(audits)} read"
                    + (f" ({read_out} rejected after reading)" if read_out else "")
                    + (f"; {skipped} skipped unread as the wrong kind of page" if skipped else "")
                    + (f"; {widen}" if widen else "")
                    + ". The format benchmark is weak; length should not be scored against it")
        return qualified, rejected, audits, note

    # ---- Ahrefs gathering ----
    def _keyword_pool(self, client, head, cand_metrics, country, lsi_limit: int = 40):
        """A WIDER pool than one matching-terms call.

        The SEO feedback was that the cluster is too narrow for this niche, so the pool is
        built from three angles: matching terms for the head term, matching terms for the
        strongest candidate (a different seed surfaces different long tails), and the
        `also_rank_for` siblings of the pages that already rank.
        """
        pool: dict[str, dict] = {}
        sources: dict[str, int] = {}

        def add(items, label):
            added = 0
            for r in items:
                kw = (r["keyword"] if isinstance(r, dict) else r.keyword) or ""
                key = kw.strip().lower()
                if not key or key in pool:
                    continue
                pool[key] = {
                    "keyword": kw,
                    "volume": r.get("volume") if isinstance(r, dict) else r.search_volume,
                    "difficulty": r.get("difficulty") if isinstance(r, dict) else r.difficulty,
                    "cpc": (r.get("cpc") if isinstance(r, dict) else r.cpc) or 0.0,
                    "source": label,
                }
                added += 1
            sources[label] = added

        rel, _ = client.get_related_keywords(head, country=country, limit=60)
        add(rel, "matching:head")

        best = max((c for c in cand_metrics if isinstance(c.get("volume"), int)),
                   key=lambda c: c["volume"], default=None)
        if best and best["keyword"].strip().lower() != (head or "").strip().lower():
            rel2, _ = client.get_related_keywords(best["keyword"], country=country, limit=40)
            add(rel2, "matching:candidate")

        sib, _ = client.related_terms(head, country=country, terms="also_rank_for",
                                      limit=lsi_limit)
        add([s for s in sib if (s.get("volume") or 0) > 0], "also_rank_for")

        return sorted(pool.values(), key=lambda r: -(r.get("volume") or 0)), sources

    def _lsi(self, client, head, cand_metrics, country, cfg):
        """LSI vocabulary — what the ranking pages talk about, filtered down to what's new."""
        raw, err = client.related_terms(head, country=country, terms="also_talk_about",
                                        limit=cfg["lsi"]["also_talk_about"])
        if err:
            return []
        targets = [head] + [c["keyword"] for c in cand_metrics]
        return filter_lsi(raw, targets, keep=cfg["lsi"]["keep"])

    def _study_serp(self, client, plan, candidates, related_list, country, cfg, topic=""):
        """Probe candidate SERPs cheaply, then buy the metrics for the winner only.

        serp-overview costs ~5 units/row without the metric columns and ~35 with them, so
        probing five candidates and paying once beats paying five times.
        """
        must = [w.lower() for w in (plan.get("must_contain") or []) if w]

        def on_topic(kw: str) -> bool:
            return (not must) or any(m in (kw or "").lower() for m in must)

        tries = (plan.get("serp_candidates") or candidates) + [
            r["keyword"] for r in related_list if r["keyword"].lower().startswith("best ")][:3]
        tries = [k for k in dict.fromkeys(tries) if k and on_topic(k)][:5]
        if not tries:  # must_contain filtered everything out -> relax
            tries = [k for k in dict.fromkeys(plan.get("serp_candidates") or candidates) if k][:5]

        # ALWAYS probe the plain head term and the topic as well, not just as a last resort.
        # The strategist prefixes its candidates with "best …", and Ahrefs frequently has no SERP
        # data for those longer commercial phrasings while it does have data for the plain term.
        # Measured: "best healthcare translation services" -> 0 organic, "healthcare translation"
        # -> 9 organic with 3 articles; "best ai voice generator" -> 18 but "audio ai news" -> 16.
        # Two articles in one regeneration run were written with NO competitor benchmark at all
        # because only the "best …" phrasings were tried. A probe carries no billable columns, so
        # the extra two calls are nearly free and the picker keeps whichever SERP is richest.
        tries += [k for k in (plan.get("head_term"), topic)
                  if k and k not in tries]

        probes, attempts = {}, []

        def probe(kw):
            pr, _ = client.serp_probe(kw, country=country, top=cfg["serp_top"])
            arts = sum(1 for o in pr["organic"]
                       if result_kind(o["url"], o.get("page_type")) == "article")
            probes[kw] = pr
            attempts.append({"keyword": kw, "results": len(pr["organic"]), "articles": arts,
                             "features": pr["features"]})

        for kw in tries:
            probe(kw)

        # Every candidate came back empty. Before concluding there is no SERP, try the plain
        # head term and the topic itself: the strategist invents different `serp_candidates`
        # on every run, and the same topic has returned 18 organic results on one run and 0 on
        # the next purely because of which phrasings it happened to propose. A probe carries no
        # billable columns, so this costs almost nothing and it is the difference between having
        # a measured competitor benchmark and the brief guessing a target with no evidence.
        if not any(a["results"] for a in attempts):
            for extra in dict.fromkeys(x for x in (plan.get("head_term"), topic) if x):
                if extra not in probes:
                    probe(extra)
                    if probes[extra]["organic"]:
                        break

        attempts.sort(key=lambda a: (a["articles"], a["results"]), reverse=True)

        win = attempts[0] if attempts and attempts[0]["results"] > 0 else None
        serp_kw = win["keyword"] if win else (tries[0] if tries else "")
        if not win:
            return serp_kw, {"organic": [], "features": {}, "paa_count": 0, "feature_only": []}, attempts, \
                "no candidate keyword returned SERP results"
        det, err = client.serp_details(serp_kw, country=country, top=cfg["serp_top"],
                                      probe=probes[serp_kw])
        return serp_kw, det, attempts, err

    # ---- meta ----
    def _finalise_meta(self, analysis, primary, spec, bench) -> dict:
        """Draft meta tags, then hold them to the character spec in code (see ensure_meta)."""
        h1 = (analysis.get("h1_suggestion") or "").strip()
        res = self.ensure_meta({"meta_title": analysis.get("meta_title"),
                                "meta_description": analysis.get("meta_description")},
                               primary, h1, spec)
        problems, rounds = res.pop("problems"), res.pop("rounds")
        res.update(
            h1_suggestion=h1,
            title_len=len(res["meta_title"]), description_len=len(res["meta_description"]),
            problems=problems, repair_rounds=rounds,
            status="ok" if not problems
                   else f"{len(problems)} issue(s) after {rounds} repair round(s)",
            spec={**self.META_SPEC, **(spec or {})},
        )
        return res

    # ---- clusters ----
    def _cluster_metrics(self, clusters, related_list):
        idx = {r["keyword"].lower(): r for r in related_list}
        out = []
        for cl in clusters:
            if isinstance(cl, str):
                # the analyst occasionally emits a bare theme string instead of the object —
                # measured crashing a whole run; a themeless cluster is still a valid row
                cl = {"theme": cl, "keywords": []}
            elif not isinstance(cl, dict):
                continue
            kws = [k for k in (cl.get("keywords") or []) if isinstance(k, str)]
            vols = [idx[k.lower()]["volume"] for k in kws
                    if k.lower() in idx and isinstance(idx[k.lower()].get("volume"), int)]
            kds = [idx[k.lower()]["difficulty"] for k in kws
                   if k.lower() in idx and isinstance(idx[k.lower()].get("difficulty"), int)]
            out.append({
                "theme": cl.get("theme", "cluster"), "keywords": kws, "count": len(kws),
                "total_volume": sum(vols), "avg_kd": round(sum(kds) / len(kds)) if kds else None,
            })
        out.sort(key=lambda c: c["total_volume"], reverse=True)
        return out

    # ---- LLM ----
    def _strategist(self, topic: str) -> dict:
        prompt = (
            f'Article topic/title: "{topic}".\n\nPlan keyword research. Return JSON:\n'
            '{"article_intent":"kind of article",'
            '"must_contain":["1-2 ESSENTIAL distinctive words the article is about so research stays on-topic, e.g. for an article about invoicing software use [\\"invoice\\"]"],'
            '"head_term":"SHORT head keyword (2-3 words) for related terms+questions",'
            '"primary_candidates":["3-4 SHORT real candidate primary keywords, each containing a must_contain word"],'
            '"serp_candidates":["2-4 SHORT \'best/comparison\' keywords whose SERP returns real listicle ARTICLES — each MUST contain a must_contain word so the SERP stays on-topic"]}\n'
            "Keep keywords short (Ahrefs lacks SERP data for long phrases). Return ONLY JSON."
        )
        data, _ = self.call_claude_json(prompt, system="You are an SEO strategist. Return only JSON.",
                                       max_tokens=400)
        return data if isinstance(data, dict) else {}

    def _analyst(self, topic, plan, cand_metrics, related, questions, serp_kw, organic,
                 protos, bench, det, lsi) -> dict:
        year = date.today().year
        serp_block = "\n".join(
            f"- #{o.get('position')} [{o.get('kind')}] DR {o.get('dr')} · UR {o.get('ur')} · "
            f"{o.get('refdomains')} ref.domains / {o.get('backlinks')} backlinks · "
            f"{o.get('words') or '?'}w · {o.get('title') or o['url']}"
            for o in organic) or "(no organic SERP results)"
        feat = det.get("features") or {}
        feat_block = ", ".join(f"{k}×{v}" for k, v in sorted(feat.items(), key=lambda kv: -kv[1])) or "(none)"
        proto_block = self._prototype_prompt_block(protos)
        if not organic:
            proto_block += (
                "\n\n!! Ahrefs returned NO SERP rows for any candidate keyword. That means "
                "Ahrefs HAS NO DATA for these queries (common for newer or lower-volume "
                "phrasings), NOT that Google shows no results and NOT that the topic is "
                "uncontested. Do NOT describe the SERP as empty, uncontested, a blue ocean or "
                "a first-mover opportunity, and do NOT claim zero competitiveness — say the "
                "SERP data is unavailable and base competitiveness on the keyword difficulty "
                "scores instead. Plan the format from the search intent, the keyword pool and "
                "the LSI vocabulary.")
        bench_block = self._bench_prompt_block(bench)
        cand_block = "\n".join(f"- {c['keyword']}: vol {c.get('volume')}, KD {c.get('kd')}"
                               for c in cand_metrics)
        rel_block = "\n".join(f"- {r['keyword']} (vol {r['volume']}, KD {r['difficulty']})"
                              for r in related[:70])
        lsi_block = ", ".join(x["keyword"] for x in lsi) or "(none)"
        q_block = "\n".join(f"- {q}" for q in questions[:40]) or "(none)"

        prompt = f"""TOPIC/TITLE: {topic}
ARTICLE INTENT: {plan.get('article_intent')}
SERP studied: "{serp_kw}"   ·   TODAY: {date.today().isoformat()} (the current year is {year})

CANDIDATE PRIMARY KEYWORDS:
{cand_block}

SERP ORGANIC RESULTS (DR = domain strength; ref.domains/backlinks are PAGE-level — how many
sites link to that specific article, which is what makes it beatable or not):
{serp_block}

SERP FEATURES PRESENT: {feat_block}

PROTOTYPE COMPETITORS — the three we are actually competing with, torn down:
{proto_block}

MEASURED FORMAT BENCHMARK (computed from those prototypes — use these numbers, do not re-estimate):
{bench_block}

KEYWORD POOL:
{rel_block}

LSI VOCABULARY (terms the ranking pages MENTION — vocabulary for the prose, NOT section ideas):
{lsi_block}

QUESTIONS (Ahrefs + suggestions + competitor headings):
{q_block}

== TASK == Return keyword STRATEGY as JSON:
{{
  "primary_keyword": "MUST be one of the candidate/pool keywords above verbatim (real, with volume) — favour commercial/listicle intent with a WINNABLE KD (look at the prototypes' DR and page-level link counts) over a high-KD head term. Do NOT invent a phrase.",
  "primary_rationale": "2-3 sentences citing volume/KD and what the prototypes' DR and referring-domain counts say about whether we can take them",
  "alternatives": [{{"keyword":"...","volume":0,"kd":0,"why_not":"..."}}],
  "search_intent": "informational|commercial|transactional",
  "h1_suggestion": "H1, MAX 10 words, contains the primary keyword. If a year belongs in it, the year is {year}",
  "meta_title": "title tag, 50-70 CHARACTERS, contains the primary keyword, NOT identical to the H1. Count the characters. Any year is {year}",
  "meta_description": "130-150 CHARACTERS, contains the primary keyword, ends on a value hook. Count the characters",
  "meta_notes": "one sentence on how these differ from the competitors' meta tags shown above",
  "serp_landscape": {{"dominant_content_type":"...","article_competitors":0,"typical_length_words":0,"competitiveness":"read the DR AND the page-level referring domains — a DR 80 page with 7 referring domains is beatable","what_they_cover":"concrete, from the prototype outlines","content_gap":"what they miss that we win on"}},
  "clusters": [{{"theme":"e.g. best & comparison / by use case / free tools / informational / pricing / by platform","keywords":["..."]}}],
  "recommended_sections": ["ordered H2 outline for OUR article, informed by the prototypes + the gap. Merge overlapping ground; do NOT mirror a competitor's H3 sprawl; never include an FAQ section"],
  "content_plan": ["what content TYPES the article needs to match the format, tied to sections — e.g. 'comparison table in the roundup section', 'screenshot per reviewed app', 'pricing chart'. Ground this in the measured benchmark above"],
  "secondary_keywords": ["highest-value secondary targets"],
  "strategy": "2-3 sentences: angle + which keywords win where"
}}
Cluster EVERY pool keyword into exactly one theme. Return ONLY JSON."""
        # system=None -> the agent's own prompt file (core/prompts/keyword_researcher.md).
        # It used to pass a one-line system string here, which silently bypassed that file and
        # left every rule in it dead.
        data, _ = self.call_claude_json(prompt, max_tokens=6000)
        return data if isinstance(data, dict) else {
            "primary_keyword": plan.get("head_term"), "clusters": [], "_parse_failed": True}

    # ---- prompt/report blocks ----
    @staticmethod
    def _prototype_prompt_block(protos) -> str:
        if not protos:
            return "(no article competitors in this SERP)"
        L = []
        for p in protos:
            a = p.get("audit") or {}
            imgs = a.get("images") or {}
            tables = a.get("tables") or []
            faq = a.get("faq") or {}
            L.append(
                f"### [{p['role'].upper()}] {(a.get('meta_title') or p.get('title') or p['url'])[:90]}\n"
                f"  {p['url']}\n"
                f"  picked because: {p.get('why')}\n"
                f"  DR {p.get('dr')} · UR {p.get('ur')} · {p.get('refdomains')} referring domains / "
                f"{p.get('backlinks')} backlinks · est. traffic {p.get('traffic')}\n"
                f"  LENGTH {a.get('words')} words · {a.get('h2')} H2 / {a.get('h3')} H3\n"
                f"  meta_title ({a.get('meta_title_len')} ch): {a.get('meta_title')}\n"
                f"  meta_description ({a.get('meta_description_len')} ch): {a.get('meta_description')}\n"
                f"  images {imgs.get('total', 0)} {imgs.get('kinds') or ''} · tables {len(tables)} "
                f"· video {a.get('videos')} · lists {a.get('lists')}\n"
                f"  table headers: " + (" ; ".join(", ".join(t['headers'][:6]) for t in tables[:3]) or "(none)") + "\n"
                f"  FAQ: {faq.get('how') or 'none'}"
                + (f" ({faq.get('count')} questions)" if faq.get("present") else "") + "\n"
                f"  schema: {', '.join(a.get('schema_types') or []) or '(none)'}\n"
                f"  template junk cut from the outline: {', '.join(a.get('template_junk') or []) or '(none)'}\n"
                f"  OUTLINE (words, and which content types sit inside each heading):\n"
                + "\n".join(
                    f"    {'  ' if h['lvl'] != 'h2' else ''}{h['lvl']} [{h['words']}w"
                    + (", " + ", ".join(f"{k}×{v}" for k, v in h['has'].items()) if h.get("has") else "")
                    + f"] {h['text']}"
                    for h in (a.get("outline") or [])[:26]) or "    (outline unavailable)"
            )
        return "\n".join(L)

    @staticmethod
    def _bench_prompt_block(b) -> str:
        if not b:
            return "(prototype pages could not be read — fall back to the SERP word counts)"
        return (f"- length: AIM {b.get('words_target')} words — the upper part of the field, not "
                f"the middle (median {b.get('words_median')}, range {b.get('words_min')}-"
                f"{b.get('words_max')}, n={b.get('sample')}). Ceiling is {b.get('words_max')}\n"
                f"- headings: median {b.get('h2_median')} H2 / {b.get('h3_median')} H3\n"
                f"- images: median {b.get('images_median')} per article; kinds seen across the set: "
                f"{b.get('image_kinds')}\n"
                f"- tables: median {b.get('tables_median')} "
                f"({b.get('comparison_tables')} of {b.get('sample')} prototypes use at least one)\n"
                f"- video: median {b.get('videos_median')}\n"
                f"- FAQ: {b.get('with_faq')} of {b.get('sample')} ship one"
                + (f" (as {', '.join(b.get('faq_how') or [])})" if b.get("faq_how") else ""))

    # ---- file ----
    def _format(self, state) -> str:
        kr = state.keyword_research
        a = kr.get("analysis", {})
        ps = kr.get("primary_stats") or {}
        ps_kd = ps.get("kd", ps.get("difficulty"))
        meta = kr.get("meta", {})
        bench = kr.get("content_benchmark", {})
        L = [f"# Keyword research — {state.topic}", "",
             f"**Article intent:** {kr.get('plan', {}).get('article_intent', '—')} · "
             f"**market:** {kr.get('country', '—')} · **run:** {date.today().isoformat()}", ""]

        L += ["## Chosen primary keyword", "", f"### `{kr.get('primary_keyword')}`"]
        L.append(f"- **Volume / KD:** {ps.get('volume', '—')} / {ps_kd if ps_kd is not None else '—'}")
        L.append(f"- **Search intent:** {a.get('search_intent', '—')}")
        L.append(f"- **Why this one:** {a.get('primary_rationale', '—')}")
        if a.get("alternatives"):
            L += ["", "**Alternatives considered:**", "", "| Keyword | Vol | KD | Why not primary |",
                  "|---|---|---|---|"]
            for alt in a["alternatives"]:
                L.append(f"| {alt.get('keyword')} | {alt.get('volume')} | {alt.get('kd')} | "
                         f"{alt.get('why_not', '')} |")

        # ---- meta tags ----
        L += ["", "## Meta tags", "",
              f"- **H1 (max 10 words):** {meta.get('h1_suggestion') or a.get('h1_suggestion') or '—'}",
              f"- **meta_title** ({meta.get('title_len', '?')} ch, spec "
              f"{meta.get('spec', {}).get('title_min')}-{meta.get('spec', {}).get('title_max')}): "
              f"{meta.get('meta_title') or '—'}",
              f"- **meta_description** ({meta.get('description_len', '?')} ch, spec "
              f"{meta.get('spec', {}).get('description_min')}-{meta.get('spec', {}).get('description_max')}): "
              f"{meta.get('meta_description') or '—'}"]
        if a.get("meta_notes"):
            L.append(f"- **vs competitors:** {a['meta_notes']}")
        if meta.get("problems"):
            L += ["", "> ⚠️ meta still off spec after the repair pass:"] + \
                 [f"> - {p}" for p in meta["problems"]]
        comp_meta = [p for p in kr.get("prototypes", []) if (p.get("audit") or {}).get("meta_title")]
        if comp_meta:
            L += ["", "**Competitor meta tags (what we are competing against in the SERP):**", "",
                  "| Role | Title (len) | Description (len) |", "|---|---|---|"]
            for p in comp_meta:
                au = p["audit"]
                L.append(f"| {p['role']} | {au.get('meta_title', '')[:70]} "
                         f"({au.get('meta_title_len')}) | "
                         f"{(au.get('meta_description') or '')[:80]} ({au.get('meta_description_len')}) |")

        # ---- SERP landscape ----
        sl = a.get("serp_landscape", {})
        feats = kr.get("serp_features") or {}
        L += ["", f"## SERP landscape — studied `{kr.get('serp_keyword')}`", ""]
        if kr.get("serp_attempts"):
            L.append("_Keywords probed: " + ", ".join(
                f"{x['keyword']} ({x['articles']} art / {x['results']} organic)"
                for x in kr["serp_attempts"]) + "_")
        L.append(f"- **SERP features:** " + (", ".join(f"{k} ×{v}" for k, v in
                 sorted(feats.items(), key=lambda kv: -kv[1])) or "none")
                 + f" · PAA blocks: {kr.get('serp_paa_count')}")
        L.append(f"- **Dominant type:** {sl.get('dominant_content_type', '—')} · "
                 f"**article competitors:** {sl.get('article_competitors', '—')} · "
                 f"**typical length:** {sl.get('typical_length_words', '—')}w")
        L.append(f"- **Competitiveness:** {sl.get('competitiveness', '—')}")
        L.append(f"- **What they cover:** {sl.get('what_they_cover', '—')}")
        L.append(f"- **Content gap (our opening):** {sl.get('content_gap', '—')}")
        if kr.get("serp_error"):
            L.append(f"- ⚠️ SERP warning: {kr['serp_error']}")

        L += ["", "**Organic results — DR is the domain, ref.domains/backlinks are THIS page "
              "(how many sites actually link to the article):**", "",
              "| # | Type | DR | UR | Ref.domains | Backlinks | Traffic | Words | Page |",
              "|---|---|---|---|---|---|---|---|---|"]
        for o in kr.get("serp_organic", []):
            title = (o.get("title") or o["url"])[:56]
            L.append(f"| {o.get('position', '—')} | {o.get('kind')} | {o.get('dr', '—')} | "
                     f"{o.get('ur', '—')} | {o.get('refdomains', '—')} | {o.get('backlinks', '—')} | "
                     f"{o.get('traffic', '—')} | {o.get('words', '—')} | [{title}]({o['url']}) |")
        if kr.get("serp_feature_only"):
            L += ["", "_Pages Google surfaces only through a SERP feature (AI overview, PAA) — "
                  "real competitors for the query, no organic metrics:_", ""]
            for f in kr["serp_feature_only"][:8]:
                L.append(f"- [{', '.join(f['types'])}] [{(f.get('title') or f['url'])[:70]}]({f['url']})")

        # ---- prototypes ----
        L += ["", "## Prototype competitors (the 3 we plan against)", "",
              "_Selection rule: the top-ranked article is the benchmark; the other two are the "
              "articles with the FEWEST direct links to the page, i.e. the realistic targets._", ""]
        if kr.get("prototype_note"):
            L += [f"> ⚠️ {kr['prototype_note']}", ""]
        L += ["| Role | DR | Ref.dom | Backlinks | Words | H2/H3 | Images | Tables | Video | FAQ | Page |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
        for p in kr.get("prototypes", []):
            au = p.get("audit") or {}
            faq = au.get("faq") or {}
            L.append(f"| {p['role']} | {p.get('dr', '—')} | {p.get('refdomains', '—')} | "
                     f"{p.get('backlinks', '—')} | {au.get('words', '—')} | "
                     f"{au.get('h2', '—')}/{au.get('h3', '—')} | "
                     f"{(au.get('images') or {}).get('total', '—')} | {len(au.get('tables') or [])} | "
                     f"{au.get('videos', '—')} | {faq.get('how') or '—'} | "
                     f"[{(p.get('title') or p['url'])[:44]}]({p['url']}) |")
        L.append("")
        for p in kr.get("prototypes", []):
            au = p.get("audit") or {}
            L.append(f"### [{p['role']}] [{(p.get('title') or p['url'])[:80]}]({p['url']})")
            L.append(f"_Picked because: {p.get('why')}. Fetched with {au.get('fetched_with', '?')}._")
            imgs = au.get("images") or {}
            if imgs.get("kinds"):
                L.append(f"- **Images:** {imgs.get('total')} — "
                         + ", ".join(f"{k} ×{v}" for k, v in imgs["kinds"].items()))
            for t in (au.get("tables") or [])[:4]:
                L.append(f"- **Table** ({t['rows']} rows): " + ", ".join(t["headers"][:8]))
            if au.get("schema_types"):
                L.append(f"- **Schema:** {', '.join(au['schema_types'])}")
            if au.get("template_junk"):
                L.append(f"- **Template junk cut (do NOT copy these into our outline):** "
                         + "; ".join(au["template_junk"]))
            if au.get("year_in_title"):
                L.append(f"- **Year in title:** {au['year_in_title']}")
            if au.get("error"):
                L.append(f"- ⚠️ fetch issue: {au['error']}")
            L += ["", "| H | Words | Content inside | Heading |", "|---|---|---|---|"]
            for h in (au.get("outline") or [])[:30]:
                has = ", ".join(f"{k} ×{v}" for k, v in (h.get("has") or {}).items()) or "—"
                L.append(f"| {h['lvl']} | {h['words']} | {has} | {h['text'][:70]} |")
            L.append("")

        # ---- benchmark ----
        if bench:
            L += ["## Format benchmark (measured from the prototypes, not estimated)", "",
                  f"- **Length:** median **{bench.get('words_median')}** words "
                  f"(range {bench.get('words_min')}–{bench.get('words_max')}, n={bench.get('sample')})"
                  " ← this is the target-length input for the brief",
                  f"- **Headings:** median {bench.get('h2_median')} H2 / {bench.get('h3_median')} H3",
                  f"- **Images:** median {bench.get('images_median')} per article — "
                  + (", ".join(f"{k} ×{v}" for k, v in (bench.get('image_kinds') or {}).items()) or "—"),
                  f"- **Tables:** median {bench.get('tables_median')} "
                  f"({bench.get('comparison_tables')}/{bench.get('sample')} use at least one)",
                  f"- **Video:** median {bench.get('videos_median')}",
                  f"- **FAQ:** {bench.get('with_faq')}/{bench.get('sample')} ship one"
                  + (f" ({', '.join(bench.get('faq_how') or [])})" if bench.get("faq_how") else ""), ""]
        if a.get("content_plan"):
            L += ["**Content types our article needs:**", ""] + \
                 [f"- {c}" for c in a["content_plan"]] + [""]

        # ---- clusters ----
        L += ["## Keyword clusters (ranked by total volume)", "",
              f"_Pool: {len(kr.get('related', []))} keywords from {kr.get('pool_sources')}._", "",
              "| Theme | Keywords | Total vol | Avg KD |", "|---|---|---|---|"]
        for cl in kr.get("clusters_ranked", []):
            L.append(f"| {cl['theme']} | {cl['count']} | {cl['total_volume']} | "
                     f"{cl['avg_kd'] if cl['avg_kd'] is not None else '—'} |")
        L.append("")
        for cl in kr.get("clusters_ranked", []):
            L.append(f"**{cl['theme']}** (vol {cl['total_volume']}, KD~{cl['avg_kd']}): "
                     + ", ".join(cl["keywords"]))
        L.append("")

        # ---- LSI ----
        lsi = kr.get("lsi") or []
        L += [f"## LSI vocabulary ({len(lsi)})", "",
              "_What the ranking pages TALK ABOUT (Ahrefs `also_talk_about`), filtered to terms "
              "the primary keyword does not already contain. These are words for the prose and "
              "entities to mention — NOT section ideas and NOT keywords to rank for._", ""]
        if lsi:
            L += ["| Term | Vol | KD |", "|---|---|---|"]
            L += [f"| {x['keyword']} | {x.get('volume', '—')} | {x.get('difficulty', '—')} |"
                  for x in lsi]
        else:
            L.append("_(none returned)_")
        L.append("")

        if a.get("recommended_sections"):
            L += ["## Recommended outline (hand-off to the brief)", ""] + \
                 [f"- {h}" for h in a["recommended_sections"]] + [""]
        if a.get("secondary_keywords"):
            L += ["## Secondary targets", "", ", ".join(a["secondary_keywords"]), ""]

        qs = kr.get("questions") or []
        srcs = kr.get("question_sources", {})
        L += [f"## Questions to answer ({len(qs)}) — Ahrefs:{srcs.get('ahrefs', 0)} · "
              f"suggestions:{srcs.get('suggestions', 0)} · "
              f"competitor headings:{srcs.get('competitor_headings', 0)}", ""]
        L += [f"- {q}" for q in qs] + [""]
        L += ["## Strategy", "", a.get("strategy", "—"), ""]
        if a.get("_parse_failed"):
            L += ["> ⚠️ analyst JSON parse failed — fallback shown."]
        return "\n".join(L)
