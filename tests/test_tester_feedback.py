"""The testers' feedback on the deployed Studio (2026-10-20): our product near the top, no text cut
with "…", no picture made of text, no advert of our own site, the most important first on Review,
and a tick on every change the machine can make."""
from __future__ import annotations

import json
import re

import pytest


# ---- our product near the top ----------------------------------------------------------------
ROUNDUP = """# Best widget tools

Intro text about widgets and how teams choose them.

## The best widget tools in 2026

{entries}

## How to choose

Prose.
"""


def _roundup(names):
    return ROUNDUP.format(entries="\n\n".join(f"### {i}. {n}\n\n" + "Words about it. " * 60
                                              for i, n in enumerate(names, 1)))


@pytest.mark.parametrize("names,place,warned", [
    (["Acme Widgets", "Bolt", "Crate", "Dyno"], 1, False),
    (["Bolt", "Acme", "Crate", "Dyno"], 2, False),
    (["Bolt", "Crate", "Acme Widgets", "Dyno"], 3, True),
    (["Bolt", "Crate", "Dyno"], None, False),
])
def test_the_structure_report_says_where_our_entry_stands(names, place, warned):
    from core.validators import analyze_structure, format_structure_report

    rep = analyze_structure(_roundup(names), own_names=("Acme",))
    assert rep["own_places"] == [{"section": "The best widget tools in 2026", "place": place, "of": len(names)}]
    text = format_structure_report(rep)
    assert ("the first or the second place is the rule" in text) is warned
    assert ("OUR ENTRY: not among" in text) is (place is None)
    # a word that only contains the name is not our entry ("Acmeville")
    assert analyze_structure(_roundup(["Acmeville", "Bolt", "Crate"]), own_names=("Acme",))["own_places"][0]["place"] is None
    # no names, no measure
    assert analyze_structure(_roundup(names))["own_places"] == []


@pytest.mark.parametrize("module,cls,words", [
    ("brief_generator", "BriefGenerator", "it is the first or the second entry"),
    ("copywriter", "Copywriter", "it is the first or the second"),
    ("editor", "Editor", "our entry is the first or the second"),
])
def test_the_brief_the_writer_and_the_review_put_our_product_near_the_top(output_dir, module, cls, words):
    import importlib

    from core.config import load_core_config, load_project

    cfg = load_core_config()
    text = " ".join(getattr(importlib.import_module(f"core.agents.{module}"), cls)(load_project(core=cfg), cfg)
                    .system_prompt().split())
    assert words in text


# ---- no text cut with "…" ------------------------------------------------------------------------
LONG = "A " + "very " * 60 + "long title"


@pytest.mark.parametrize("title,url,shown", [
    # the testers' caption: the site's name, by its initials, at atanet.org
    ("What is the Difference Between Translation and Interpreting? - American Translators Association",
     "https://www.atanet.org/client-assistance/", "What is the Difference Between Translation and Interpreting?"),
    ("Pricing plans | Acme", None, "Pricing plans"),
    ("Pricing | Acme", None, "Pricing"),                         # RA5D-01: one word is a title too
    ("Home - Acme", "https://acme.example/", "Home"),
    ("Home - Acme", None, "Home - Acme"),                        # a dash with no site named: the words stay
    ("Acme | Pricing plans", "https://acme.example/pricing", "Pricing plans"),     # the site's name first
    # only the site's name goes: a clause after a dash stays (RA54-01, RA5D-01)
    ("Pricing plans - annual versus monthly | Acme", None, "Pricing plans - annual versus monthly"),
    ("Pricing plans - Annual Versus Monthly", "https://vendor.example/pricing", "Pricing plans - Annual Versus Monthly"),
    ("Example Vendor Pricing - Example Vendor", "https://www.example-vendor.com/pricing", "Example Vendor Pricing"),
    (LONG, None, LONG),                                          # never cut (RA54-01: no silent cut either)
    # the title's own characters stay: the Studio's caption keeps them, made safe by safe_words
    ("Setup guide [2026] | Acme", None, "Setup guide [2026]"),
])
def test_a_page_title_caption_is_never_cut_with_an_ellipsis(title, url, shown):
    from core.agents.visual_director import page_title

    assert page_title(title, url) == shown


# ---- no picture made of text ------------------------------------------------------------------------
def test_a_refused_screenshot_is_not_replaced_by_a_picture_of_text(tmp_path, monkeypatch):
    """The fallback that drew a vendor's researched facts as text cards is gone: the slot is
    dropped with its reason, and no infographic is made."""
    from core.agents import visual_director as vdm
    from core.config import load_core_config, load_project
    from core.workflow_state import ArticleState

    monkeypatch.setenv("CONTENT_OUTPUT_DIR", str(tmp_path))
    cfg = load_core_config()
    vd = vdm.VisualDirector(load_project(core=cfg), cfg)
    monkeypatch.setattr(vdm.VisualDirector, "_enrich", lambda self, state: None)
    monkeypatch.setattr(vdm.VisualDirector, "_shoot", lambda self, *a, **kw: (None, {"reason": "LOGIN", "detail": "a sign-in wall"}))
    monkeypatch.setattr(vdm, "_alt_pages", lambda research, url: [])
    s = ArticleState(topic="widgets", slug="w")
    s.output_dir = str(tmp_path / "w")
    (tmp_path / "w").mkdir()
    url = "https://vendor.example/pricing"
    s.research = {"key_statistics": [{"stat": "Plan", "value": "$8 per user", "source_url": url},
                                     {"stat": "Seats", "value": "Up to 50", "source_url": url}]}
    s.draft_markdown = f'# T\n\n## Vendor\n\nProse.\n\n<!-- visual:screenshot url="{url}" caption="Vendor pricing" -->\n'
    vd.run(s)
    assert not list((tmp_path / "w" / "assets").glob("infographic_*")) if (tmp_path / "w" / "assets").exists() else True
    entry = next(v for v in s.visuals if v.get("type") == "screenshot")
    assert entry["status"] == "skipped" and entry["reason"] == "LOGIN"
    assert "visual:screenshot" not in s.draft_markdown and "key facts" not in s.draft_markdown.lower()


@pytest.mark.parametrize("kind,kept", [("flow", True), ("decision", True), ("stats", True),
                                       ("cards", False), ("checklist", False)])
def test_the_visual_director_proposes_no_infographic_of_sentences(tmp_path, monkeypatch, kind, kept):
    from core.agents import visual_director as vdm
    from core.config import load_core_config, load_project
    from core.workflow_state import ArticleState

    monkeypatch.setenv("CONTENT_OUTPUT_DIR", str(tmp_path))
    cfg = load_core_config()
    vd = vdm.VisualDirector(load_project(core=cfg), cfg)
    spec = {"type": kind, "title": "How it works", "steps": [{"label": "One", "desc": "a"}, {"label": "Two", "desc": "b"},
                                                             {"label": "Three", "desc": "c"}],
            "branches": [{"if": "a", "then": "b"}, {"if": "c", "then": "d"}, {"if": "e", "then": "f"}],
            "items": [{"value": "40%", "label": "faster"}, {"value": "3x", "label": "more"}] if kind == "stats"
            else ["one", "two", "three", "four"]}
    seen = []

    def model(self, prompt, **kw):
        seen.append(prompt)
        return {"insertions": [{"type": "infographic", "after_heading": "How it works", "spec": spec}]}, ""

    monkeypatch.setattr(vdm.VisualDirector, "call_claude_json", model)
    monkeypatch.setattr(vdm.infographic, "render", lambda spec, out, **kw: (open(out, "wb").write(b"png") and out))
    s = ArticleState(topic="widgets", slug="w")
    s.output_dir = str(tmp_path / "w")
    (tmp_path / "w").mkdir()
    s.research = {}
    s.draft_markdown = "# T\n\n## How it works\n\n" + "Words. " * 900 + "\n"
    vd._enrich(s)
    assert ("visual:infographic" in s.draft_markdown or "infographic_" in s.draft_markdown) is kept, s.log[-3:]
    assert seen and "cards" not in re.findall(r'"type": "([a-z|]+)", "title"', seen[0])[0]


def test_the_picture_check_names_text_only_pictures_and_adverts_of_our_site():
    from core.agents.visual_checker import VisualChecker

    for defect in ("text_only", "marketing"):
        got, dropped = VisualChecker.accept({"path": "assets/infographic_1.png", "made_as": "infographic"},
                                            [{"severity": "Major", "defect": defect, "action": "remove",
                                              "problem": "Sentences in boxes", "what": ""}])
        assert got and got[0]["defect"] == defect and not dropped


@pytest.mark.parametrize("module,cls,words", [
    ("visual_checker", "VisualChecker", "an advert of our own site"),
    ("visual_checker", "VisualChecker", "only text"),
    ("brief_generator", "BriefGenerator", "never our homepage or a landing page's top"),
    ("copywriter", "Copywriter", "never our homepage or a landing page's top"),
])
def test_our_own_site_is_shot_only_where_it_shows_the_product(output_dir, module, cls, words):
    import importlib

    from core.config import load_core_config, load_project

    cfg = load_core_config()
    text = " ".join(getattr(importlib.import_module(f"core.agents.{module}"), cls)(load_project(core=cfg), cfg)
                    .system_prompt().split()).lower()
    assert words in text


# ---- Review: the most important first, a tick on every change the machine can make -------------
QUOTE = "The tag is not a place to stuff keywords."     # a sentence of the sample article, found once


def _sample_with(output_dir, findings):
    from core import qa_changes as qc, qa_model as qm, sample, store

    slug = sample.add(output_dir)
    art = store.Repository(output_dir).get(slug)
    run, rv = qc.records(art)
    for fid, sev, action, note in findings:
        f = qm.finding(id=fid, text=f"Finding {fid}.", review=rv["reviews"][-1]["id"], source="reader",
                       severity=sev, kind="text", action=action,
                       target=QUOTE if action == "edit" else "the first section")
        if note:
            f["note"], f["tried"] = note, 1
        rv["findings"].append(f)
    run["review"] = rv
    art.save_run(run)
    return art


def test_needs_you_lists_the_most_important_first(studio, output_dir):
    from core import agent_door
    from ui import article_view as V
    from core import store

    art = _sample_with(output_dir, [("x-nit", "nitpick", "outline", None), ("x-crit", "critical", "outline", None),
                                    ("x-maj", "major", "outline", None)])
    body = store.split_frontmatter(art.text())[1]
    queue = [i for i in V.review_queue(art, body, agent_door.review_view(art), {}) if i["group"] == "needs"]
    sevs = [i.get("severity") for i in queue]
    ranks = {"critical": 0, "major": 1, "unknown": 1, "minor": 2, "nitpick": 3}
    assert [ranks[s] for s in sevs] == sorted(ranks[s] for s in sevs) and sevs[0] == "critical"
    page = studio.get(f"/a/{art.slug}?tab=review").get_data(as_text=True)
    assert page.index("Finding x-crit.") < page.index("Finding x-maj.") < page.index("Finding x-nit.")
    assert "the most important first" in page


def test_a_try_again_item_has_a_tick_and_is_made_with_the_others(studio, output_dir, monkeypatch):
    from core import agent_door, qa_changes as qc

    art = _sample_with(output_dir, [("x-left", "major", "edit", "the change reached outside the paragraph that holds its words")])
    items = {i["id"]: i for i in agent_door.review_view(art)["needs_you"]}
    assert items["x-left"]["type"] == "left"
    page = studio.get(f"/a/{art.slug}?tab=review").get_data(as_text=True)
    ticks = re.findall(r'<input type="checkbox" form="batch" name="ids" value="([^"]+)"', page)
    assert "x-left" in ticks and set(ticks) >= {i for i, it in items.items() if it["type"] in ("proposal", "left") and not it.get("choices")}
    assert "Make the ticked changes" in page
    tried = []

    def attempt(w, f, group=None, chosen=False):
        tried.append((f["id"], chosen))
        return None, "stand-in: not made"

    monkeypatch.setattr(qc, "attempt", attempt)
    monkeypatch.setattr(qc, "_already_there", lambda w, f: None)
    rv = agent_door.review_view(art)["review"]
    rep = qc.apply_proposals(art.slug, ["x-left", "r1-3"], art.version(), rv["id"])
    assert ("x-left", False) in tried and ("r1-3", True) in tried, rep
