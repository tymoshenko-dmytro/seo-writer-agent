"""The review of c91879d (seo-writer-agent-reviews/refactoring-review-c91879d.md), its probes kept as
tests (RC91-01..06). All data are synthetic."""
from __future__ import annotations

import json
import os
import py_compile
from pathlib import Path

import pytest
import standin

SOURCE = '''\
from __future__ import annotations
import json
from dataclasses import dataclass

NAME = "ledger"
KEEP = ("_ledger.json",)

@dataclass
class Row:
    label: str
    value: int

def gate(article_dir, topic, keyword, project):
    p = article_dir / "_ledger.json"
    if p.exists():
        return "off" if json.loads(p.read_text())["off"] else "pinned"
    row = Row("Widget", 812)
    off = "outside" in topic.lower()
    p.write_text(json.dumps({"off": off, "text": f"{row.label} {row.value} ms"}))
    return "off" if off else "on"

def corpus_block(article_dir, project):
    p = article_dir / "_ledger.json"
    if not p.exists():
        return ""
    saved = json.loads(p.read_text())
    return "" if saved["off"] else "LEDGER-v1: " + saved["text"] + " [source: https://vendor.example/board]"

def leaves_out(article_dir, url):
    return url == "https://vendor.example/board"
'''


def prepare(tmp_path, monkeypatch, source=SOURCE):
    from core.config import load_core_config, load_project
    root = tmp_path / "project"
    root.mkdir()
    (root / "profile.yaml").write_text('name: Test Project\nbrand:\n  domains: [northwind.example]\nlinks:\n  site_sitemap: ""\n')
    if source is not None:
        (root / "research_sources").mkdir()
        (root / "research_sources" / "ledger.py").write_text(source)
    monkeypatch.setenv("CONTENT_PROJECT_DIR", str(root))
    monkeypatch.setenv("CONTENT_OUTPUT_DIR", str(tmp_path / "out"))
    standin.install(monkeypatch)
    cfg = load_core_config()
    return root, cfg, load_project(core=cfg)


def writer_state(tmp_path, cfg, proj, label="article"):
    from core.agents.copywriter import Copywriter
    from core.workflow_state import ArticleState
    out = tmp_path / label
    out.mkdir()
    st = ArticleState(topic=standin.TOPIC, slug=label)
    st.output_dir = str(out)
    st.keyword_research = json.loads(json.dumps(standin.KEYWORD_RESEARCH))
    st.brief = json.loads(json.dumps(standin.BRIEF))
    st.research = json.loads(json.dumps(standin.RESEARCH))
    return Copywriter(proj, cfg), st


def cached_article(tmp_path):
    from core import store
    art = store.Repository(tmp_path / "out").reserve("original")
    art.save_run({"schema": 3, "topic": standin.TOPIC, "keyword_research": standin.KEYWORD_RESEARCH,
                  "brief": standin.BRIEF, "research": standin.RESEARCH})
    return art


def test_rc91_excluded_page_is_not_a_screenshot_fallback(tmp_path, monkeypatch):
    from core.agents.visual_director import VisualDirector
    from core.workflow_state import ArticleState
    _, cfg, project = prepare(tmp_path, monkeypatch)
    out = tmp_path / "article"
    out.mkdir()
    state = ArticleState(topic="Vendor overview", slug="article")
    state.output_dir = str(out)
    state.draft_markdown = ('# Overview\n\n## Vendor\n\n'
                            '<!-- visual:screenshot url="https://vendor.example/home" caption="Product overview" -->\n')
    state.frontmatter = {"cover": "assets/cover.png"}
    state.research = {"sources": [{"url": "https://vendor.example/board", "title": "Board"}]}
    requests = []
    monkeypatch.setattr(VisualDirector, "call_claude_json", lambda *a, **k: ({"insertions": []}, ""))
    def shoot(self, url, png, caption, seen, **kw):
        requests.append(url)
        if url.endswith("/home"):
            return None, {"reason": "LOGIN", "detail": "stand-in login wall"}
        png.write_bytes(standin._background_png())
        return str(png), {"title": "Board", "dhash": 1}
    monkeypatch.setattr(VisualDirector, "_shoot", shoot)
    VisualDirector(project, cfg).run(state)
    assert "https://vendor.example/board" not in requests, {"captures": requests, "visuals": state.visuals}


@pytest.mark.parametrize("broken", ["dangling", "bad_keep", "keep_directory"])
def test_rc91_broken_source_never_aborts_entry_point(tmp_path, monkeypatch, broken):
    from core.orchestrator import generate_article
    root, cfg, project = prepare(tmp_path, monkeypatch,
                                 SOURCE.replace('KEEP = ("_ledger.json",)', 'KEEP = 7') if broken == "bad_keep" else SOURCE)
    if broken == "dangling":
        (root / "research_sources" / "missing.py").symlink_to(root / "missing_source.py")
        agent, state = writer_state(tmp_path, cfg, project)
        agent.run(state)
        assert "meta description" in state.draft_markdown.lower()
    else:
        art = cached_article(tmp_path)
        if broken == "keep_directory":
            art.path("_ledger.json").mkdir()
        result = generate_article(standin.TOPIC, reuse_from=art.slug, reuse_out="rerun")
        assert result["status"] == "success", result.get("error")


def test_rc91_keep_does_not_copy_symlink_target_outside_article(tmp_path, monkeypatch):
    from core.orchestrator import generate_article
    from core import store
    prepare(tmp_path, monkeypatch)
    art = cached_article(tmp_path)
    outside = tmp_path / "private-fixture.json"
    outside.write_text('{"off": false, "text": "SYNTHETIC-PRIVATE-CONTENT"}')
    art.path("_ledger.json").symlink_to(outside)
    generate_article(standin.TOPIC, reuse_from=art.slug, reuse_out="rerun")
    copied = store.Repository(tmp_path / "out").get("rerun").path("_ledger.json")
    assert not copied.exists() or "SYNTHETIC-PRIVATE-CONTENT" not in copied.read_text(), copied.read_text()


def test_rc91_nanosecond_reload_does_not_reuse_stale_bytecode(tmp_path, monkeypatch):
    root, cfg, project = prepare(tmp_path, monkeypatch)
    path = root / "research_sources" / "ledger.py"
    second = 1_800_000_000
    os.utime(path, ns=(second * 10**9 + 100, second * 10**9 + 100))
    # Existing bytecode is a normal product of importing operator configuration. The test
    # process has PYTHONDONTWRITEBYTECODE=1; explicit compilation seeds only this temp fixture.
    py_compile.compile(str(path), doraise=True)
    prompts = []
    real = standin.answer
    monkeypatch.setattr(standin, "answer", lambda a, t: (prompts.append((a, t)), real(a, t))[1])
    first, st = writer_state(tmp_path, cfg, project, "first")
    first.run(st)
    assert any("LEDGER-v1" in t for a, t in prompts if a == "copywriter")
    path.write_text(SOURCE.replace("LEDGER-v1", "LEDGER-v2"))
    os.utime(path, ns=(second * 10**9 + 200, second * 10**9 + 200))
    prompts.clear()
    second_agent, st = writer_state(tmp_path, cfg, project, "second")
    second_agent.run(st)
    assert any("LEDGER-v2" in t for a, t in prompts if a == "copywriter"), [t[-700:] for a, t in prompts if a == "copywriter"]


def test_rc91_source_citation_can_be_applied_by_review(tmp_path, monkeypatch):
    from core import qa_changes, qa_model as qm, store
    from core.agents.base import BaseAgent
    _, cfg, project = prepare(tmp_path, monkeypatch, SOURCE.replace('return url == "https://vendor.example/board"', 'return False'))
    art = cached_article(tmp_path)
    claim = "Widget answers in 812 ms in our measurement."
    body = "# Our measurements\n\n## Results\n\n" + claim + "\n"
    art.write_article('---\ntitle: "Our measurements"\n---\n', body)
    run = art.run()
    run["review"] = {"reviews": [qm.review_record(id="r1", version=art.version())], "changes": [],
                     "findings": [qm.finding(id="r1-1", text="Add the measurement's citation from https://vendor.example/board to this claim.",
                                             review="r1", source="reader", severity="major", kind="fact", action="cite", target=claim)]}
    art.save_run(run)
    from core.agents.editor import Editor
    state = qa_changes.seed_state(art, "", body, run, project)
    assert "LEDGER-v1: Widget 812 ms [source: https://vendor.example/board]" in Editor(project, cfg)._user_message(state)
    prompts = []
    def cached(self, model, base, tail="", **kw):
        prompts.append(base + tail)
        return json.dumps({"find": claim, "replace": "[" + claim + "](https://vendor.example/board)"})
    monkeypatch.setattr(BaseAgent, "call_claude_cached", cached)
    monkeypatch.setattr(BaseAgent, "call_claude_json", lambda *a, **kw: ({"items": [{"id": "r1-1", "resolved": True, "why": "stand-in", "new_problem": None}]}, ""))
    result = qa_changes.fix_again(art.slug, "r1-1", art.version(), config=cfg, project=project)
    assert prompts, result
    assert "](https://vendor.example/board)" in art.text(), result


@pytest.mark.parametrize("operation", ["create", "redraw"])
def test_rc91_picture_repair_reads_the_pinned_source(tmp_path, monkeypatch, operation):
    from core import qa_changes
    from core.agents.interactive import VisualProducerService
    _, cfg, project = prepare(tmp_path, monkeypatch, SOURCE.replace(' + " [source:', ' + "; Gadget 640 ms [source:'))
    art = cached_article(tmp_path)
    art.write_article('---\ntitle: "Results"\n---\n', '# Results\n\nOur measured comparison.\n')
    qa_changes.seed_state(art, "", art.text(), art.run(), project)
    assert art.path("_ledger.json").exists()
    prompts = []
    def model(self, user, **kw):
        prompts.append(user)
        # A model obeying 'numbers may only come from here' cannot invent the missing rows.
        if "LEDGER-v1: Widget 812 ms; Gadget 640 ms" not in user:
            return {}, "{}"
        return {"producer": "chart", "spec": {"type": "bar", "title": "Measured result", "categories": ["Widget", "Gadget"],
                                                 "values": [812, 640], "unit": "ms"}, "alt": "Measured result"}, ""
    monkeypatch.setattr(VisualProducerService, "call_claude_json", model)
    service = VisualProducerService(project, cfg)
    if operation == "create":
        result = service.create(art.dir, "Chart our measured results.", "Our measured comparison.", "")
    else:
        art.assets.mkdir(exist_ok=True)
        art.path("assets/chart_1.png").write_bytes(standin._background_png())
        art.path("assets/chart_1.json").write_text(json.dumps({"producer": "chart", "spec": {"type": "bar", "categories": ["Widget"], "values": [812]}}))
        result = service.edit_visual(art.dir, "assets/chart_1.png", "Show all rows in our measured results.", alt="Measured comparison")
    assert "error" not in result, {"result": result, "source_in_prompt": ["LEDGER-v1" in p for p in prompts]}


def test_rc91_interactive_capture_respects_excluded_page(tmp_path, monkeypatch):
    from core import qa_changes
    from core.agents.interactive import VisualProducerService
    from core.producers import screenshot
    _, cfg, project = prepare(tmp_path, monkeypatch)
    cfg["pipeline"]["visuals"]["vision_check"] = False
    art = cached_article(tmp_path)
    art.write_article('---\ntitle: "Vendor"\n---\n', '# Vendor\n\nVendor overview.\n')
    qa_changes.seed_state(art, "", art.text(), art.run(), project)
    monkeypatch.setattr(VisualProducerService, "call_claude_json", lambda *a, **kw: (
        {"producer": "screenshot", "spec": {"url": "https://vendor.example/board"}, "alt": "Board", "caption": "Board"}, ""))
    calls = []
    def capture(url, path, **kw):
        calls.append(url)
        Path(path).write_bytes(standin._background_png())
        return str(path)
    monkeypatch.setattr(screenshot, "capture", capture)
    result = VisualProducerService(project, cfg).create(art.dir, "Add a screenshot of the vendor.", "Vendor overview.", "")
    assert not calls, {"captures": calls, "result": result}
