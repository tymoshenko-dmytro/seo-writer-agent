"""A project's own research sources (core/research_sources.py): what a source adds reaches every
agent's view of the research, its decision is kept per article, a rerun copies what it pinned, a
page it leaves out is not offered for a picture; a project without one sees nothing of it."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

SOURCE = '''
from dataclasses import dataclass

NAME = "board"
KEEP = ("_board_snapshot.json",)


@dataclass
class Row:                      # a source is a module like any other: a dataclass must load
    model: str
    ms: int


def gate(article_dir, topic, keyword, project):
    snap, off = article_dir / "_board_snapshot.json", article_dir / "_no_board"
    if snap.exists():
        return "pinned"
    if off.exists():
        return "off"
    if "{off_word}" in topic.lower():
        off.write_text("off")
        return "off"
    row = Row("Widget", 812)
    snap.write_text(f"{row.model} {row.ms} ms")
    return "on"


def corpus_block(article_dir, project):
    snap = article_dir / "_board_snapshot.json"
    return ("BOARD ROWS (measured by us): " + snap.read_text()) if snap.exists() else ""


def leaves_out(article_dir, url):
    return "board.example" in url
'''


def _project(tmp_path, source: str | None = SOURCE, off_word: str = "never-matches"):
    project = tmp_path / "project"
    project.mkdir()
    (project / "profile.yaml").write_text('name: "Northwind Payroll"\nbrand:\n  domains: ["northwind.example"]\n'
                                          'links:\n  site_sitemap: ""\n', encoding="utf-8")
    if source is not None:
        (project / "research_sources").mkdir()
        (project / "research_sources" / "board.py").write_text(source.replace("{off_word}", off_word),
                                                               encoding="utf-8")
    return project


def _run(tmp_path, monkeypatch, project, prompts):
    import standin

    monkeypatch.setenv("CONTENT_PROJECT_DIR", str(project))
    monkeypatch.setenv("CONTENT_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("OPENAI_API_KEY", "stand-in")
    standin.install(monkeypatch)
    real = standin.answer

    def answer(agent, text):
        prompts.append((agent, text))
        return real(agent, text)

    monkeypatch.setattr(standin, "answer", answer)
    # one more source in the research: a page of the board the source leaves out of pictures
    from core.agents import deep_researcher

    research = deep_researcher.DeepResearcher.run

    def with_board(self, state):
        state = research(self, state)
        state.research.setdefault("sources", []).append({"title": "The board", "url": "https://board.example/tts"})
        return state

    monkeypatch.setattr(deep_researcher.DeepResearcher, "run", with_board)
    from core import store
    from core.orchestrator import generate_article

    res = generate_article(standin.TOPIC)
    assert res["status"] == "success", res.get("error")
    return store.Repository(tmp_path / "out").get(res["slug"]), res


def test_a_source_reaches_the_writer_and_the_pictures_and_keeps_its_decision(tmp_path, monkeypatch):
    prompts: list = []
    art, _ = _run(tmp_path, monkeypatch, _project(tmp_path), prompts)
    assert art.path("_board_snapshot.json").read_text() == "Widget 812 ms"      # decided and pinned once
    writer = [t for a, t in prompts if a == "copywriter" and "Answer each FAQ question" not in t]
    assert writer and "BOARD ROWS (measured by us): Widget 812 ms" in writer[0]
    pictures = [t for a, t in prompts if a == "visual_director"]
    assert pictures and all("board.example" not in t for t in pictures)
    assert any("BOARD ROWS" in t for t in pictures)                               # the chart builder's facts
    checks = [t for a, t in prompts if a == "edit_verifier"]
    assert checks and all("BOARD ROWS" in t for t in checks)


def test_a_source_says_off_and_nothing_of_it_is_read(tmp_path, monkeypatch):
    import standin

    prompts: list = []
    art, _ = _run(tmp_path, monkeypatch, _project(tmp_path, off_word=standin.TOPIC.split()[0].lower()), prompts)
    assert art.path("_no_board").is_file() and not art.path("_board_snapshot.json").exists()
    assert all("BOARD ROWS" not in t for _, t in prompts)
    assert any("research source board: left out" in line for line in art.run()["log"])


def test_a_project_without_sources_sees_nothing(tmp_path, monkeypatch):
    from core import research_sources
    from core.agents.base import BaseAgent

    project = SimpleNamespace(root=_project(tmp_path, source=None), links={})
    agent = SimpleNamespace(project=project, citable_facts=BaseAgent.citable_facts)
    research = {"key_statistics": [{"stat": "Clicks", "value": "4%", "source_url": "https://a.example"}]}
    assert (BaseAgent.render_corpus(agent, research, tmp_path)
            == BaseAgent.render_corpus(agent, research))
    assert research_sources.gate_all(tmp_path, "t", "k", project) == []
    assert research_sources.corpus_blocks(tmp_path, project) == ""
    assert research_sources.keep_files(project) == ()
    assert not research_sources.leaves_out(tmp_path, "https://board.example/tts", project)
    # the project the release ships has none
    from pathlib import Path

    shipped = Path(__file__).resolve().parents[1] / "project"
    assert not (shipped / research_sources.DIR).exists()


def test_a_broken_source_is_left_out_and_a_private_file_skipped(tmp_path, caplog):
    import logging

    from core import research_sources

    project = SimpleNamespace(root=_project(tmp_path))
    folder = project.root / research_sources.DIR
    (folder / "broken.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    (folder / "half.py").write_text("def gate(*a):\n    return 'on'\n", encoding="utf-8")
    (folder / "_helper.py").write_text("raise RuntimeError('never loaded')\n", encoding="utf-8")
    (folder / "noisy.py").write_text("def gate(*a):\n    raise ValueError('gate')\n"
                                     "def corpus_block(*a):\n    raise ValueError('block')\n"
                                     "def leaves_out(*a):\n    raise ValueError('leaves')\n", encoding="utf-8")
    art = tmp_path / "article"
    art.mkdir()
    with caplog.at_level(logging.WARNING):
        names = [research_sources.name_of(s) for s in research_sources.sources(project)]
        assert names == ["board", "noisy"]
        assert research_sources.gate_all(art, "a topic", "", project) == []
        assert research_sources.corpus_blocks(art, project) == "BOARD ROWS (measured by us): Widget 812 ms"
        assert research_sources.leaves_out(art, "https://board.example/x", project)
        assert not research_sources.leaves_out(art, "https://other.example/x", project)
    assert "broken.py could not be loaded" in caplog.text and "half.py has no gate() and corpus_block()" in caplog.text
    assert "never loaded" not in caplog.text


def test_a_changed_source_file_is_read_again(tmp_path):
    import os

    from core import research_sources

    project = SimpleNamespace(root=_project(tmp_path))
    art = tmp_path / "article"
    art.mkdir()
    research_sources.gate_all(art, "a topic", "", project)
    assert "Widget 812 ms" in research_sources.corpus_blocks(art, project)
    path = project.root / research_sources.DIR / "board.py"
    path.write_text(SOURCE.replace("BOARD ROWS (measured by us): ", "ROWS v2: "), encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert research_sources.corpus_blocks(art, project) == "ROWS v2: Widget 812 ms"


@pytest.mark.parametrize("name", ["../escape.json", "a/b.json", "..", ""])
def test_kept_files_are_names_in_the_article_folder_only(tmp_path, name):
    from core import research_sources

    project = SimpleNamespace(root=_project(tmp_path, source=SOURCE.replace(
        'KEEP = ("_board_snapshot.json",)', f'KEEP = ("_board_snapshot.json", {name!r})')))
    assert research_sources.keep_files(project) == ("_board_snapshot.json",)


def test_figures_a_source_gives_are_not_hinted_as_uncited(tmp_path):
    from core.validators import attested_numbers_from, uncited_numeral_paragraphs

    md = "Widget answers in 812 ms, the fastest we measured.\n\nOther tools take 900 ms on average."
    without = uncited_numeral_paragraphs(md, attested_numbers_from(""))
    with_board = uncited_numeral_paragraphs(md, attested_numbers_from("", "BOARD ROWS: Widget 812 ms"))
    assert any("812" in p for p in without) and not any("812" in p for p in with_board)
    assert any("900" in p for p in with_board)


def test_a_rerun_copies_what_the_source_pinned(tmp_path, monkeypatch):
    prompts: list = []
    art, res = _run(tmp_path, monkeypatch, _project(tmp_path), prompts)
    art.path("_board_snapshot.json").write_text("Widget 640 ms (pinned earlier)")
    from core import store
    from core.orchestrator import generate_article

    again = generate_article(json.loads(json.dumps(res.get("topic") or ""))
                             or art.run().get("topic"), reuse_from=art.slug)
    assert again["status"] == "success", again.get("error")
    rerun = store.Repository(tmp_path / "out").get(again["slug"])
    assert rerun.path("_board_snapshot.json").read_text() == "Widget 640 ms (pinned earlier)"


@pytest.mark.parametrize("agent", ["BriefGenerator", "Copywriter", "Editor", "VisualDirector", "VisualChecker"])
def test_the_projects_editorial_rules_reach_the_plan_the_text_and_the_pictures(tmp_path, monkeypatch, agent):
    """A project's own rules reach every agent that decides what the article says or shows: the
    brief plans around them, the charts and the picture check follow them, as the writer and the
    review do (an agent without them planned or drew what the rules forbid)."""
    import importlib

    from core.config import load_core_config, load_project

    project = _project(tmp_path, source=None)
    (project / "editorial_rules.md").write_text("RULE-7: never chart a price without its date.", encoding="utf-8")
    monkeypatch.setenv("CONTENT_PROJECT_DIR", str(project))
    monkeypatch.setenv("CONTENT_OUTPUT_DIR", str(tmp_path / "out"))
    cfg = load_core_config()
    mod = {"BriefGenerator": "brief_generator", "Copywriter": "copywriter", "Editor": "editor",
           "VisualDirector": "visual_director", "VisualChecker": "visual_checker"}[agent]
    cls = getattr(importlib.import_module(f"core.agents.{mod}"), agent)
    text = cls(load_project(core=cfg), cfg).system_prompt()
    assert "RULE-7: never chart a price without its date." in text and "{{editorial_rules}}" not in text


@pytest.mark.parametrize("topic,reads", [("a never-matches topic", True), ("off topic", False)])
def test_the_studio_decides_for_an_old_article_before_a_picture_reads_the_sources(tmp_path, monkeypatch, topic, reads):
    """An article written or imported before a source decided: the Studio's picture producer runs the
    source's gate from the article's own topic before it reads anything, so a topic the source
    leaves out never gets its rows (nor a pin the next review would keep)."""
    import json as _json

    from core.agents.interactive import VisualProducerService
    from core.config import load_core_config, load_project

    project = _project(tmp_path, off_word="off topic")
    monkeypatch.setenv("CONTENT_PROJECT_DIR", str(project))
    monkeypatch.setenv("CONTENT_OUTPUT_DIR", str(tmp_path / "out"))
    art = tmp_path / "out" / "old-article"
    art.mkdir(parents=True)
    (art / "_run.json").write_text(_json.dumps({"topic": topic, "research": {}}), encoding="utf-8")
    seen = []
    monkeypatch.setattr(VisualProducerService, "call_claude_json", lambda self, user, **kw: (seen.append(user), ({}, "{}"))[1])
    cfg = load_core_config()
    VisualProducerService(load_project(core=cfg), cfg).create(art, "Chart the measurements.", "", "")
    assert seen and ("BOARD ROWS" in seen[0]) is reads
    assert art.joinpath("_board_snapshot.json").exists() is reads and art.joinpath("_no_board").exists() is not reads
