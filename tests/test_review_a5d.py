"""The review of a5d195e (seo-writer-agent-reviews/refactoring-review-a5d195e.md): its probes for
RA5D-01..04 kept as tests: the site's name in a caption, our own site in the Studio's shots, one
queue with the half-done changes, a $0.00 estimate in the browser's batch."""
import json
import re
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from test_review_a54 import (EARLY, LATE, article, config_project, finding, history_costs,
                             model_edits)
from test_release_controls import cli_json


def screenshot_standins(monkeypatch, *, title, original=None, marketing=False):
    from core.agents.visual_director import ShotLook
    from core.producers import screenshot
    shots, looks = [], []

    def capture(url, out, diag=None, **kw):
        shots.append(url)
        if url == original:
            diag.update(reason="LOGIN", detail="Fixture sign-in wall")
            return None
        im = Image.new("RGB", (720, 400), "white")
        ImageDraw.Draw(im).text((30, 50), "Work better. Sign up today." if marketing else "Plan | Seats | Price", fill="black")
        im.save(out)
        diag.update(title=title, focus={"after": 3}, outline=[])
        return out

    def look(self, prompt, **kw):
        rubric = "\n".join(x.get("text", "") for x in prompt)
        looks.append(rubric)
        # The same scene is refused only under the new own-site rubric. This stand-in
        # exercises real prompt routing, without predicting a live model's behaviour.
        label = "MARKETING" if marketing and "MARKETING -" in rubric else "OK"
        return {"label": label, "why": "A readable hero and CTA." if marketing else "A readable pricing table."}, ""

    monkeypatch.setattr(screenshot, "capture", capture)
    monkeypatch.setattr(screenshot, "outline", lambda *a, **kw: [])
    monkeypatch.setattr(ShotLook, "call_claude_json", look)
    return shots, looks


def visual_entry(monkeypatch, art, mode, first, candidate=None, caption="", instruction="Show Acme Widgets"):
    from core import store
    from core.agents.interactive import VisualProducerService
    from core.agents.visual_director import VisualDirector
    from core.workflow_state import ArticleState
    cfg, proj = config_project()
    cfg.setdefault("pipeline", {}).setdefault("visuals", {})["vision_check"] = True
    if mode == "director":
        monkeypatch.setattr(VisualDirector, "call_claude_json", lambda *a, **kw: ({"insertions": []}, ""))
        state = ArticleState(topic="Widget overview", slug=art.slug)
        state.output_dir, state.research = str(art.dir), {"sources": [{"url": candidate}]} if candidate else {}
        state.frontmatter = {"cover": "assets/cover.png"}
        state.draft_markdown = f'# Widgets\n\n## 1. Acme Widgets\n\n<!-- visual:screenshot url="{first}" caption="{caption or instruction}" -->\n'
        VisualDirector(proj, cfg).run(state)
        entries = [x for x in state.visuals if x.get("type") == "screenshot"]
        assert len(entries) == 1
        return {"ok": entries[0]["status"] == "generated", "caption": entries[0].get("caption"),
                "markdown": state.draft_markdown, "entry": entries[0]}
    data = {"producer": "screenshot", "spec": {"url": first}, "caption": caption, "alt": caption}
    if candidate:
        data["candidates"] = [{"url": candidate}]
    monkeypatch.setattr(VisualProducerService, "call_claude_json", lambda *a, **kw: (data, ""))
    service = VisualProducerService(proj, cfg)
    if mode == "create":
        rep = service.create(art.dir, instruction, EARLY, LATE)
        if "error" not in rep:
            side = store.read_json(art.dir / rep["path"].replace(".png", ".json"))
            rep.update(ok=True, caption=side.get("page_title"))
        return rep
    pic = art.assets / "screenshot_1.png"
    Image.new("RGB", (100, 60), "red").save(pic)
    store.write_json(pic.with_suffix(".json"), {"producer": "screenshot", "spec": {"url": first}})
    before = pic.read_bytes()
    rep = service.edit_visual(art.dir, "assets/screenshot_1.png", instruction)
    rep["unchanged"] = pic.read_bytes() == before
    return rep


@pytest.mark.parametrize("mode", ["director", "create", "redo"])
@pytest.mark.parametrize("raw,expected", [
    ("Pricing plans - Annual Versus Monthly", "Pricing plans - Annual Versus Monthly"),
    ("Pricing | Acme", "Pricing"),
])
def test_caption_preserves_title_clause_and_removes_site_from_one_word_title(output_dir, monkeypatch, mode, raw, expected):
    art = article(output_dir, [])
    old, substitute = "https://vendor.example/private", "https://vendor.example/pricing"
    screenshot_standins(monkeypatch, title=raw, original=old)
    rep = visual_entry(monkeypatch, art, mode, old, substitute)
    assert rep.get("ok"), rep
    actual = rep["caption"]
    if mode == "director":
        actual = actual.removeprefix("Acme Widgets: ")
    assert actual == expected, rep


@pytest.mark.parametrize("mode", ["create", "redo"])
@pytest.mark.parametrize("substitute", [False, True], ids=["own-product-page", "own-homepage-candidate"])
def test_studio_rejects_own_marketing_shots_in_all_capture_paths(output_dir, monkeypatch, mode, substitute):
    from core import setup
    setup.apply(output_dir, name="Acme Widgets", domain="acme.example", blog="https://blog.acme.example",
                audience="Widget users", test=False)
    art = article(output_dir, [])
    first = "https://acme.example/product"
    candidate = "https://acme.example/" if substitute else None
    shots, looks = screenshot_standins(monkeypatch, title="Acme Widgets | Work better", marketing=True,
                                      original=first if substitute else None)
    rep = visual_entry(monkeypatch, art, mode, first, candidate)
    assert shots and looks
    assert not rep.get("ok"), {"result": rep, "shots": shots, "rubric_has_marketing": ["MARKETING -" in p for p in looks]}
    if mode == "redo":
        assert rep["unchanged"]


def test_browser_batch_keeps_a_zero_cent_item_at_zero(studio, output_dir, monkeypatch, capsys):
    from core import agent_door
    from playwright.sync_api import sync_playwright
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-no-real-key")
    art = article(output_dir, [finding("machine", target=EARLY), finding("proposal", target=LATE, kind="fact")])
    # A real nonzero ledger entry rounds to $0.00 in the estimator, as small model calls do.
    history_costs(art, {"fix": .004, "proposals": .21})
    assert agent_door.estimate(output_dir, "fix")["usd"] == 0
    code, rep = cli_json(monkeypatch, capsys, "apply", art.slug, "machine", "proposal",
                         "--version", art.version(), "--review", "r1")
    assert code == 1 and rep["refused"] == "needs_yes" and rep["estimate"]["usd"] == .21, rep
    html = studio.get(f"/a/{art.slug}?tab=review").get_data(as_text=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--single-process", "--no-zygote"], chromium_sandbox=False)
        try:
            page = browser.new_page()
            page.route("**/*", lambda route: route.abort())
            page.set_content(html)
            page.add_script_tag(content=Path("ui/static/js/article_page.js").read_text())
            assert page.locator('input[form=batch][value=machine]').get_attribute("data-cost") == "about $0.00"
            page.locator("[data-select-all]").check()
            shown = page.locator("#apply-ticked").inner_text()
            assert "$0.21" in shown, {"cli": rep["estimate"], "browser": shown}
        finally:
            browser.close()


def test_cli_half_done_critical_precedes_open_minor_as_on_review_page(studio, output_dir, monkeypatch, capsys):
    from core import agent_door, qa_changes as qc, store
    from core.agents.edit_verifier import EditVerifier
    from ui.article_view import review_queue
    import cli
    art = article(output_dir, [finding("critical", "critical", target=EARLY), finding("minor", "minor", target=LATE)])
    model_edits(monkeypatch, {EARLY: EARLY.replace("describes", "explains")})
    monkeypatch.setattr(EditVerifier, "call_claude_json", lambda *a, **kw: (
        {"items": [{"id": "c1", "resolved": False, "why": "The critical problem is still here."}]}, ""))
    rep = qc.fix_again(art.slug, "critical", art.version())
    assert rep["landed"] == ["critical"], rep
    rv = agent_door.review_view(art)
    group = rv["changed"][0]
    assert group["half_done"] and group["severity"] == "critical"
    expected = [group["id"], "minor"]
    queue = [i["id"] for i in review_queue(art, store.split_frontmatter(art.text())[1], rv, {}) if i["group"] == "needs"]
    assert queue == expected
    page = studio.get(f"/a/{art.slug}?tab=review").get_data(as_text=True)
    assert page.index(f'id="f-{group["id"]}"') < page.index('id="f-minor"')
    monkeypatch.setattr(sys, "argv", ["cli.py", "findings", art.slug])
    cli.main()
    output = capsys.readouterr().out
    rows = re.findall(r"^  (?:needs you|changed)\s+(\S+)", output, re.M)
    assert rows == expected, {"web": queue, "cli": output, "agent_needs": [x["id"] for x in rv["needs_you"]]}
