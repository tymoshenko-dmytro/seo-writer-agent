"""A stand-in run: the real engine end to end, with every paid service replaced by a canned answer.

The models (Anthropic and OpenAI) are replaced where the engine reaches the network: an agent's
Anthropic client answers from the canned texts below, chosen by the agent and what it was asked,
and the OpenAI read of the review panel does the same. Keyword research (Ahrefs) and deep research
(Parallel) return a canned result, the cover's background is drawn locally, the link index is
empty, and the uniqueness check is skipped (no Winston key). Everything else is the engine's own
code: the brief's structure gate, the writer's clean-up, the charts and the infographic, the cover,
the keyword report, the first version, the review, the safe changes and their check.

Used by tests/test_smoke.py (a topic becomes an article, offline and free) and by
bin/make_sample.py (the sample article in samples/). The article it writes is made up for the
tour: a guide to meta descriptions whose only source is Google's documentation on snippets.
"""
from __future__ import annotations

import io
import json
import re
from types import SimpleNamespace

TOPIC = "how to write a meta description"
SOURCE = "https://developers.google.com/search/docs/appearance/snippet"
SOURCE_TITLE = "Control your snippets in search results (Google Search Central)"

EXAMPLES = [
    ("Answer first", "A question with a short answer",
     "A meta description should run about 120 to 155 characters. Here is why, and how to write one that fits every screen size."),
    ("Outcome", "A guide or tutorial",
     "Write meta descriptions that win clicks: five patterns, a length rule and a checklist you can use on your next page today."),
    ("Specifics", "A product or pricing page",
     "Plans from $9 a month, unlimited projects and a free 14-day trial. Compare the features and pick the plan that fits your team."),
    ("Comparison", "A roundup or versus page",
     "We compared six project tools on price, setup time and integrations, so you can choose one without reading six pricing pages."),
    ("Local", "A page for one place",
     "Open seven days a week in central Leeds. Walk-in repairs for phones and laptops, most done while you wait. See our prices."),
]

META_TITLE = "Meta Description Guide: Length, Patterns and Common Mistakes"
META_DESCRIPTION = ("Learn how to write a meta description that earns the click: how long it should be, "
                    "five patterns that work, and the mistakes to avoid.")

KEYWORD_RESEARCH = {
    "topic": TOPIC, "country": "us", "primary_keyword": "meta description",
    "primary_stats": {},
    "analysis": {"search_intent": "informational",
                 "secondary_keywords": ["meta description length", "how to write a meta description"]},
    "related": [{"keyword": k} for k in ("meta description length", "how to write a meta description",
                                          "meta description examples", "seo meta description",
                                          "meta description best practices", "what is a meta description")],
    "lsi": [{"keyword": k} for k in ("search results", "snippet", "title tag", "search intent",
                                      "character limit", "click")],
    "questions": ["How long should a meta description be?", "Does Google always use the meta description?",
                  "Do meta descriptions affect rankings?"],
    "mandatory_keywords": [],
    "clusters_ranked": [],
    "meta": {"meta_title": META_TITLE, "meta_description": META_DESCRIPTION, "problems": []},
    "content_benchmark": {"images_median": 3, "sample": 3, "words_min": 820, "words_median": 1000,
                          "words_max": 1250, "words_target": 1000},
    "prototypes": [],
    "stand_in": True,
}


def _section(h2, words, keywords, content=()):
    return {"h2": h2, "words": words, "intent": h2, "keywords": list(keywords), "content_types": list(content), "h3": []}


BRIEF = {
    "title": "How to Write a Meta Description That Earns the Click",
    "meta_title": META_TITLE, "meta_description": META_DESCRIPTION,
    "search_intent": "informational", "target_words": 1000,
    "serp_notes": "Made up for the stand-in run.",
    "sections": [
        _section("What a meta description does", 200, ["what is a meta description"]),
        _section("How long a meta description should be", 200, ["meta description length"],
                 ["chart: the length of the example descriptions"]),
        _section("Patterns that make a description worth clicking", 250, ["meta description examples"],
                 ["comparison table: pattern, when it fits, example"]),
        _section("Mistakes that waste the snippet", 200, ["meta description best practices"]),
        _section("A check before you publish", 150, ["seo meta description"], ["checklist"]),
    ],
    "faq": ["How long should a meta description be?", "Does Google always use the meta description?"],
    "research_questions": ["What does Google's documentation say about meta descriptions and snippets?"],
}

RESEARCH = {
    "findings": [{
        "question": "What does Google's documentation say about meta descriptions and snippets?",
        "confidence": "high",
        "summary": "Google sometimes uses the meta description for the snippet; there is no length limit, but "
                   "snippets are truncated to fit the device; descriptions should be unique and readable.",
        "facts": [
            {"claim": "Google sometimes uses the meta description if it describes the page more accurately than "
                      "text taken from the page", "source_url": SOURCE},
            {"claim": "There is no limit on how long a meta description can be; the snippet is truncated as "
                      "needed, typically to fit the device width", "source_url": SOURCE},
            {"claim": "Identical or similar descriptions on every page are not helpful in search results",
             "source_url": SOURCE},
            {"claim": "Descriptions made of long strings of keywords do not give users a clear idea of the "
                      "page's content", "source_url": SOURCE},
            {"claim": "The nosnippet rule prevents a snippet; max-snippet sets its maximum length",
             "source_url": SOURCE},
        ],
    }],
    "key_statistics": [],
    "sources": [{"url": SOURCE, "title": SOURCE_TITLE}],
    "gaps": [],
}

_TABLE = "\n".join(["| Pattern | When it fits | Example |", "|---|---|---|"]
                   + [f"| {p} | {w} | \"{e}\" |" for p, w, e in EXAMPLES])

DRAFT = f"""<!-- visual:cover prompt="A tidy desk with a laptop showing a page of search results, soft daylight" -->

# How to Write a Meta Description That Earns the Click

A meta description is the short summary under a page's title in search results. Writing a good one takes ten minutes and pays back every time the page appears in a search, because it is the one line you control that tells a searcher why your page answers the question. This guide covers what the tag does, how long it should be, five patterns that work and the mistakes that waste it.

## What a meta description does

The meta description is an HTML tag in the head of a page: `<meta name="description" content="...">`. Readers never see it on the page itself. They see it in search results, where it often becomes the snippet under the title.

Often, not always. Google's documentation says it [sometimes uses the meta description]({SOURCE}) when the tag describes the page more accurately than text taken from the page. When it does not, Google builds the snippet from the page content instead, so a description that matches the page closely is the one most likely to be shown.

The tag is not a place to stuff keywords. Its job is to win the click once the page already ranks: the searcher has a question, and the description says your page answers it.

## How long a meta description should be

There is no fixed meta description length. The same documentation says there is no limit on how long a description can be, but the snippet is truncated as needed, typically to fit the device width. In practice the first part of the description carries the weight, and anything after it may be cut.

A sensible working range is about 120 to 155 characters. Shorter descriptions leave room unused; longer ones risk losing their ending on a phone. Put the most important words first, so a cut costs only the least important ones.

<!-- visual:chart data="the five example descriptions below, counted" caption="The length of the five example descriptions in this guide, in characters" -->

The chart counts the five examples from the next section. All of them sit inside the working range, and none needs its last words to make sense.

## Patterns that make a description worth clicking

Most descriptions that earn clicks follow one of a few patterns. Pick the one that matches what the searcher wants from the page.

{_TABLE}

The prices and places in the examples are made up for illustration: on a real page, use your own.

Each pattern makes one promise the page keeps. That is the real rule: a description that oversells gets the click and then loses the reader, who goes back to the results and picks someone else.

## Mistakes that waste the snippet

The most common mistake is the same description on many pages. Google notes that identical or similar descriptions on every page are not helpful when individual pages appear in search results. If a template fills in the same sentence everywhere, write page-specific descriptions for your most important pages first.

The second is a list of keywords instead of a sentence. Descriptions made of long strings of keywords do not give searchers a clear idea of the page's content, and they read as spam. Write for a person with a question.

The third is a description that does not match the page. Most searchers decide in under two seconds which result to open, so a mismatch costs you twice: the click goes elsewhere, or the visitor leaves at once.

Finally, avoid writing the description last, in a hurry, after the page is published. Google may rewrite the snippet anyway, but a careful description is what it shows when the page and the tag agree.

## A check before you publish

Before a page goes live, run through a short check. It takes a minute and catches most of the problems above.

1. The description is unique to this page.
2. It answers the searcher's question in the first 120 characters.
3. It reads as a sentence, not a list of keywords.
4. It promises only what the page delivers.
5. It ends with a reason to click: a number, a result or a next step.

If a page should never show a snippet, the nosnippet rule handles that, and max-snippet sets a maximum length. Both are described in the same Google guide.
"""

FAQ = {"faq": [
    {"question": "How long should a meta description be?",
     "answer": "There is no fixed limit, but search results cut the snippet to fit the screen. About 120 to 155 "
               "characters, with the most important words first, works on most devices."},
    {"question": "Does Google always use the meta description?",
     "answer": "No. Google uses it when it describes the page better than text taken from the page itself, and "
               "builds its own snippet otherwise."},
]}

CHECKLIST = {"type": "checklist", "title": "Before you publish a meta description",
             "items": ["Unique to this page", "Answers the question in 120 characters",
                       "Reads as a sentence", "Promises only what the page delivers",
                       "Ends with a reason to click"]}

ENRICH = {"insertions": [{"type": "infographic", "after_heading": "A check before you publish", "spec": CHECKLIST}]}

CHARTS = {"charts": [{
    "marker_index": 1, "skip": False, "type": "hbar",
    "title": "The five example descriptions, in characters",
    "unit": "", "note": "the working range is about 120 to 155",
    "categories": [p for p, _, _ in EXAMPLES], "values": [len(e) for _, _, e in EXAMPLES], "highlight": None,
    "caption": "The length of the five example descriptions in this guide, in characters",
    "alt": "Bar chart of the length of five example meta descriptions, each between 120 and 130 characters long",
    "source": "Counted from the examples in this guide",
}]}

# the review: what the panel's reads raise (the editor's output contract, prompts/editor.md)
_REPEAT = ("Google may rewrite the snippet anyway, but a careful description is what it shows when the page "
           "and the tag agree.")
_UNSOURCED = ("Most searchers decide in under two seconds which result to open, so a mismatch costs you twice: "
              "the click goes elsewhere, or the visitor leaves at once.")
FINDINGS = [
    {"fix": f"The sentence '{_REPEAT}' repeats the point of the first section. Cut it here.",
     "severity": "Minor", "check": "structure", "basis": "other", "section": "Mistakes that waste the snippet",
     "kind": "text", "action": "edit", "target": _REPEAT, "phrase": None},
    {"fix": "The section 'Patterns that make a description worth clicking' does not use the phrase 'meta description "
            "examples'. Reword its first sentence to carry it.",
     "severity": "Minor", "check": "keywords", "basis": "other", "section": "Patterns that make a description worth clicking",
     "kind": "keyword", "action": "insert_keyword", "target": "Patterns that make a description worth clicking",
     "phrase": "meta description examples"},
    {"fix": f"The sentence '{_UNSOURCED}' gives a figure that no source supports. Remove the number, or cite a "
            f"study that measured it.",
     "severity": "Major", "check": "citations", "basis": "not_in_research", "section": "Mistakes that waste the snippet",
     "kind": "fact", "action": "edit", "target": _UNSOURCED, "phrase": None},
    {"fix": "The checklist picture repeats the five numbered items right above it. Remove the picture, or keep it "
            "and cut the list.",
     "severity": "Minor", "check": "visuals", "basis": "other", "section": "A check before you publish",
     "kind": "picture", "action": "remove_picture", "target": "assets/infographic_1.png", "phrase": None},
]


def editor_read(n: int) -> dict:
    """Read n of the panel (1-based): the first raises everything, the others part of it, as
    real reads do."""
    picked = FINDINGS if n == 1 else [FINDINGS[0], FINDINGS[2]] if n == 2 else [FINDINGS[0], FINDINGS[3]]
    sev = {"critical": 0, "major": 0, "minor": 0, "nitpick": 0}
    for f in picked:
        sev[f["severity"].lower()] += 1
    score = 100 - 7 * sev["major"] - 3 * sev["minor"]
    return {"decision": "REJECTED" if sev["major"] else "APPROVED", "score": score, "score_breakdown": sev,
            "checks": {}, "feedback": ["Clear, well-sourced guide."], "required_fixes": picked,
            "blocking": bool(sev["major"])}


# what the fixer writes for each change the machine may make: (the span it finds, its new words)
FIXES = [
    ("repeats the point of the first section",
     "Finally, avoid writing the description last, in a hurry, after the page is published. " + _REPEAT,
     "Finally, avoid writing the description last, in a hurry, after the page is published."),
    ("meta description examples",
     "Most descriptions that earn clicks follow one of a few patterns.",
     "Most descriptions that earn clicks follow one of a few patterns, and the meta description examples below show each one."),
]


# ---------------------------------------------------------------------------------------------
# the answers, by agent
# ---------------------------------------------------------------------------------------------
def _text_of(kw: dict) -> str:
    """Everything the agent sent, as one text (the system prompt, the user blocks)."""
    parts = []
    sys_ = kw.get("system")
    parts += [b.get("text", "") for b in sys_] if isinstance(sys_, list) else [str(sys_ or "")]
    for m in kw.get("messages") or []:
        c = m.get("content")
        parts += [b.get("text", "") for b in c if isinstance(b, dict)] if isinstance(c, list) else [str(c or "")]
    return "\n".join(parts)


def answer(agent: str, text: str) -> str:
    """The canned answer an agent gets for what it asked."""
    if agent == "brief_generator":
        return json.dumps(BRIEF)
    if agent == "copywriter":
        return json.dumps(FAQ) if "Answer each FAQ question" in text else DRAFT
    if agent == "visual_director":
        return json.dumps({**ENRICH, **CHARTS})
    if agent == "visual_checker":
        return json.dumps({"ok": True, "problems": []})
    if agent == "fix_applier":
        fix = text.rsplit("THE FIX TO APPLY", 1)[-1]
        for key, find, replace in FIXES:
            if key in fix:
                return json.dumps({"find": find, "replace": replace, "note": "stand-in"})
        return json.dumps({"find": "", "replace": "", "note": "cannot read naturally: a stand-in has no edit for it"})
    if agent == "edit_verifier":
        ids = re.findall(r"^\[([^\]\s]+)\]", text, flags=re.M)
        return json.dumps({"items": [{"id": i, "resolved": True, "why": "The text now does what the item asked.",
                                      "new_problem": None} for i in ids]})
    return ""                                  # the finding matcher, the meta repair: no answer


class _Messages:
    def __init__(self, agent):
        self.agent = agent

    def create(self, **kw):
        name = getattr(self.agent, "name", "")
        if name == "editor":
            self.agent._standin_reads = getattr(self.agent, "_standin_reads", 0) + 1
            text = json.dumps(editor_read(self.agent._standin_reads))
        else:
            text = answer(name, _text_of(kw))
        usage = SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_input_tokens=0,
                                cache_creation_input_tokens=0, cache_creation=None)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], usage=usage,
                               stop_reason="end_turn", model="stand-in")


def _background_png() -> bytes:
    """The cover's background, drawn here instead of by an image model: soft bands of colour."""
    from PIL import Image, ImageDraw, ImageFilter

    w, h = 1536, 1024
    im = Image.new("RGB", (w, h), (28, 46, 74))
    d = ImageDraw.Draw(im)
    for i, colour in enumerate([(42, 111, 219), (90, 160, 200), (230, 190, 120), (36, 70, 120)]):
        d.ellipse([(i * 380) - 200, 300 + (i % 2) * 220, (i * 380) + 600, 1100 + (i % 2) * 220], fill=colour)
    im = im.filter(ImageFilter.GaussianBlur(120))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def install(mp) -> None:
    """Every paid or networked step answered locally. `mp` is a pytest MonkeyPatch."""
    from core.agents import base, deep_researcher, editor, keyword_researcher
    from core.store import atomic_write_text
    from core.tools import link_index, openai_images

    mp.setattr(base.BaseAgent, "client", property(lambda self: SimpleNamespace(messages=_Messages(self))))

    def openai_read(self, model, user, system=None, max_tokens=None, agent_key="editor_openai"):
        self._standin_reads = getattr(self, "_standin_reads", 0) + 1
        return json.dumps(editor_read(self._standin_reads))

    mp.setattr(base.BaseAgent, "call_openai", openai_read)

    def keywords(self, state):
        from pathlib import Path

        state.current_agent = "keyword_researcher"
        state.keyword_research = json.loads(json.dumps(KEYWORD_RESEARCH))
        atomic_write_text(Path(state.output_dir) / "01_keyword_research.md",
                          f"# Keyword research: {state.topic}\n\nA stand-in: no search data was read.\n\n"
                          f"Primary keyword: **{KEYWORD_RESEARCH['primary_keyword']}**\n")
        state.note("[keywords] stand-in keyword research")
        return state

    def research(self, state):
        from pathlib import Path

        state.current_agent = "deep_researcher"
        state.research = json.loads(json.dumps(RESEARCH))
        atomic_write_text(Path(state.output_dir) / "03_research.md",
                          f"# Research: {state.topic}\n\nA stand-in: one source.\n\n- [{SOURCE_TITLE}]({SOURCE})\n")
        state.note("[research] stand-in research: 1 source")
        return state

    mp.setattr(keyword_researcher.KeywordResearcher, "run", keywords)
    mp.setattr(deep_researcher.DeepResearcher, "run", research)
    mp.setattr(openai_images, "generate", lambda prompt, quality="medium", size="1536x1024": _background_png())
    mp.setattr(link_index.LinkIndex, "build", classmethod(lambda cls, cfg, log=print: cls([], generated_at=0.0)))
    mp.setattr(editor.Editor, "_hint_block", lambda self, state: "")
