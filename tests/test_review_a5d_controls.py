"""The review of a5d195e: its controls kept as tests (our own pricing page and its link, a
candidate's own caption, a third party's homepage, the batch's prices and order)."""
import re
import sys
from pathlib import Path

import pytest
from PIL import Image

from core import store
from test_review_a54 import EARLY, LATE, article, finding, history_costs
from test_release_controls import cli_json
from test_review_a5d import screenshot_standins, visual_entry


def own_project(root):
    from core import setup
    setup.apply(root, name="Acme Widgets", domain="acme.example", blog="https://blog.acme.example",
                audience="Widget users", test=False)


@pytest.mark.parametrize("mode", ["director", "create", "redo"])
def test_own_product_pricing_screenshot_and_link_survive(output_dir, monkeypatch, mode):
    own_project(output_dir)
    art = article(output_dir, [])
    url = "https://acme.example/pricing"
    shots, looks = screenshot_standins(monkeypatch, title="Widget pricing | Acme")
    rep = visual_entry(monkeypatch, art, mode, url, caption="The product pricing table")
    assert rep.get("ok"), rep
    assert shots == [url]
    words = rep.get("markdown") or rep.get("caption")
    assert f"]({url})" in words, rep


@pytest.mark.parametrize("mode", ["create", "redo"])
def test_candidate_caption_is_not_replaced_by_page_title(output_dir, monkeypatch, mode):
    from core.agents.interactive import VisualProducerService
    from test_review_a54 import config_project
    art = article(output_dir, [])
    first, second = "https://vendor.example/private", "https://vendor.example/pricing"
    screenshot_standins(monkeypatch, title="Title not to use | Vendor", original=first)
    data = {"producer": "screenshot", "spec": {"url": first}, "caption": "Rejected page's words",
            "candidates": [{"url": second, "caption": "The candidate's own pricing table", "alt": "Plans and seats"}]}
    monkeypatch.setattr(VisualProducerService, "call_claude_json", lambda *a, **kw: (data, ""))
    cfg, proj = config_project()
    service = VisualProducerService(proj, cfg)
    if mode == "create":
        rep = service.create(art.dir, "Show pricing", EARLY, LATE)
        assert "error" not in rep
        shown = rep["markdown"]
    else:
        pic = art.assets / "screenshot_1.png"
        Image.new("RGB", (100, 60), "red").save(pic)
        store.write_json(pic.with_suffix(".json"), {"producer": "screenshot", "spec": {"url": first}})
        rep = service.edit_visual(art.dir, "assets/screenshot_1.png", "Show pricing")
        assert rep.get("ok")
        shown = rep["caption"]
    assert "The candidate's own pricing table" in shown and "Title not to use" not in shown and "Rejected page" not in shown


def test_director_own_marketing_label_refuses_without_fact_card(output_dir, monkeypatch):
    own_project(output_dir)
    art = article(output_dir, [])
    shots, looks = screenshot_standins(monkeypatch, title="Acme Widgets | Work better", marketing=True)
    rep = visual_entry(monkeypatch, art, "director", "https://acme.example/product")
    assert not rep["ok"] and rep["entry"]["reason"] == "MARKETING", rep
    assert shots and all("MARKETING -" in p for p in looks)
    assert not list(art.assets.glob("infographic_*.png"))


@pytest.mark.parametrize("first,second", [
    ("https://acme.example/private", "https://acme.example/pricing"),
    ("https://vendor.example/private", "https://vendor.example/"),
])
def test_director_allowed_substitutes_remain_available(output_dir, monkeypatch, first, second):
    own_project(output_dir)
    art = article(output_dir, [])
    shots, looks = screenshot_standins(monkeypatch, title="Product pricing | Vendor", original=first)
    rep = visual_entry(monkeypatch, art, "director", first, second)
    assert rep["ok"] and rep["entry"]["url"] == second and rep["entry"]["fallback"] == "alt_page", rep
    assert shots == [first, second]
    assert ("MARKETING -" in looks[-1]) is ("acme.example" in second)


def test_director_own_homepage_filtered_before_capture_even_when_look_would_accept(output_dir, monkeypatch):
    own_project(output_dir)
    art = article(output_dir, [])
    first, second = "https://acme.example/private", "https://acme.example/"
    shots, looks = screenshot_standins(monkeypatch, title="Widget pricing | Acme", original=first)
    rep = visual_entry(monkeypatch, art, "director", first, second)
    assert not rep["ok"] and shots == [first] and not looks


def test_director_reframes_own_page_to_product_screen(output_dir, monkeypatch):
    from core.agents.visual_director import ShotLook
    from core.producers import screenshot
    own_project(output_dir)
    art = article(output_dir, [])
    headings, prompts = [], []

    def capture(url, out, diag=None, heading=None, **kw):
        headings.append(heading)
        Image.new("RGB", (100, 60), "red" if not heading else "blue").save(out)
        diag.update(title="Our product | Acme", outline=[{"heading": "Pricing", "text": "Plans and seats"}],
                    focus={"after": 3, "heading": heading})
        return out

    def look(self, prompt, **kw):
        text = " ".join(x.get("text", "") for x in prompt)
        prompts.append(text)
        return ({"label": "REFRAME", "heading": "Pricing", "why": "The table is below"} if len(prompts) == 1
                else {"label": "OK", "why": "The pricing table is now shown"}), ""

    monkeypatch.setattr(screenshot, "capture", capture)
    monkeypatch.setattr(ShotLook, "call_claude_json", look)
    rep = visual_entry(monkeypatch, art, "director", "https://acme.example/product", caption="The pricing table")
    assert rep["ok"] and headings == [None, "Pricing"], rep
    assert all("MARKETING -" in p for p in prompts)


def test_open_findings_priority_matches_page_json_and_plain_cli_including_unknown(studio, output_dir, monkeypatch, capsys):
    from core import agent_door
    from ui.article_view import review_queue
    import cli
    art = article(output_dir, [
        finding("nit", "nitpick", target=EARLY),
        finding("missing", "major", target="A missing passage"),
        finding("late", "major", target=LATE),
        finding("unknown", "unknown", target=EARLY),
        finding("minor", "minor", target=EARLY),
        finding("critical", "critical", target=LATE)])
    expected = ["critical", "unknown", "late", "missing", "minor", "nit"]
    rv = agent_door.review_view(art)
    assert [x["id"] for x in rv["needs_you"]] == expected
    queue = review_queue(art, store.split_frontmatter(art.text())[1], rv, {})
    assert [x["id"] for x in queue if x["group"] == "needs"] == expected
    html = studio.get(f"/a/{art.slug}?tab=review").get_data(as_text=True)
    positions = [html.index(f'id="f-{i}"') for i in expected]
    assert positions == sorted(positions)
    code, rep = cli_json(monkeypatch, capsys, "findings", art.slug)
    assert code == 0 and [x["id"] for x in rep["needs_you"]] == expected
    monkeypatch.setattr(sys, "argv", ["cli.py", "findings", art.slug])
    cli.main()
    assert re.findall(r"^  needs you\s+(\S+)", capsys.readouterr().out, re.M) == expected


@pytest.mark.parametrize("fix_cost", [.73, 1.26])
@pytest.mark.parametrize("remove", [False, True])
def test_cli_batch_history_and_free_removal_and_no_execution_before_yes(output_dir, monkeypatch, capsys, fix_cost, remove):
    from core import qa_changes as qc
    art = article(output_dir, [finding("machine", target=EARLY), finding("proposal", target=LATE, kind="fact"),
                              finding("remove", action="remove_picture", kind="picture", target="assets/chart_1.png"),
                              finding("add", action="add_picture", kind="picture", target=EARLY)])
    history_costs(art, {"fix": fix_cost, "proposals": .21, "visual_insert": .08})
    monkeypatch.setattr(qc, "apply_proposals", lambda *a, **kw: pytest.fail("Executed before consent"))
    ids = ["machine", "proposal", "add"] + (["remove"] if remove else [])
    code, rep = cli_json(monkeypatch, capsys, "apply", art.slug, *ids, "--version", art.version(), "--review", "r1")
    assert code == 1 and rep["refused"] == "needs_yes"
    assert rep["estimate"]["usd"] == round(fix_cost + .21 + .08, 2), rep
