"""The review of 1020ae2 (seo-writer-agent-reviews/refactoring-review-1020ae2.md): its probes for
R102-01..02 kept as tests: words that only resemble the host stay in a caption; a standard port
keeps our own site ours."""
import pytest

from test_review_a54 import article
from test_review_a5d import screenshot_standins, visual_entry
from test_review_a5d_controls import own_project


@pytest.mark.parametrize("mode", ["director", "create", "redo"])
@pytest.mark.parametrize("host,title", [
    ("acme.example", "Pricing plans - Annual Cost"),
    ("acme.example", "Acme pricing - Annual Versus Monthly"),
    ("linear.example", "Performance guide - Nonlinear Growth"),
], ids=["initials-are-not-a-site-name", "brand-in-topic-is-not-a-site-name",
        "substring-is-not-a-site-name"])
def test_r102_01_ambiguous_dash_preserves_all_title_words(output_dir, monkeypatch, mode, host, title):
    art = article(output_dir, [])
    first, replacement = f"https://{host}/private", f"https://{host}/guide"
    shots, looks = screenshot_standins(monkeypatch, title=title, original=first)
    result = visual_entry(monkeypatch, art, mode, first, replacement)
    assert result.get("ok"), result
    assert shots == [first, replacement] and looks
    actual = result["caption"].removeprefix("Acme Widgets: ") if mode == "director" else result["caption"]
    assert actual == title, {"entry": mode, "url": replacement, "title": title, "caption": actual}


@pytest.mark.parametrize("mode", ["director", "create", "redo"])
@pytest.mark.parametrize("candidate", [False, True], ids=["own-product", "own-homepage-fallback"])
def test_r102_02_standard_https_port_does_not_disable_own_site_rule(output_dir, monkeypatch, mode, candidate):
    own_project(output_dir)
    art = article(output_dir, [])
    first = "https://acme.example/private" if candidate else "https://acme.example:443/product"
    replacement = "https://acme.example:443/" if candidate else None
    shots, looks = screenshot_standins(monkeypatch, title="Acme Widgets | Work better", marketing=True,
                                      original=first if candidate else None)
    result = visual_entry(monkeypatch, art, mode, first, replacement, instruction="Show Acme Widgets")
    assert not result.get("ok"), {
        "entry": mode, "result": result, "shots": shots,
        "own_rubric": ["MARKETING -" in p for p in looks],
    }
    if mode == "director" and candidate:
        assert shots == [first], "Own homepage must be excluded before capture"
    elif looks:
        assert all("MARKETING -" in p for p in looks)
    if mode == "redo":
        assert result["unchanged"]
