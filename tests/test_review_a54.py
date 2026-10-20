"""The review of a549d73 (seo-writer-agent-reviews/refactoring-review-a549d73.md): its probes for
RA54-01..05 kept as tests: the caption from a page title, our own homepage as a substitute shot,
one order of what needs the person everywhere, a half-done picture in that order, the price of a
mixed batch of ticks."""
import json
from pathlib import Path

import pytest

from core import qa_model as qm, store
from core.config import load_core_config, load_project
from test_release_controls import cli_json

EARLY = "The early paragraph describes the widget."
LATE = "The later paragraph explains the choice."
BODY = f"""# Widget guide

## First section

{EARLY}

![Widget comparison](assets/chart_1.png)
*Widget comparison*

## Last section

{LATE}

The phrase widget guide is already here.
"""


def finding(fid, severity="major", action="edit", target=LATE, kind="text", **extra):
    f = qm.finding(id=fid, text=f"Change requested for {fid}.", review="r1", source="reader",
                   severity=severity, kind=kind, action=action, target=target,
                   phrase=extra.pop("phrase", None))
    f.update(extra)
    return f


def article(root, findings, body=BODY):
    from PIL import Image
    from core.qa_migration import RUN_SCHEMA
    art = store.Repository(root).reserve("reviewer-widget")
    art.write_article('title: Widget guide\nslug: reviewer-widget\n', body)
    art.assets.mkdir(exist_ok=True)
    Image.new("RGB", (80, 60), "white").save(art.assets / "chart_1.png")
    store.write_json(art.assets / "chart_1.json", {
        "producer": "chart", "spec": {"type": "bar", "title": "Widget comparison",
                                     "categories": ["Alpha", "Beta"], "values": [2, 3]}})
    art.save_run({"schema": RUN_SCHEMA, "topic": "Widget guide", "status": "success",
                  "research": {}, "brief": {}, "keyword_research": {},
                  "review": {"reviews": [qm.review_record(id="r1", version=art.version())],
                             "findings": findings, "changes": []}})
    return art


def config_project():
    cfg = load_core_config()
    return cfg, load_project(core=cfg)


def history_costs(art, costs):
    """A wholly synthetic ledger, read by the real cost estimator."""
    for op, cost in {"generation": 0.0, **costs}.items():
        store.append_jsonl(art.dir / "_usage.jsonl", {
            "ts": "2026-10-19T10:00:00+00:00", "op": op, "op_id": f"fixture-{op}",
            "stage": "edits", "provider": "anthropic", "item": "synthetic-model",
            "units": {"calls": 1}, "cost_usd": cost, "estimated": False})


def model_edits(monkeypatch, mapping):
    """Only replace the model response. Routing, guards, writes and versions remain real."""
    from core.agents.fix_applier import FixApplier
    from core.agents.edit_verifier import EditVerifier
    calls = []

    def answer(self, model, base, tail="", **kw):
        calls.append((base, tail))
        for before, after in mapping.items():
            if before in tail:
                return json.dumps({"find": before, "replace": after, "note": "Local fixture"})
        return json.dumps({"find": "", "note": "No fixture edit"})

    monkeypatch.setattr(FixApplier, "call_claude_cached", answer)
    monkeypatch.setattr(EditVerifier, "call_claude_json", lambda *a, **kw: ({"items": []}, ""))
    return calls


def test_cli_mixed_ticks_estimate_uses_each_operations_own_cost(output_dir, monkeypatch, capsys):
    from core import agent_door
    from ui.article_view import review_queue, costs_of
    from core import store
    art = article(output_dir, [
        finding("machine", target=EARLY),
        finding("proposal", target=LATE, kind="fact"),
        finding("remove", action="remove_picture", target="assets/chart_1.png", kind="picture")])
    costs = {"fix": .73, "proposals": .21, "visual_insert": .08}
    history_costs(art, costs)
    assert agent_door.estimate(output_dir, "fix")["usd"] == .73
    assert agent_door.estimate(output_dir, "proposals")["usd"] == .21
    ui = review_queue(art, store.split_frontmatter(art.text())[1], agent_door.review_view(art),
                      costs_of(output_dir, {}))
    assert {i["id"]: i["cost"] for i in ui} == {
        "machine": "about $0.73", "proposal": "about $0.21", "remove": None}
    code, rep = cli_json(monkeypatch, capsys, "apply", art.slug, "machine", "proposal", "remove",
                         "--version", art.version(), "--review", "r1")
    assert code == 1 and rep["refused"] == "needs_yes", rep
    assert rep["estimate"]["usd"] == .94, rep


@pytest.mark.parametrize("entry", ["agent_door", "cli"])
def test_needs_you_has_the_same_priority_order_in_agent_and_ui(studio, output_dir, monkeypatch, capsys, entry):
    from core import agent_door, store
    from ui.article_view import review_queue

    art = article(output_dir, [
        finding("nit-early", "nitpick", target=EARLY),
        finding("major-late", "major"),
        finding("critical-late", "critical"),
        finding("major-early", "major", target=EARLY),
    ])
    rv = agent_door.review_view(art)
    expected = ["critical-late", "major-early", "major-late", "nit-early"]
    queue = review_queue(art, store.split_frontmatter(art.text())[1], rv, {})
    assert [i["id"] for i in queue if i["group"] == "needs"] == expected
    # Actual HTTP rendering agrees with the UI queue.
    page = studio.get(f"/a/{art.slug}?tab=review").get_data(as_text=True)
    positions = [page.index(f'id="f-{fid}"') for fid in expected]
    assert positions == sorted(positions)
    if entry == "cli":
        code, rv = cli_json(monkeypatch, capsys, "findings", art.slug)
        assert code == 0
    assert [i["id"] for i in rv["needs_you"]] == expected


def test_half_done_picture_keeps_article_order_within_its_severity(studio, output_dir, monkeypatch):
    from PIL import Image
    from core import agent_door, qa_changes as qc, store
    from core.agents.interactive import VisualProducerService
    from core.agents.visual_checker import VisualChecker
    from core.producers import charts
    from ui.article_view import review_queue

    art = article(output_dir, [
        finding("picture", action="redraw", target="assets/chart_1.png", kind="picture"),
        finding("late", target=LATE),
    ])
    monkeypatch.setattr(VisualProducerService, "call_claude_json", lambda *a, **kw: ({
        "producer": "chart", "spec": {"type": "bar", "title": "Widget comparison",
                                     "categories": ["Alpha", "Beta"], "values": [2, 3]},
        "alt": "Widget comparison", "caption": "Widget comparison"}, ""))

    def render(spec, out, **kw):
        Image.new("RGB", (80, 60), "blue").save(out)
        return out

    monkeypatch.setattr(charts, "build", render)
    monkeypatch.setattr(VisualChecker, "call_claude_json", lambda *a, **kw: ({
        "ok": False, "problems": [{"severity": "Major", "defect": "unreadable",
                                  "problem": "The labels are still unreadable.",
                                  "action": "redraw", "what": "Larger labels"}]}, ""))
    rep = qc.fix_again(art.slug, "picture", art.version())
    assert rep["landed"] == ["picture"], rep
    rv = agent_door.review_view(art)
    group = rv["changed"][0]
    assert group["half_done"] and group["severity"] == "major", group
    body = store.split_frontmatter(art.text())[1]
    assert body.index("assets/chart_1.png") < body.index(LATE)
    queue = [i for i in review_queue(art, body, rv, {}) if i["group"] == "needs"]
    page = studio.get(f"/a/{art.slug}?tab=review").get_data(as_text=True)
    assert "The labels are still unreadable." in page
    assert [i["id"] for i in queue] == [group["id"], "late"], [
        (i["id"], i["severity"], i["at"], i["section"]) for i in queue]


@pytest.mark.parametrize("mode", ["director-title-clause", "director-long-title", "studio-create", "studio-redo",
                                 "studio-create-suffix", "studio-redo-suffix"])
def test_substitute_caption_is_the_page_title_without_only_the_site_suffix(output_dir, monkeypatch, mode):
    from PIL import Image
    from core.agents.interactive import VisualProducerService
    from core.agents.visual_director import VisualDirector, ShotLook
    from core.producers import screenshot
    from core.workflow_state import ArticleState
    from core import store

    cfg, proj = config_project()
    title = ("Pricing plans" if mode.endswith("-suffix") else
             "Pricing plans - annual versus monthly"
             if mode == "director-title-clause" else
             "A complete guide to choosing a widget plan for distributed teams with seasonal demand "
             "and different billing requirements across regional offices without losing access to "
             "shared reports and project histories")
    page_title = title + " | Acme"
    art = article(output_dir, [])
    old, substitute = "https://vendor.example/private", "https://vendor.example/guide"
    art.save_run({**art.run(), "research": {"sources": [{"url": substitute}]}})

    def capture(url, out, diag=None, **kw):
        if url == old:
            diag.update(reason="LOGIN", detail="Synthetic login wall")
            return None
        Image.new("RGB", (80, 60), "blue").save(out)
        diag.update(title=page_title, focus={"after": 12})
        return out

    monkeypatch.setattr(screenshot, "capture", capture)
    monkeypatch.setattr(screenshot, "outline", lambda *a, **kw: [])
    # A usable picture; the independent look did not provide caption words.
    monkeypatch.setattr(ShotLook, "call_claude_json", lambda *a, **kw: (
        {"label": "OK", "why": "A readable product screen."}, ""))
    if mode.startswith("director"):
        monkeypatch.setattr(VisualDirector, "call_claude_json", lambda *a, **kw: ({"insertions": []}, ""))
        state = ArticleState(topic="Widget overview", slug=art.slug)
        state.output_dir, state.research = str(art.dir), art.run()["research"]
        state.frontmatter = {"cover": "assets/cover.png"}
        state.draft_markdown = f'# Widget\n\n## 1. Acme\n\n<!-- visual:screenshot url="{old}" caption="Product overview" -->\n'
        VisualDirector(proj, cfg).run(state)
        entry = next(v for v in state.visuals if v.get("type") == "screenshot")
        assert entry["status"] == "generated" and entry["fallback"] == "alt_page"
        caption = entry["caption"]
    else:
        answer = {"producer": "screenshot", "spec": {"url": old},
                  "candidates": [{"url": substitute}]}
        monkeypatch.setattr(VisualProducerService, "call_claude_json", lambda *a, **kw: (answer, ""))
        service = VisualProducerService(proj, cfg)
        if mode.startswith("studio-create"):
            rep = service.create(art.dir, "Show the product screen", EARLY, LATE)
            assert "error" not in rep, rep
            caption = store.read_json(art.dir / rep["path"].replace(".png", ".json"))["page_title"]
        else:
            Image.new("RGB", (80, 60), "white").save(art.assets / "screenshot_1.png")
            store.write_json(art.assets / "screenshot_1.json", {"producer": "screenshot", "spec": {"url": old}})
            rep = service.edit_visual(art.dir, "assets/screenshot_1.png", "Show the product screen")
            assert rep.get("ok"), rep
            caption = rep["caption"]
    # The vendor prefix is allowed; all words of the page title must survive.
    assert title in caption and " | Acme" not in caption, {"expected_title": title, "actual_caption": caption}


def test_own_marketing_homepage_is_not_kept_as_product_screenshot_fallback(output_dir, monkeypatch):
    from PIL import Image, ImageDraw
    from core import setup
    from core.agents.visual_director import VisualDirector, ShotLook
    from core.producers import screenshot
    from core.workflow_state import ArticleState

    setup.apply(output_dir, name="Acme Widgets", domain="acme.example",
                blog="https://blog.acme.example", audience="Widget users", test=False)
    cfg, proj = config_project()
    art = article(output_dir, [])
    shots, looks = [], []
    original, home = "https://acme.example/product", "https://acme.example/"

    def capture(url, out, diag=None, **kw):
        shots.append(url)
        if url == original:
            diag.update(reason="LOGIN", detail="Synthetic sign-in wall")
            return None
        im = Image.new("RGB", (720, 400), "white")
        draw = ImageDraw.Draw(im)
        draw.text((40, 60), "Acme Widgets: transform your work", fill="black")
        draw.text((40, 140), "Sign up today", fill="blue")
        im.save(out)
        diag.update(title="Acme Widgets | Work better", focus={"after": 4},
                    outline=[{"heading": "Acme Widgets: transform your work", "text": "Sign up today"}])
        return out

    def look(self, prompt, **kw):
        looks.append(prompt)
        # Follows the supplied ShotLook rubric: a clean, readable page under its own title.
        return {"label": "OK", "why": "A clean and readable homepage hero with a sign-up call to action."}, ""

    monkeypatch.setattr(screenshot, "capture", capture)
    monkeypatch.setattr(ShotLook, "call_claude_json", look)
    monkeypatch.setattr(VisualDirector, "call_claude_json", lambda *a, **kw: ({"insertions": []}, ""))
    state = ArticleState(topic="Widget overview", slug=art.slug)
    state.output_dir = str(art.dir)
    state.frontmatter = {"cover": "assets/cover.png"}
    state.research = {"sources": [{"url": home}]}
    state.draft_markdown = f'# Widget guide\n\n## 1. Acme Widgets\n\n<!-- visual:screenshot url="{original}" caption="Acme Widgets product overview" -->\n'
    VisualDirector(proj, cfg).run(state)
    assert shots and shots[0] == original
    assert not any(v.get("status") == "generated" and v.get("url") == home for v in state.visuals), {
        "visuals": state.visuals, "markdown": state.draft_markdown}
