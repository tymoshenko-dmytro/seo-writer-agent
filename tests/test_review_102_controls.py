"""The review of 1020ae2: its controls kept as tests (round 2's acceptance and the six items of the
testers' feedback, offline)."""
import re
import sys
from pathlib import Path

import pytest
from PIL import Image

from core import agent_door, qa_changes as qc, store
from core.agents.edit_verifier import EditVerifier
from core.agents.interactive import VisualProducerService
from core.agents.visual_director import ShotLook
from core.producers import screenshot
from test_release_controls import cli_json
from test_review_a54 import EARLY, LATE, article, config_project, finding, history_costs, model_edits
from test_review_a5d import screenshot_standins, visual_entry
from test_review_a5d_controls import own_project


@pytest.mark.parametrize("mode", ["director", "create", "redo"])
@pytest.mark.parametrize("host,raw,want", [
    ("vendor.example", "Pricing plans - annual versus monthly", "Pricing plans - annual versus monthly"),
    ("vendor.example", "Pricing plans - Annual Versus Monthly", "Pricing plans - Annual Versus Monthly"),
    ("vendor.example", "Pricing | Acme", "Pricing"),
    ("acme.example", "Home - Acme", "Home"),
    ("example-vendor.example", "Example Vendor | Pricing plans", "Pricing plans"),
    ("atanet.org", "Translation and Interpreting - American Translators Association", "Translation and Interpreting"),
    ("vendor.example", "Pricing plans - Annual Versus Monthly | Acme", "Pricing plans - Annual Versus Monthly"),
    ("vendor.example", "How teams " + "share reports across regions " * 12 + "| Acme",
     "How teams " + "share reports across regions " * 11 + "share reports across regions"),
])
def test_caption_acceptance_through_all_three_entries(output_dir, monkeypatch, mode, host, raw, want):
    art = article(output_dir, [])
    first, replacement = f"https://{host}/private", f"https://{host}/pricing"
    screenshot_standins(monkeypatch, title=raw, original=first)
    rep = visual_entry(monkeypatch, art, mode, first, replacement)
    assert rep.get("ok"), rep
    actual = rep["caption"].removeprefix("Acme Widgets: ") if mode == "director" else rep["caption"]
    assert actual == want, rep
    assert "…" not in actual


@pytest.mark.parametrize("mode", ["create", "redo"])
@pytest.mark.parametrize("scenario", ["explicit-home", "candidate-reframe", "refuse-all", "third-party-home"])
def test_studio_own_rule_request_reframe_and_refusal_bytes(output_dir, monkeypatch, mode, scenario):
    own_project(output_dir)
    art = article(output_dir, [])
    cfg, proj = config_project()
    cfg.setdefault("pipeline", {}).setdefault("visuals", {})["vision_check"] = True
    service = VisualProducerService(proj, cfg)
    first = "https://vendor.example/" if scenario == "third-party-home" else "https://acme.example/"
    if scenario in ("candidate-reframe", "refuse-all"):
        first = "https://acme.example/private"
    candidate = "https://acme.example/product"
    request = ("Show our homepage hero and sign-up banner."
               if scenario == "explicit-home" else "Show the pricing table.")
    answer = {"producer": "screenshot", "spec": {"url": first}, "caption": "Pricing table", "alt": "Pricing table"}
    if scenario in ("candidate-reframe", "refuse-all"):
        answer["candidates"] = [{"url": candidate}]
    monkeypatch.setattr(VisualProducerService, "call_claude_json", lambda *a, **kw: (answer, ""))
    monkeypatch.setattr(screenshot, "outline", lambda *a, **kw: [])
    captures, rubrics = [], []

    def capture(url, out, diag=None, heading=None, **kw):
        captures.append((url, heading))
        if url.endswith("/private"):
            diag.update(reason="LOGIN", detail="Fixture only")
            return None
        Image.new("RGB", (100, 60), "blue" if heading else "green").save(out)
        diag.update(title="Product pricing | Acme", outline=[{"heading": "Pricing", "text": "Plans and seats"}],
                    focus={"after": 5, "heading": heading})
        return str(out)

    def look(self, prompt, **kw):
        text = "\n".join(x.get("text", "") for x in prompt)
        rubrics.append(text)
        assert request in text
        if scenario == "third-party-home":
            assert "MARKETING -" not in text
        else:
            assert "MARKETING -" in text
            assert "and the person did not ask for that section" in text
        if scenario == "refuse-all":
            return {"label": "MARKETING", "why": "Hero only, no product block"}, ""
        if scenario == "candidate-reframe" and len(rubrics) == 1:
            return {"label": "REFRAME", "heading": "Pricing", "why": "The requested table is below"}, ""
        return {"label": "OK", "why": "The requested section is visible"}, ""

    monkeypatch.setattr(screenshot, "capture", capture)
    monkeypatch.setattr(ShotLook, "call_claude_json", look)
    pic = art.assets / "screenshot_1.png"
    if mode == "redo":
        Image.new("RGB", (100, 60), "red").save(pic)
        store.write_json(pic.with_suffix(".json"), {"producer": "screenshot", "spec": {"url": first}})
        old_png, old_side = pic.read_bytes(), pic.with_suffix(".json").read_bytes()
        rep = service.edit_visual(art.dir, "assets/screenshot_1.png", request)
    else:
        rep = service.create(art.dir, request, EARLY, LATE)
    assert rubrics
    if scenario == "refuse-all":
        assert "error" in rep, rep
        if mode == "redo":
            assert pic.read_bytes() == old_png and pic.with_suffix(".json").read_bytes() == old_side
        else:
            assert not pic.exists() and not pic.with_suffix(".json").exists()
    else:
        assert "error" not in rep, rep
        if scenario == "candidate-reframe":
            assert captures == [(first, None), (candidate, None), (candidate, "Pricing")]
        else:
            assert captures == [(first, None)]
        words = rep.get("markdown") or rep.get("caption")
        if scenario != "third-party-home":
            assert f"]({candidate if scenario == 'candidate-reframe' else first})" in words
    assert not list(art.assets.glob("*.next.png"))


@pytest.mark.parametrize("kind,finish", [
    ("text", "keep"), ("text", "adjust"), ("text", "revert"),
    ("picture", "keep"), ("picture", "revert"),
])
def test_common_queue_with_real_group_and_lifecycle(studio, output_dir, monkeypatch, capsys, kind, finish):
    import cli
    from core.agents.visual_checker import VisualChecker
    from core.producers import charts
    from ui.article_view import review_queue

    target = "assets/chart_1.png" if kind == "picture" else EARLY
    change = finding("changing", "major", kind="picture" if kind == "picture" else "text",
                     action="redraw" if kind == "picture" else "edit", target=target)
    art = article(output_dir, [finding("nit", "nitpick", target=EARLY), finding("late", "major", target=LATE),
                              change, finding("critical", "critical", target=LATE)])
    replacement = EARLY.replace("describes", "explains")
    model_edits(monkeypatch, {EARLY: replacement})
    monkeypatch.setattr(EditVerifier, "call_claude_json", lambda *a, **kw: (
        {"items": [{"id": "c1", "resolved": False, "why": "Still needs attention"}]}, ""))
    if kind == "picture":
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
                                      "problem": "Labels remain unreadable", "action": "redraw", "what": "Larger labels"}]}, ""))
    rep = qc.fix_again(art.slug, "changing", art.version())
    assert rep["landed"] == ["changing"], rep
    expected = ["critical", "c1", "late", "nit"]

    def assert_interfaces(wanted):
        rv = agent_door.review_view(art)
        assert [i["id"] for i in rv["queue"]] == wanted
        assert rv["counts"]["needs_you"] == len(wanted)
        body = store.split_frontmatter(art.text())[1]
        cards = [i for i in review_queue(art, body, rv, {}) if i["group"] == "needs"]
        assert [i["id"] for i in cards] == wanted
        assert [i["n"] for i in cards] == list(range(1, len(cards) + 1))
        page = studio.get(f"/a/{art.slug}?tab=review").get_data(as_text=True)
        positions = [page.index(f'id="f-{fid}"') for fid in wanted]
        assert positions == sorted(positions)
        for n, fid in enumerate(wanted, 1):
            assert re.search(rf'<article class="ritem[^"]*" id="item-{n}">\s*<span class="anchor" id="f-{fid}"', page)
        code, data = cli_json(monkeypatch, capsys, "findings", art.slug)
        assert code == 0 and [i["id"] for i in data["queue"]] == wanted
        monkeypatch.setattr(sys, "argv", ["cli.py", "findings", art.slug])
        cli.main()
        plain = capsys.readouterr().out
        assert re.findall(r"^  needs you\s+(\S+)", plain, re.M) == wanted
        if "c1" in wanted:
            assert "half_done" in plain and "problem is still there" in plain
        return cards

    cards = assert_interfaces(expected)
    assert cards[1]["at"] is not None and cards[1]["section"] == "First section"
    if finish == "keep":
        qc.keep_as_is(art.slug, "c1", art.version(), who="Reviewer fixture")
    elif finish == "adjust":
        qc.edit_block(art.slug, art.version(), replacement, replacement.replace("widget", "widget clearly"))
    else:
        qc.revert(art.slug, "c1", art.version())
    assert_interfaces(["critical", "changing", "late", "nit"] if finish == "revert" else ["critical", "late", "nit"])


def test_all_zero_estimates_stay_zero_in_browser_and_cli(studio, output_dir, monkeypatch, capsys):
    from playwright.sync_api import sync_playwright
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-only")
    art = article(output_dir, [finding("machine", target=EARLY), finding("proposal", kind="fact", target=LATE)])
    history_costs(art, {"fix": .004, "proposals": .003})
    code, result = cli_json(monkeypatch, capsys, "apply", art.slug, "machine", "proposal",
                            "--version", art.version(), "--review", "r1")
    assert code == 1 and result["refused"] == "needs_yes" and result["estimate"]["usd"] == 0
    page_html = studio.get(f"/a/{art.slug}?tab=review").get_data(as_text=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--single-process", "--no-zygote"], chromium_sandbox=False)
        try:
            page = browser.new_page()
            page.route("**/*", lambda route: route.abort())
            page.set_content(page_html)
            page.add_script_tag(content=Path("ui/static/js/article_page.js").read_text())
            assert page.locator("input[form=batch]").evaluate_all(
                "(items) => items.map(i => i.dataset.cost)") == ["about $0.00", "about $0.00"]
            page.locator("[data-select-all]").check()
            assert page.locator("#apply-ticked").is_enabled()
            assert page.locator("#apply-ticked .btn-cost").count() == 0
            assert "2 ticked changes" in page.locator("#apply-ticked").inner_text()
        finally:
            browser.close()
