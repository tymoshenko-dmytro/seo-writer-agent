"""An install that ran an older release keeps working on this one: a prompt edited before a slot
was added gets the slot (its page saves again, the agent reads the project's text), and a short
or published UI_SECRET never signs the sessions."""
from __future__ import annotations

import pytest


def _old_edit(name: str) -> str:
    """The shipped prompt as an older release had it: without its editorial-rules section and its
    readers line, with the operator's own line added."""
    from core import prompt_overrides as po

    text = po.shipped(name)
    lines = [ln for ln in text.splitlines() if "{{editorial_rules}}" not in ln and not ln.startswith("## Editorial rules")]
    text = "\n".join(lines).replace(" The readers: {{audience}}.", "")
    return text + "\n\nOPERATOR LINE: keep this.\n"


@pytest.mark.parametrize("name", ["copywriter", "editor", "visual_checker", "brief_generator"])
def test_a_prompt_edited_before_a_slot_existed_gets_it_and_saves_again(studio, output_dir, name):
    from core import prompt_overrides as po

    old = _old_edit(name)
    p = po.override_path(output_dir, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(old, encoding="utf-8")                          # as the older release saved it
    shown = po.current(output_dir, name)
    assert "OPERATOR LINE: keep this." in shown and "{{editorial_rules}}" in shown
    assert po.placeholders(po.shipped(name)) <= po.placeholders(shown)
    assert po.current(output_dir, name) == shown and shown.count("{{editorial_rules}}") == 1   # never twice
    # its page saves it again with one more line
    r = studio.post(f"/settings/prompts/{name}", data={"text": shown + "\nANOTHER LINE.\n"})
    assert r.status_code in (200, 302) and "Not saved" not in r.get_data(as_text=True), r.get_data(as_text=True)[:400]
    saved = p.read_text(encoding="utf-8")
    assert "ANOTHER LINE." in saved and saved.count("{{editorial_rules}}") == 1
    # a slot taken out on purpose is still refused
    r = studio.post(f"/settings/prompts/{name}", data={"text": saved.replace("{{editorial_rules}}", "")})
    assert "{{editorial_rules}}" in r.get_data(as_text=True) and "{{editorial_rules}}" in p.read_text(encoding="utf-8")


def test_the_projects_rules_reach_an_agent_with_an_old_edit(output_dir, tmp_path, monkeypatch):
    from core import project_texts, prompt_overrides as po
    from core.agents.copywriter import Copywriter
    from core.config import load_core_config, load_project

    p = po.override_path(output_dir, "copywriter")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_old_edit("copywriter"), encoding="utf-8")
    project_texts.save(output_dir, "editorial_rules", "RULE-9: never promise a delivery date.")
    cfg = load_core_config()
    text = Copywriter(load_project(core=cfg), cfg).system_prompt()
    assert "OPERATOR LINE: keep this." in text and "RULE-9: never promise a delivery date." in text


@pytest.mark.parametrize("value,used", [("x" * 31, False), ("short-public-value", False), ("y" * 32, True), ("", False)])
def test_a_short_ui_secret_is_not_used(tmp_path, monkeypatch, caplog, value, used):
    from ui import auth

    monkeypatch.setenv("UI_SECRET", value)
    secret = auth.session_secret(tmp_path)
    if used:
        assert secret == value
    else:
        assert secret != value and secret == (tmp_path / "_ui_secret").read_text().strip() and len(secret) >= 32
        assert ("shorter than" in caplog.text) == bool(value)
