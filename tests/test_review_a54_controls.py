"""The review of a549d73: its controls kept as tests (the batch of ticks through the real page,
the CLI and the actions; the new picture defects; our place in a roundup reaching the editor and
the fixer; the infographic kinds). Offline: the model and the renderers are stand-ins."""
import json
import re
from pathlib import Path

import pytest
from werkzeug.datastructures import MultiDict

from test_release_controls import cli_json
from test_review_a54 import BODY, EARLY, LATE, article, config_project, finding, history_costs, model_edits


def run_http_batch(studio, art, ids, monkeypatch, *, version=None, review="r1"):
    from ui.jobs import runner
    queued = []
    monkeypatch.setattr(runner, "submit_on_article",
                        lambda art, label, fn, kind, version, **kw: queued.append(fn) or "local-job")
    data = MultiDict([("version", version or art.version()), ("review", review)]
                     + [("ids", i) for i in ids])
    response = studio.post(f"/a/{art.slug}/apply", data=data)
    assert response.status_code == 302
    return queued


def test_mixed_batch_lands_machine_and_proposal_once_and_closes_already_done(studio, output_dir, monkeypatch):
    from core import qa_changes as qc, versions
    from core.agents.fix_applier import FixApplier
    from core.agents.edit_verifier import EditVerifier
    monkeypatch.setenv("ANTHROPIC_API_KEY", "reviewer-dummy")
    fs = [finding("machine", target=EARLY),
          finding("proposal", kind="fact", target=LATE),
          finding("done", kind="keyword", action="insert_keyword", target="Last section", phrase="widget guide"),
          finding("remove", kind="picture", action="remove_picture", target="assets/chart_1.png"),
          finding("outline", kind="structure", action="outline", target="First section"),
          finding("choice", kind="picture", action="redraw", target="assets/chart_1.png",
                  needs_choice="Pick one.", choices=[{"action": "remove_picture", "text": "Remove."},
                                                     {"action": "redraw", "text": "Redraw."}])]
    art = article(output_dir, fs)
    before_image = art.path("assets/chart_1.png").read_bytes()
    calls = model_edits(monkeypatch, {EARLY: EARLY.replace("describes", "explains"),
                                     LATE: LATE.replace("choice", "decision")})
    queued = run_http_batch(studio, art, ["machine", "proposal", "done", "outline", "choice", "remove", "machine"], monkeypatch)
    assert len(queued) == 1
    result = queued[0](lambda *a: None)
    assert "3 made" in result["step"] and "1 already done" in result["step"], result
    rv = qc.records(art)[1]
    by_id = {f["id"]: f for f in rv["findings"]}
    assert {i: by_id[i]["status"] for i in ("machine", "proposal", "done", "remove", "outline", "choice")} == {
        "machine": "changed", "proposal": "changed", "done": "not_important", "remove": "changed",
        "outline": "open", "choice": "open"}
    assert by_id["done"]["closed"]["already"] is True
    assert by_id["machine"]["tried"] == 1
    assert len(calls) == 2
    assert "assets/chart_1.png" not in art.text()
    assert art.path("assets/chart_1.png").read_bytes() == before_image
    assert [g["findings"] for g in rv["changes"]] == [["machine"], ["proposal"], ["remove"]]
    index = {v["group"]: v for v in versions.index(art) if v.get("group")}
    assert [index[g["id"]]["kind"] for g in rv["changes"]] == ["fix", "proposal", "proposal"]
    assert EARLY.replace("describes", "explains") in art.text()
    assert LATE.replace("choice", "decision") in art.text()


@pytest.mark.parametrize("stale", ["version", "review", "foreign_finding", "unknown_finding"])
def test_batch_rejects_stale_or_foreign_ticks_before_any_model_call(studio, output_dir, monkeypatch, stale):
    from core import qa_changes as qc
    monkeypatch.setenv("ANTHROPIC_API_KEY", "reviewer-dummy")
    foreign = finding("foreign")
    foreign["review"] = "r0"
    art = article(output_dir, [finding("machine", target=EARLY), foreign])
    before = art.text()
    calls = model_edits(monkeypatch, {EARLY: EARLY.replace("describes", "explains")})
    ids = ["machine"] + (["foreign"] if stale == "foreign_finding" else
                         ["missing"] if stale == "unknown_finding" else [])
    queued = run_http_batch(studio, art, ids, monkeypatch,
                            version="old-version" if stale == "version" else art.version(),
                            review="r0" if stale == "review" else "r1")
    assert len(queued) == 1
    with pytest.raises(qc.Stale):
        queued[0](lambda *a: None)
    assert art.text() == before and not calls
    assert not qc.records(art)[1]["changes"]


def test_batch_machine_scope_is_not_widened_when_an_earlier_tick_changes_target(studio, output_dir, monkeypatch):
    from core import qa_changes as qc
    monkeypatch.setenv("ANTHROPIC_API_KEY", "reviewer-dummy")
    art = article(output_dir, [finding("first", target=EARLY), finding("second", target=EARLY)])
    calls = model_edits(monkeypatch, {EARLY: EARLY.replace("describes", "explains")})
    queued = run_http_batch(studio, art, ["first", "second"], monkeypatch)
    queued[0](lambda *a: None)
    rv = qc.records(art)[1]
    fs = {f["id"]: f for f in rv["findings"]}
    assert fs["first"]["status"] == "changed" and fs["second"]["status"] == "open"
    assert len(calls) == 1
    assert len(rv["changes"]) == 1


@pytest.mark.parametrize("selection,ai,images,expected", [
    (["remove"], False, False, True),
    (["remove", "text"], False, False, False),
    (["remove", "text"], True, False, True),
    (["remove", "cover"], True, False, False),
    (["remove", "cover"], False, True, True),
    (["text", "cover"], True, False, False),
    (["text", "cover"], True, True, True),
    (["illustration", "text"], True, False, False),
    (["illustration", "text"], True, True, True),
])
def test_batch_keys_are_checked_for_every_selected_item(studio, output_dir, monkeypatch, selection, ai, images, expected):
    from PIL import Image
    from core import store
    art = article(output_dir, [
        finding("remove", action="remove_picture", kind="picture", target="assets/chart_1.png"),
        finding("text", target=EARLY),
        finding("cover", action="redraw", kind="picture", target="assets/cover.png"),
        finding("illustration", action="redraw", kind="picture", target="assets/image_1.png")])
    Image.new("RGB", (80, 60), "white").save(art.assets / "cover.png")
    Image.new("RGB", (80, 60), "white").save(art.assets / "image_1.png")
    store.write_json(art.assets / "image_1.json", {"producer": "image", "spec": {"prompt": "Illustration"}})
    if ai:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "reviewer-dummy")
    if images:
        monkeypatch.setenv("OPENAI_API_KEY", "reviewer-dummy")
    queued = run_http_batch(studio, art, selection, monkeypatch)
    assert bool(queued) is expected


def test_a_free_removal_does_not_need_keys_and_changes_the_article(studio, output_dir, monkeypatch):
    from core import qa_changes as qc
    art = article(output_dir, [finding("remove", action="remove_picture", kind="picture", target="assets/chart_1.png")])
    # Removal check may ask the verifier; ensure even an accidental paid call is replaced.
    model_edits(monkeypatch, {})
    queued = run_http_batch(studio, art, ["remove"], monkeypatch)
    assert len(queued) == 1
    result = queued[0](lambda *a: None)
    assert result["step"] == "1 made"
    assert "assets/chart_1.png" not in art.text()
    assert qc.records(art)[1]["findings"][0]["status"] == "changed"


@pytest.mark.parametrize("keys", [True, False])
def test_browser_ticks_use_each_price_and_key_state(studio, output_dir, monkeypatch, keys):
    from playwright.sync_api import sync_playwright
    from ui import article_view
    if keys:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "reviewer-dummy")
    art = article(output_dir, [
        finding("machine", target=EARLY),
        finding("proposal", kind="fact", target=LATE),
        finding("remove", action="remove_picture", kind="picture", target="assets/chart_1.png"),
        finding("choice", kind="picture", action="redraw", target="assets/chart_1.png",
                needs_choice="Pick one.", choices=[{"action": "remove_picture", "text": "Remove."}]),
        finding("outline", action="outline", kind="structure", target="First section")])
    history_costs(art, {"fix": .73, "proposals": .21, "visual_insert": .08})
    html = studio.get(f"/a/{art.slug}?tab=review").get_data(as_text=True)
    script = Path("ui/static/js/article_page.js").read_text()
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--single-process", "--no-zygote"],
                                    chromium_sandbox=False)
        try:
            page = browser.new_page()
            page.route("**/*", lambda route: route.abort())
            page.set_content(html)
            page.add_script_tag(content=script)
            ticks = page.locator('input[name=ids][form=batch]')
            assert ticks.count() == 3
            page.locator('[data-select-all]').check()
            button = page.locator("#apply-ticked")
            assert "Make the 3 ticked changes" in button.inner_text()
            assert "$0.94" in button.inner_text()
            assert button.is_disabled() is (not keys)
            page.locator('input[name=ids][form=batch][value=machine]').uncheck()
            page.locator('input[name=ids][form=batch][value=proposal]').uncheck()
            assert button.is_enabled() and "$" not in button.inner_text()
        finally:
            browser.close()


def test_severity_numbering_and_article_marks_remain_consistent(studio, output_dir):
    from core import agent_door, store
    from ui.article_view import review_queue, shown_marks
    art = article(output_dir, [
        finding("minor", "minor", target=EARLY),
        finding("critical", "critical", target=LATE)])
    rv = agent_door.review_view(art)
    body = store.split_frontmatter(art.text())[1]
    items = review_queue(art, body, rv, {})
    assert [(i["id"], i["n"]) for i in items] == [("critical", 1), ("minor", 2)]
    assert shown_marks(body, items) == {1, 2}
    review = studio.get(f"/a/{art.slug}?tab=review").get_data(as_text=True)
    marked = studio.get(f"/a/{art.slug}?tab=article&marks=1").get_data(as_text=True)
    assert 'marks=1#mark-1' in review and 'marks=1#mark-2' in review
    assert 'id="mark-1"' in marked and 'id="mark-2"' in marked
    assert rv["counts"]["needs_you"] == 2
    assert "2 need you" in review and "2 need you" in marked


def test_a_half_done_change_group_uses_its_worst_finding_severity(output_dir, monkeypatch):
    from core import agent_door, qa_changes as qc, store
    from core.agents.edit_verifier import EditVerifier
    from ui.article_view import review_queue
    second = "Another sentence describes the process."
    art = article(output_dir, [
        finding("minor", "minor", target=EARLY),
        finding("critical", "critical", target=second),
        finding("major-outline", "major", action="outline", kind="structure", target="Last section")],
        BODY.replace(EARLY, EARLY + " " + second))
    model_edits(monkeypatch, {EARLY: EARLY.replace("describes", "explains"),
                             second: second.replace("describes", "explains")})
    monkeypatch.setattr(EditVerifier, "call_claude_json", lambda *a, **kw: (
        {"items": [{"id": "c1", "resolved": False, "why": "Still needs an editorial decision."}]}, ""))
    cfg, proj = config_project()
    with art.lock(wait=False, holder="reviewer"):
        run, rv = qc.records(art)
        work = qc.Work(art, run, rv, cfg, proj)
        rep = qc.apply_safe(work, rv["reviews"][-1])
    assert rep["landed"] == ["minor", "critical"], rep
    rv = agent_door.review_view(art)
    assert rv["changed"][0]["findings"] == ["minor", "critical"]
    assert rv["changed"][0]["severity"] == "critical" and rv["changed"][0]["half_done"]
    queue = review_queue(art, store.split_frontmatter(art.text())[1], rv, {})
    assert [(i["id"], i["severity"]) for i in queue if i["group"] == "needs"] == [
        ("c1", "critical"), ("major-outline", "major")]


@pytest.mark.parametrize("defect,producer", [("text_only", "infographic"), ("marketing", "screenshot")])
def test_new_picture_defects_survive_real_picture_check_and_route_to_removal(output_dir, monkeypatch, defect, producer):
    from core import qa_model as qm, store
    from core.agents.visual_checker import VisualChecker, pictures
    art = article(output_dir, [])
    store.write_json(art.assets / "chart_1.json", {"producer": producer, "spec": {}})
    cfg, proj = config_project()
    monkeypatch.setattr(VisualChecker, "call_claude_json", lambda *a, **kw: ({
        "ok": False, "problems": [{"severity": "Major", "defect": defect,
                                  "problem": "The picture contains a marketing banner." if defect == "marketing"
                                             else "The picture contains only sentences in boxes.",
                                  "action": "remove", "what": ""}]}, ""))
    pic = next(p for p in pictures(BODY, article_dir=art.dir) if p["path"] == "assets/chart_1.png")
    checker = VisualChecker(proj, cfg)
    result = checker.check_one(art.dir, pic)
    assert result["verdict"] == "problems" and result["problems"][0]["defect"] == defect
    answer = qm.picture_answer(pic["path"], result["problems"][0]["action"])
    f = qm.finding(id="picture", review="r1", source="picture_check", text="Remove this picture.",
                   severity="major", **answer)
    assert qm.route(f, art.text(), art.cover())[0] == "proposal" and f["action"] == "remove_picture"


@pytest.mark.parametrize("name,domain", [("Acme Widgets", "acme.example"), ("Northwind Payroll", "northwind.example")])
def test_own_places_reach_editor_and_fixer_with_project_neutral_prompts(output_dir, monkeypatch, name, domain):
    from core import setup, qa_changes as qc
    from core.agents.editor import Editor
    from core.agents.fix_applier import FixApplier
    from core.agents.brief_generator import BriefGenerator
    from core.agents.copywriter import Copywriter
    from core.agents.interactive import VisualProducerService
    from core.agents.visual_checker import VisualChecker
    from core.workflow_state import ArticleState
    setup.apply(output_dir, name=name, domain=domain, blog="https://blog." + domain,
                audience="Small teams", texts={"product_facts": f"{name} serves small teams."}, test=False)
    cfg, proj = config_project()
    # Three separate roundups. The last covers a kind of product this project does not make.
    body = "# Tools\n\n" + "\n\n".join(
        f"## {heading}\n\nIntro.\n\n" + "\n\n".join(f"### {i}. {n}\n\n{EARLY if i == 1 and heading == 'Best tools' else 'Descriptions.'}"
                                                     for i, n in enumerate(names, 1))
        for heading, names in [("Best tools", [name, "Bolt", "Crate"]),
                               ("Alternative tools", ["Bolt", "Crate", name]),
                               ("Best kitchen tools", ["Spoon", "Fork", "Knife"])])
    art = article(output_dir, [finding("edit", target=EARLY)], body)
    state = ArticleState(topic="Tools", slug=art.slug)
    state.output_dir, state.draft_markdown = str(art.dir), body
    captured = []

    def panel(self, spec, base, tail="", **kw):
        captured.append((self.system_prompt(), base, tail))
        return json.dumps({"decision": "pass", "required_fixes": []})

    monkeypatch.setattr(Editor, "_panel_read", panel)
    Editor(proj, cfg).collect(state)
    assert [x["place"] for x in state.structure["own_places"]] == [1, 3, None]
    assert captured and "OUR ENTRY: #3" in captured[0][1]
    assert "OUR ENTRY: not among" in captured[0][1]
    calls = model_edits(monkeypatch, {EARLY: EARLY.replace("describes", "explains")})
    rep = qc.fix_again(art.slug, "edit", art.version(), config=cfg, project=proj)
    assert rep["landed"] == ["edit"], rep
    assert calls and "OUR ENTRY: #3" in calls[0][0]
    for cls in (BriefGenerator, Copywriter, Editor, VisualProducerService, VisualChecker):
        prompt = " ".join(cls(proj, cfg).system_prompt().split())
        assert name in prompt
        assert not re.search(r"palabra|coval", prompt, re.I)
        assert "{{" not in prompt
    brief = " ".join(BriefGenerator(proj, cfg).system_prompt().split())
    writer = " ".join(Copywriter(proj, cfg).system_prompt().split())
    editor = " ".join(Editor(proj, cfg).system_prompt().split())
    assert "one of our products is" in brief and "honestly leads on" in brief
    assert "wherever one of our products answers the article's question" in writer
    assert "A section whose subject we do not serve says nothing about us" in writer
    assert "Where one of our products answers the article's question" in editor
    assert "honestly ranks" in editor
    for cls in (BriefGenerator, Copywriter, VisualProducerService, VisualChecker):
        prompt = " ".join(cls(proj, cfg).system_prompt().split())
        assert domain in prompt
        assert "product itself" in prompt


@pytest.mark.parametrize("kind,kept", [("flow", True), ("decision", True), ("stats", True),
                                     ("cards", False), ("checklist", False)])
def test_real_director_run_only_enriches_with_structured_infographics(output_dir, monkeypatch, kind, kept):
    from PIL import Image
    from core.agents.visual_director import VisualDirector
    from core.producers import infographic
    from core.workflow_state import ArticleState
    art = article(output_dir, [])
    cfg, proj = config_project()
    spec = {"type": kind, "title": "How it works", "steps": [{"label": x} for x in ("Plan", "Build", "Check")],
            "branches": [{"if": "Small", "then": "One"}, {"if": "Large", "then": "Many"}],
            "items": [{"value": "42", "label": "Widgets"}, {"value": "15", "label": "Teams"}]}
    calls = []

    def render(spec, out, **kw):
        calls.append(spec)
        Image.new("RGB", (80, 60), "blue").save(out)
        return out

    monkeypatch.setattr(infographic, "render", render)
    monkeypatch.setattr(VisualDirector, "call_claude_json", lambda *a, **kw: ({
        "insertions": [{"type": "infographic", "after_heading": "How it works", "spec": spec}]}, ""))
    state = ArticleState(topic="Widgets", slug=art.slug)
    state.output_dir = str(art.dir)
    state.frontmatter = {"cover": "assets/cover.png"}
    state.draft_markdown = "# Widget guide\n\n## How it works\n\n" + "Words about widgets. " * 400
    VisualDirector(proj, cfg).run(state)
    assert bool(calls) is kept
    assert ("assets/infographic_" in state.draft_markdown) is kept


@pytest.mark.parametrize("kind", ["cards", "checklist"])
def test_studio_still_accepts_explicit_cards_and_checklist_requests(output_dir, monkeypatch, kind):
    from PIL import Image
    from core.agents.interactive import VisualProducerService
    from core.producers import infographic
    art = article(output_dir, [])
    cfg, proj = config_project()
    seen = []

    def answer(self, user, **kw):
        seen.append((self.system_prompt(), user))
        return {"producer": "infographic", "spec": {"type": kind, "title": "Widget checks",
                                                   "items": ["Plan", "Build", "Check"]},
                "caption": "Widget checks", "alt": "Widget checks"}, ""

    def render(spec, out, **kw):
        Image.new("RGB", (80, 60), "blue").save(out)
        return out

    monkeypatch.setattr(VisualProducerService, "call_claude_json", answer)
    monkeypatch.setattr(infographic, "render", render)
    rep = VisualProducerService(proj, cfg).create(art.dir, f"Make a {kind} infographic.", EARLY, LATE)
    assert "error" not in rep, rep
    assert "only when the user asks for them" in " ".join(seen[0][0].split())
    assert f"Make a {kind} infographic." in seen[0][1]
    assert (art.dir / rep["path"]).is_file()
