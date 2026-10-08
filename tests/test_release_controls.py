"""Controls from the independent review of the release (round 1): every failed service blocks every
way to start a run, doctor tests and keeps all seven, key rotation, the sample, two unrelated
projects, the env template and Compose, Chromium offline, a loopback server."""
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_release_probes import configured


def cli_json(monkeypatch, capsys, *args):
    import cli
    monkeypatch.setattr(sys, 'argv', ['cli.py', *args, '--json'])
    code = 0
    try:
        cli.main()
    except SystemExit as e:
        code = e.code
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize('env', ['ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'PARALLEL_API_KEY',
                                 'AHREFS_API_KEY', 'SCRAPFLY_KEY', 'WINSTON_API_KEY', 'WordPress'])
def test_every_failed_service_blocks_all_generation_entrypoints(studio, output_dir, monkeypatch, capsys, env):
    from core import connections, store
    from ui.jobs import runner
    configured(output_dir)
    if env == 'WordPress':
        connections.keep_wordpress(output_dir, {'ok': False, 'detail': 'stand-in refusal'})
    else:
        connections.record(output_dir, env, {'ok': False, 'detail': 'stand-in refusal'})
    queued = []
    monkeypatch.setattr(runner, 'submit', lambda *a, **kw: queued.append((a, kw)) or 'job')
    assert studio.post('/run', data={'topic': 'recruiting tips'}).status_code == 302
    d = output_dir / 'unfinished'
    d.mkdir()
    store.write_json(d / '_run.json', {'topic': 'recruiting tips', 'status': 'failed'})
    assert studio.post('/a/unfinished/run-again').status_code == 302
    assert not queued
    code, data = cli_json(monkeypatch, capsys, 'generate', 'recruiting tips', '--yes')
    assert code == 1 and data['refused'] == 'not ready', data


def test_doctor_tests_and_records_all_seven_and_generate_estimates_only(output_dir, monkeypatch, capsys):
    from core import connections, setup
    configured(output_dir)
    seen = []
    monkeypatch.setattr(connections, 'test_key', lambda env, value=None: seen.append(env) or {'ok': True, 'detail': 'offline'})
    monkeypatch.setattr(connections, 'test_wordpress', lambda *a, **kw: seen.append('WordPress') or {'ok': True, 'detail': 'offline'})
    code, data = cli_json(monkeypatch, capsys, 'doctor')
    assert code == 0 and data['ok'] and data['state']['done'], data
    assert set(seen) == {*connections.REQUIRED, 'WordPress'}
    assert len(connections.recorded(output_dir)) == 7
    code, data = cli_json(monkeypatch, capsys, 'generate', 'recruiting tips')
    assert code == 1 and data['refused'] == 'needs_yes' and data['estimate']['usd'] == 5


@pytest.mark.parametrize('env', ['ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'PARALLEL_API_KEY',
                                 'AHREFS_API_KEY', 'SCRAPFLY_KEY', 'WINSTON_API_KEY'])
def test_ordinary_key_rotation_invalidates_result(studio, output_dir, env):
    from core import connections, setup
    configured(output_dir)
    assert studio.post('/settings/keys', data={env: 'dummy-rotated-key'}).status_code == 302
    assert env not in connections.recorded(output_dir)
    assert not setup.state(output_dir)['done']
    assert (output_dir / '_secrets.json').stat().st_mode & 0o777 == 0o600
    assert 'dummy-rotated-key' not in studio.get('/settings/keys').get_data(as_text=True)


def test_sample_cli_preserves_edits_and_default_estimate(output_dir, monkeypatch, capsys):
    from core import store, agent_door
    code, data = cli_json(monkeypatch, capsys, 'sample')
    assert code == 0
    art = store.Repository(output_dir).get(data['slug'])
    art.path('article.md').write_text(art.text() + '\nReviewer local sample edit.\n')
    code, second = cli_json(monkeypatch, capsys, 'sample')
    assert code == 0 and second['slug'] == data['slug']
    assert 'Reviewer local sample edit.' in art.text()
    assert agent_door.estimate(output_dir, 'generation')['usd'] == 5
    assert not art.path('_wp.json').exists()


@pytest.mark.parametrize('name,domain,audience,facts', [
    ('Northwind Recruiting', 'northwind.example', 'Hiring managers', 'Northwind Recruiting finds warehouse staff.'),
    ('Market Square', 'market-square.example', 'Buyers and independent merchants', 'Market Square lists handmade furniture.'),
])
def test_two_unrelated_projects_run_with_their_own_prompts(studio, output_dir, monkeypatch, name, domain, audience, facts):
    import standin
    from core import connections, project_settings
    from core.agents.base import BaseAgent
    from core.config import load_project, load_core_config
    from core.agents.copywriter import Copywriter
    from core.agents.editor import Editor
    from core.orchestrator import generate_article
    assert studio.post('/setup/project', data={'name': name, 'domain': domain, 'blog': 'https://blog.' + domain,
                                               'audience': audience}).status_code == 302
    rule = 'Never describe a named organization as a customer without written confirmation.'
    assert studio.post('/setup/texts', data={'product_facts': facts, 'brand_voice': 'Short, plain sentences.',
                                           'editorial_rules': rule}).status_code == 302
    project_settings.save_website(output_dir, site_sitemap='', blog_sitemap='')
    cfg, project = load_core_config(), load_project()
    for cls in [Copywriter, Editor]:
        prompt = cls(project, cfg).system_prompt()
        assert name in prompt and audience in prompt and facts in prompt and rule in prompt
        assert not re.search(r'palabra|coval|speech|interpreters|dubbing|\bZoom\b|\bTTS\b|\bSTT\b', prompt, re.I)
        assert '{{' not in prompt
    monkeypatch.setenv('OPENAI_API_KEY', 'stand-in')
    standin.install(monkeypatch)
    prompts = []
    original = standin._Messages.create
    def capture(self, **kwargs):
        prompts.append(json.dumps(kwargs))
        return original(self, **kwargs)
    monkeypatch.setattr(standin._Messages, 'create', capture)
    result = generate_article(standin.TOPIC)
    assert result['status'] == 'success', result.get('error')
    assert prompts and not re.search(r'palabra|coval|speech|interpreters|dubbing|\bZoom\b|\bTTS\b|\bSTT\b', '\n'.join(prompts), re.I)


def test_env_example_parses_and_compose_is_local():
    from dotenv import dotenv_values
    import yaml
    root = Path(__file__).resolve().parents[1]
    vals = dotenv_values(root / '.env.example')
    assert vals['UI_HOST'] == '127.0.0.1' and vals['UI_PORT'] == '5050'
    assert all(v == '' for k, v in vals.items() if k not in {'UI_HOST', 'UI_PORT'})
    compose = yaml.safe_load((root / 'docker-compose.yml').read_text())
    service = compose['services']['studio']
    assert service['ports'] == ['127.0.0.1:5050:5050']
    assert service['environment']['UI_PASSWORD'].startswith('${UI_PASSWORD:?')


def test_chromium_can_render_offline():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(args=['--no-sandbox', '--single-process', '--no-zygote'], chromium_sandbox=False)
        try:
            page = browser.new_page()
            page.route('**/*', lambda route: route.abort())
            page.set_content('<h1>Offline Chromium</h1>')
            assert page.inner_text('h1') == 'Offline Chromium'
        finally:
            browser.close()


def test_loopback_studio_can_listen(tmp_path, monkeypatch):
    from werkzeug.serving import make_server
    out = tmp_path / 'empty-install'
    monkeypatch.setenv('CONTENT_OUTPUT_DIR', str(out))
    import ui.app
    app = ui.app.create_app(out)
    server = make_server('127.0.0.1', 0, app)
    server.server_close()
