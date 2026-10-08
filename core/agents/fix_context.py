"""What the fixer is given beside one instruction (core/qa_changes.Work): the links an edit may
keep or add, the names it may introduce, and the context every call shares. Taken out of the old
QAFixer when its pass loop left; the old editor's
verdict, which the context used to open with, is no longer part of it."""
from __future__ import annotations

from ..keyword_report import format_coverage_report
from ..tools.link_index import LinkIndex
from ..validators import analyze_structure, format_structure_report, selling_surface
from .fix_routing import _FACTS_URL, _LINK


def allowed_urls(project, state, original: str) -> set:
    """The URLs an edit may leave in or add: whatever the article cites plus every source the
    research supplied, except a competitor's selling page (cited in prose, never linked); our own
    pages the product facts link; the internal link index. Anything else an edit introduces is
    invented."""
    allowed = {u for _, u in _LINK.findall(original)}
    in_article = set(allowed)
    links = project.links or {}
    hosts, docs = links.get("competitor_hosts") or (), links.get("competitor_doc_paths") or ()

    def allow(url):
        if url and (url in in_article or not selling_surface(url, hosts, docs)):
            allowed.add(url)

    r = state.research or {}
    for st in (r.get("key_statistics") or []):
        allow(st.get("source_url"))
    for f in (r.get("findings") or []):
        for fact in (f.get("facts") or []):
            allow(fact.get("source_url"))
    # replacing an invented CTA with our app's sign-up page was refused as "not in the research"
    for u in _FACTS_URL.findall(project.product_facts or ""):
        allowed.add(u.rstrip(".,;"))
    idx = LinkIndex.load(project.root)
    if idx:
        allowed |= {t.url for t in idx.targets}
    return allowed


def sanctioned_names(project, state) -> str:
    """The text whose names an edit may introduce (the smuggling guard's exemptions): the
    researched facts, the key statistics, the sources' own titles and the product facts. Research
    QUESTIONS are left out: a question names what was asked about, not what was found."""
    r = state.research or {}
    exempt = []
    for f in (r.get("findings") or []):
        for fact in (f.get("facts") or []):
            exempt.append(str(fact.get("claim") or "") + " " + str(fact.get("value") or ""))
    for st in (r.get("key_statistics") or []):
        exempt.append(str(st.get("stat") or "") + " " + str(st.get("value") or ""))
    # a source's own title is what the page is called, and an edit linking it may use it as the
    # anchor (refused twice as a smuggled name on the webinar replays of 2026-09-28)
    for s in (r.get("sources") or []):
        if isinstance(s, dict):
            exempt.append(str(s.get("title") or ""))
    exempt.append(project.product_facts or "")
    return "\n".join(exempt)


def context_block(project, state, target) -> str:
    """The context every fixer call shares. It does not list the other changes: each call makes
    exactly one, and showing the rest invites one sweeping edit."""
    idx = LinkIndex.load(project.root)
    inventory = "(no inventory available)"
    links = project.links or {}
    if idx:
        picked = idx.select(" ".join([state.topic, (state.brief or {}).get("title", "")]),
                            n=links.get("per_article", 12), min_product=links.get("min_product_links", 2))
        inventory = "\n".join(f"- {t.title}\n  {t.url}" for t in picked)

    r = state.research or {}
    srcs = {x["source_url"] for x in (r.get("key_statistics") or []) if x.get("source_url")}
    for f in (r.get("findings") or []):
        for fact in (f.get("facts") or []):
            if fact.get("source_url"):
                srcs.add(fact["source_url"])
    rep = analyze_structure(state.draft_markdown, target_words=target, sources_available=len(srcs),
                            allowed_urls=[t.url for t in idx.targets] if idx else None,
                            competitor_hosts=links.get("competitor_hosts") or (),
                            competitor_doc_paths=links.get("competitor_doc_paths") or (),
                            article_dir=getattr(state, "output_dir", "") or None)
    return f"""CONTEXT — the article is being polished, not rebuilt.

MEASURED STRUCTURE (do not make any of these worse):
{format_structure_report(rep)}

MEASURED KEYWORD COVERAGE (the numbers behind any keyword fix above; a target marked "already
covered in prose but never written as this phrase" is fixed by rewording ONE sentence, not by
adding text — and never force a phrase into unnatural English):
{format_coverage_report(state.keywords, max_items=6) if state.keywords else "(not measured)"}

A CLAIM THAT SOMETHING IS ABSENT ("does not publish its prices", "no public pricing") is never
fixed by supplying the missing figure or name: the research does not have it, and an edit that
invents one is refused. Remove the claim, or, when the fix says our check found nothing, state it
as our own dated check ("We could not find a published price on X's website (checked <month
year>)").

INTERNAL LINK INVENTORY (only if the fix asks for an internal link; never invent a URL):
{inventory}

SOURCES AN EDIT MAY LINK (only if the fix asks for a citation; copy the URL exactly as written
here: a near-miss spelling of a real page is refused as an invented source):
{linkable_sources(project, state, srcs)}"""


def linkable_sources(project, state, srcs) -> str:
    links = project.links or {}
    hosts, docs = links.get("competitor_hosts") or (), links.get("competitor_doc_paths") or ()
    in_article = {u for _, u in _LINK.findall(state.draft_markdown or "")}
    keep = [u for u in sorted(srcs) if u in in_article or not selling_surface(u, hosts, docs)]
    keep += [u.rstrip(".,;") for u in _FACTS_URL.findall(project.product_facts or "")]
    keep = list(dict.fromkeys(keep))[:80]
    return "\n".join(f"- {u}" for u in keep) or "(none)"
