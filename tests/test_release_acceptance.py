"""Expanded Round 1 acceptance tests: real entry points, temporary files, stand-ins."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_release_probes import configured
from test_release_controls import cli_json

TEXTS = ['brand_voice', 'product_facts', 'editorial_rules']


@pytest.mark.parametrize('key', TEXTS)
@pytest.mark.parametrize('shipped', ['', 'Original project text.'])
def test_setup_cli_clear_preserve_and_empty_prompt(studio, output_dir, tmp_path, monkeypatch, capsys, key, shipped):
    from core import config, project_texts
    from core.agents.base import BaseAgent
    project = tmp_path / 'project'
    project.mkdir()
    original = Path(__file__).resolve().parents[1] / 'project'
    for f in original.iterdir():
        if f.is_file():
            (project / f.name).write_bytes(f.read_bytes())
    (project / {'brand_voice': 'brand.md', 'product_facts': 'products.md', 'editorial_rules': 'editorial_rules.md'}[key]).write_text(shipped)
    monkeypatch.setenv('CONTENT_PROJECT_DIR', str(project))
    for text_key in TEXTS:
        assert studio.post('/setup/texts', data={text_key: 'Saved text for ' + text_key}).status_code == 302
    empty = tmp_path / 'empty.md'
    empty.write_text(' \n\t')
    code, result = cli_json(monkeypatch, capsys, 'setup', '--no-test')
    assert code == 0 and result['ok']
    assert project_texts.current(output_dir, key) == 'Saved text for ' + key
    code, result = cli_json(monkeypatch, capsys, 'setup', '--no-test', '--' + key.replace('_', '-') + '-file', str(empty))
    assert code == 0 and result['ok']
    assert project_texts.current(output_dir, key) == ''
    for other in set(TEXTS) - {key}:
        assert project_texts.current(output_dir, other) == 'Saved text for ' + other
    loaded = config.load_project()
    assert getattr(loaded, key) == ''
    prompt = BaseAgent(loaded, config.load_core_config())._inject('{{' + key + '}}')
    assert 'None given' in prompt and 'Saved text' not in prompt and 'Original project' not in prompt
    import re
    page = studio.get('/setup?step=2')
    assert page.status_code == 200
    textarea = re.search(r'<textarea[^>]*name="' + key + r'"[^>]*>(.*?)</textarea>', page.get_data(as_text=True), re.S)
    assert textarea is not None and not textarea.group(1).strip()


@pytest.mark.parametrize('endpoint', ['/settings/wordpress/test', '/settings/keys/test-all'])
@pytest.mark.parametrize('part', ['WP_BASE_URL', 'WP_USERNAME', 'WP_APP_PASSWORD'])
def test_wordpress_rotation_during_transport_is_not_certified(studio, output_dir, monkeypatch, endpoint, part):
    from core import connections, setup, api_keys
    configured(output_dir)
    original = tuple(api_keys.current(output_dir, env) for env in api_keys.WP_GROUP)
    seen = []
    rotated = []
    for env in connections.REQUIRED:
        monkeypatch.setitem(connections.TESTS, env, lambda key: {'ok': True, 'detail': 'offline'})
    def get(url, **kwargs):
        auth = kwargs.get('auth')
        if auth:
            seen.append((url, auth))
            if rotated and auth[1] == 'dummy-rotated-password':
                return SimpleNamespace(status_code=401, json=lambda: {})
        if url.endswith('/users/me') and not rotated:
            updates = {'WP_APP_PASSWORD': 'dummy-rotated-password'}
            if part == 'WP_BASE_URL':
                updates[part] = 'https://replacement.example'
            if part == 'WP_USERNAME':
                updates[part] = 'new-editor'
            assert studio.post('/settings/keys', data=updates).status_code == 302
            rotated.append(True)
        data = {'name': 'Site'} if url.endswith('/wp-json') else (
            {'name': 'Editor', 'roles': ['editor']} if url.endswith('/users/me') else [])
        return SimpleNamespace(status_code=200, json=lambda: data)
    monkeypatch.setattr(connections.requests, 'get', get)
    assert studio.post(endpoint).status_code == 302
    assert seen and all(url.startswith(original[0]) and auth == original[1:] for url, auth in seen)
    assert connections.WORDPRESS not in connections.recorded(output_dir)
    assert 'WordPress' in setup.state(output_dir)['untested']
    assert setup.ready_to_write(output_dir)
    assert connections.recorded(output_dir)['WordPress']['ok'] is False


@pytest.mark.parametrize('payload,ok', [
    ({'result': {'isError': True, 'content': [{'text': 'Access denied'}]}}, False),
    ({'result': {'content': [{'text': '{}'}]}}, False),
    ({'result': {'content': [{'text': 'not JSON'}]}}, False),
    ({'result': {'content': [{'text': '[]'}]}}, False),
    ({'result': {'content': [{'text': '{"limits_and_usage": {}}'}]}}, False),
    ({'result': {'content': [{'text': '{"limits_and_usage": {"units_usage_workspace": 12, "units_limit_workspace": 1000}}'}]}}, True),
])
def test_ahrefs_doctor_contract(output_dir, monkeypatch, capsys, payload, ok):
    from core import connections
    configured(output_dir)
    for env in connections.REQUIRED:
        if env != 'AHREFS_API_KEY':
            monkeypatch.setitem(connections.TESTS, env, lambda key: {'ok': True, 'detail': 'offline'})
    monkeypatch.setattr(connections, 'test_wordpress', lambda *args: {'ok': True, 'detail': 'offline'})
    class Session:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs):
            data = {'result': {}} if kwargs['json']['method'] == 'initialize' else payload
            return SimpleNamespace(status_code=200, json=lambda: data)
    monkeypatch.setattr(connections.requests, 'Session', Session)
    code, data = cli_json(monkeypatch, capsys, 'doctor')
    assert code == (0 if ok else 1) and data['ok'] is ok
    assert connections.recorded(output_dir)['AHREFS_API_KEY']['ok'] is ok


@pytest.mark.parametrize('path', ['locate_retry', 'rewrite_retry', 'new_section_retry'])
@pytest.mark.parametrize('rule', ['', 'Always address the reader as you; never call the reader a customer.'])
def test_cli_apply_retries_keep_rules(output_dir, monkeypatch, capsys, path, rule):
    from core import sample, store, project_texts, qa_model as qm
    from core.agents.base import BaseAgent
    if rule:
        project_texts.save(output_dir, 'editorial_rules', rule)
    art = store.Repository(output_dir).get(sample.add(output_dir))
    sentence = 'You can compare the choices before selecting a plan for your team.'
    body = '# Buying guide\n\n## Compare choices\n\n' + sentence + '\n\n## Next steps\n\nYou can check the details before making a choice.\n'
    art.write_article('---\ntitle: Buying guide\n---', body)
    fix = ('Write the missing "Review options" section.' if path == 'new_section_retry' else
           'Rewrite the section "Compare choices" to make the advice clearer.')
    run = art.run()
    run['review'] = {'reviews': [qm.review_record(id='r2', version=art.version())], 'changes': [],
                     'findings': [qm.finding(id='r2-1', text=fix, review='r2', source='reader', severity='Major', kind='structure')]}
    art.save_run(run)
    calls, verifies = [], []
    def cached(self, model, base, tail, **kwargs):
        prompt = base + tail + kwargs.get('system', '')
        calls.append(prompt)
        if 'Write ONE new section' in base:
            bad = ' Read [more](https://unapproved.example).' if len(calls) == 1 else ''
            return json.dumps({'after_heading': '## Compare choices', 'section': '## Review options\n\nYou can compare the details before choosing.' + bad})
        if 'rewriting ONE SECTION' in base:
            if path == 'locate_retry':
                return '{}'
            return json.dumps({'section': '## Compare choices\n\nYou can compare the choices before selecting a plan for your team. Read [more](https://unapproved.example).'})
        if path == 'rewrite_retry' and len(calls) == 1:
            return json.dumps({'find': '', 'replace': '', 'note': 'Needs a wider edit.'})
        bad = ' Read [more](https://unapproved.example).' if len(calls) == 1 else ''
        return json.dumps({'find': sentence, 'replace': 'You can weigh the choices before selecting a plan for your team.' + bad})
    def verify(self, prompt, **kwargs):
        verifies.append(prompt + kwargs.get('system', ''))
        return {'items': [{'id': 'r2-1', 'resolved': True, 'why': 'stand-in', 'new_problem': None}]}, ''
    monkeypatch.setattr(BaseAgent, 'call_claude_cached', cached)
    monkeypatch.setattr(BaseAgent, 'call_claude_json', verify)
    code, data = cli_json(monkeypatch, capsys, 'apply', art.slug, 'r2-1', '--version', art.version(), '--review', 'r2', '--yes')
    assert code == 0, data
    assert len(calls) >= 2
    assert any('REJECTED' in p or 'rejected' in p for p in calls), calls
    assert all(rule in p for p in calls + verifies)
    assert verifies
    assert 'unapproved.example' not in art.text()
    if not rule:
        assert all('EDITORIAL RULES' not in p for p in calls)


def test_check_all_key_rotation_blocks_real_cli_generate(studio, output_dir, monkeypatch, capsys):
    from core import connections, setup
    configured(output_dir)
    for env in connections.REQUIRED:
        if env != 'ANTHROPIC_API_KEY':
            monkeypatch.setitem(connections.TESTS, env, lambda key: {'ok': True, 'detail': 'offline'})
    monkeypatch.setattr(connections, 'test_wordpress', lambda *args: {'ok': True, 'detail': 'offline'})
    seen = []
    def get(url, **kwargs):
        assert url == 'https://api.anthropic.com/v1/models'
        key = kwargs['headers']['x-api-key']
        seen.append(key)
        if key == 'dummy-ANTHROPIC_API_KEY':
            assert studio.post('/settings/keys', data={'ANTHROPIC_API_KEY': 'dummy-refused-new-key'}).status_code == 302
            return SimpleNamespace(status_code=200)
        return SimpleNamespace(status_code=401)
    monkeypatch.setattr(connections.requests, 'get', get)
    assert studio.post('/settings/keys/test-all').status_code == 302
    assert 'ANTHROPIC_API_KEY' not in connections.recorded(output_dir)
    code, data = cli_json(monkeypatch, capsys, 'generate', 'A trial topic', '--yes')
    assert code == 1 and data['refused'] == 'not ready', data
    assert seen == ['dummy-ANTHROPIC_API_KEY', 'dummy-refused-new-key']
    assert connections.recorded(output_dir)['ANTHROPIC_API_KEY']['ok'] is False
