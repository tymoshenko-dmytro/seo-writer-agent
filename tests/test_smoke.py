"""The offline self-check: a new install starts, shows the setup, saves a project, refuses to write
until the seven services pass, opens the sample article on every tab, answers `cli.py doctor`,
and goes from a topic to a reviewed article on stand-in services. No key, no network, no cost."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SEVEN = ["Anthropic", "OpenAI", "Parallel", "Ahrefs", "Scrapfly", "Winston AI", "WordPress"]


def test_a_new_install_starts_and_offers_the_setup(studio):
    assert studio.get("/healthz").status_code == 200
    page = studio.get("/").get_data(as_text=True)
    assert "The setup is not finished" in page and 'href="/setup"' in page
    assert "Open the sample article" in page
    setup = studio.get("/setup").get_data(as_text=True)
    assert "The project" in setup and "Its texts" in setup and "WordPress" in setup


def test_the_shipped_project_is_empty_and_the_prompts_say_so(output_dir):
    from core.agents.copywriter import Copywriter
    from core.agents.editor import Editor
    from core.config import load_core_config, load_project

    cfg = load_core_config()
    project = load_project(core=cfg)
    assert project.name == "" and project.brand.domains == () and project.product_facts == ""
    assert project.links.get("competitor_hosts") == []
    for agent in (Copywriter(project, cfg), Editor(project, cfg)):
        text = agent.system_prompt()
        assert "{{" not in text and "(None given" in text


def test_the_setup_saves_the_project_and_its_texts(studio, output_dir):
    r = studio.post("/setup/project", data={"name": "Northwind Payroll", "domain": "northwind.example",
                                            "blog": "https://blog.northwind.example",
                                            "audience": "Owners of small businesses"})
    assert r.status_code == 302 and r.headers["Location"].endswith("step=2")
    studio.post("/setup/texts", data={"brand_voice": "Plain and friendly.",
                                      "product_facts": "Northwind Payroll runs payroll for up to 50 people.",
                                      "editorial_rules": "Never promise a filing deadline."})
    from core.agents.copywriter import Copywriter
    from core.config import load_core_config, load_project

    cfg = load_core_config()
    project = load_project(core=cfg)
    assert project.name == "Northwind Payroll" and project.brand.domains == ("northwind.example",)
    assert project.links["site_sitemap"] == "https://northwind.example/sitemap.xml"
    assert project.links["blog_sitemap"] == "https://blog.northwind.example/sitemap_index.xml"
    prompt = Copywriter(project, cfg).system_prompt()
    assert "Never promise a filing deadline." in prompt and "Plain and friendly." in prompt


def test_writing_waits_for_all_seven_services_and_their_tests(studio, output_dir, monkeypatch):
    from core import api_keys, connections, setup

    setup.apply(output_dir, name="Northwind Payroll", domain="northwind.example", test=False)
    st = setup.state(output_dir)
    assert not st["can_write"] and all(s in st["reason"] for s in SEVEN)
    r = studio.post("/run", data={"topic": "payroll software for small teams"})
    assert r.status_code == 302 and not list(output_dir.glob("payroll-*"))

    keys = {k["env"]: "test-" + k["env"].lower() for k in api_keys.KEYS if k["env"] not in api_keys.WP_GROUP}
    for env, value in keys.items():
        monkeypatch.setenv(env, value)
    monkeypatch.setenv("WP_BASE_URL", "https://blog.northwind.example")
    monkeypatch.setenv("WP_USERNAME", "editor")
    monkeypatch.setenv("WP_APP_PASSWORD", "abcd efgh ijkl mnop")
    answers = {env: {"ok": True, "detail": "works"} for env in connections.REQUIRED}
    answers["ANTHROPIC_API_KEY"] = {"ok": False, "detail": "the key was refused"}
    monkeypatch.setattr(connections, "test_key", lambda env, value=None: answers[env])
    monkeypatch.setattr(connections, "test_wordpress", lambda *a: {"ok": True, "site": "Northwind", "detail": "connected"})
    assert setup.ready_to_write(output_dir) == "Writing needs Anthropic to pass its test."
    answers["ANTHROPIC_API_KEY"] = {"ok": True, "detail": "works"}
    assert setup.ready_to_write(output_dir) == "Writing needs Anthropic to pass its test."   # until tested again
    connections.check(output_dir, "ANTHROPIC_API_KEY")
    assert setup.ready_to_write(output_dir) == "" and setup.state(output_dir)["done"]


@pytest.mark.parametrize("tab", ["article", "review", "publish", "seo", "pictures", "sources", "history"])
def test_the_sample_article_opens_on_every_tab(studio, output_dir, tab):
    r = studio.post("/sample")
    assert r.status_code == 302
    slug = r.headers["Location"].rsplit("/", 1)[-1]
    page = studio.get(f"/a/{slug}?tab={tab}")
    assert page.status_code == 200
    assert "How to Write a Meta Description" in page.get_data(as_text=True)


def test_the_sample_carries_a_review_pictures_and_versions(output_dir):
    from core import sample, store

    slug = sample.add(output_dir)
    assert sample.add(output_dir) == slug                       # once: the copy there is kept
    art = store.Repository(output_dir).get(slug)
    rv = art.run()["review"]
    assert {f["status"] for f in rv["findings"]} == {"changed", "open"}
    assert {p.name for p in art.assets.iterdir()} >= {"cover.png", "chart_1.png", "infographic_1.png"}


def _cli(*args, env_extra=None, cwd=REPO):
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")}
    env.update(env_extra or {})
    env["PYTHONPATH"] = str(Path(__file__).parent / "netguard")
    return subprocess.run([sys.executable, str(REPO / "cli.py"), *args], capture_output=True, text=True,
                          env=env, cwd=cwd, timeout=120)


def test_doctor_says_what_is_missing(output_dir):
    res = _cli("doctor", "--json", "--no-test", env_extra={"CONTENT_OUTPUT_DIR": str(output_dir)})
    assert res.returncode == 1, res.stderr
    data = json.loads(res.stdout)
    assert data["ok"] is False
    names = {c["check"]: c for c in data["checks"]}
    assert set(SEVEN) <= set(names) and all(names[s]["required"] and not names[s]["ok"] for s in SEVEN)
    assert names["storage"]["ok"] and not names["project"]["ok"]


def test_generate_refuses_before_anything_is_paid(output_dir):
    res = _cli("generate", "payroll software", "--yes", "--json", env_extra={"CONTENT_OUTPUT_DIR": str(output_dir)})
    assert res.returncode == 1
    assert json.loads(res.stdout)["refused"] == "not ready" and not any(output_dir.iterdir())


def test_a_topic_becomes_a_reviewed_article_on_stand_in_services(tmp_path, monkeypatch):
    """The engine's own code end to end: research, brief, writing, a chart, an infographic, the
    cover, the keyword report, the first version, one review and its safe changes."""
    import standin

    project = tmp_path / "project"
    project.mkdir()
    (project / "profile.yaml").write_text('name: "Northwind Payroll"\nbrand:\n  domains: ["northwind.example"]\n'
                                          'links:\n  site_sitemap: ""\n', encoding="utf-8")
    monkeypatch.setenv("CONTENT_PROJECT_DIR", str(project))
    monkeypatch.setenv("CONTENT_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("OPENAI_API_KEY", "stand-in")
    standin.install(monkeypatch)
    from core import store, usage
    from core.orchestrator import generate_article

    res = generate_article(standin.TOPIC)
    assert res["status"] == "success", res.get("error")
    art = store.Repository(tmp_path / "out").get(res["slug"])
    body = art.text()
    assert "# How to Write a Meta Description That Earns the Click" in body
    assert "![" in body and art.path("04_draft.md").is_file() and art.path("06_keywords.md").is_file()
    rv = art.run()["review"]
    assert [f["status"] for f in rv["findings"]] == ["changed", "changed", "open", "open"]
    assert "the meta description examples below" in body and standin._REPEAT not in body
    assert usage.article_summary(art.dir, {})["total"] == 0
