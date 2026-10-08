"""Regressions from the independent review of the release (round 1, R543-01..04): the failures it reproduced; network blocked by tests/conftest.py."""
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest


def configured(output_dir):
    from core import setup, connections
    setup.apply(output_dir, name='Northwind Recruiting', domain='northwind.example',
                keys={env: 'dummy-' + env for env in connections.REQUIRED},
                wp_url='https://blog.northwind.example', wp_user='editor', wp_password='dummy-password', test=False)
    for env in connections.REQUIRED:
        connections.record(output_dir, env, {'ok': True, 'detail': 'stand-in passed'})
    connections.keep_wordpress(output_dir, {'ok': True, 'detail': 'stand-in passed'})


@pytest.mark.parametrize('key', ['brand_voice', 'product_facts', 'editorial_rules'])
def test_setup_text_can_be_cleared(studio, output_dir, key):
    from core import project_texts
    assert studio.post('/setup/texts', data={key: 'Obsolete text that the owner removed.'}).status_code == 302
    assert studio.post('/setup/texts', data={key: ''}).status_code == 302
    actual = project_texts.current(output_dir, key)
    assert actual == '', {'key': key, 'after_save': actual}


@pytest.mark.parametrize('payload', [
    {'result': {'isError': True, 'content': [{'type': 'text', 'text': 'Access denied for this API key'}]}},
    {'result': {'content': [{'type': 'text', 'text': '{}'}]}},
])
def test_ahrefs_tool_failure_is_not_a_pass(studio, output_dir, monkeypatch, payload):
    from core import connections, setup
    configured(output_dir)
    class Session:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs):
            data = {'result': {}} if kwargs['json']['method'] == 'initialize' else payload
            return SimpleNamespace(status_code=200, json=lambda: data)
    monkeypatch.setattr(connections.requests, 'Session', Session)
    assert studio.post('/settings/keys/test/AHREFS_API_KEY').status_code == 302
    actual = connections.recorded(output_dir)['AHREFS_API_KEY']
    assert actual['ok'] is False, {'record': actual, 'writing_allowed': setup.ready_to_write(output_dir) == ''}


def test_key_rotated_during_probe_is_not_certified(studio, output_dir, monkeypatch):
    from core import connections, api_keys, setup
    configured(output_dir)
    entered, release = threading.Event(), threading.Event()
    observed = []
    def get(url, **kwargs):
        observed.append(kwargs['headers']['x-api-key'])
        entered.set()
        assert release.wait(10)
        return SimpleNamespace(status_code=200)
    monkeypatch.setattr(connections.requests, 'get', get)
    def probe():
        client = studio.application.test_client()
        return client.post('/settings/keys/test/ANTHROPIC_API_KEY', headers={'Origin': 'http://localhost'})
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(probe)
        assert entered.wait(10)
        try:
            assert studio.post('/settings/keys', data={'ANTHROPIC_API_KEY': 'dummy-rejected-new-key'}).status_code == 302
        finally:
            release.set()
        assert future.result(timeout=10).status_code == 302
    actual = connections.recorded(output_dir).get('ANTHROPIC_API_KEY')
    assert not actual or actual['ok'] is not True, {
        'key_tested': observed, 'key_current': api_keys.current(output_dir, 'ANTHROPIC_API_KEY'),
        'record': actual, 'writing_allowed': setup.ready_to_write(output_dir) == ''}


@pytest.mark.parametrize('path', ['rewrite', 'new_section'])
def test_cli_apply_passes_rules_to_every_writer(output_dir, monkeypatch, capsys, path):
    from core import sample, store, project_texts, qa_model as qm
    from core.agents.base import BaseAgent
    import cli
    rule = 'Always address the reader as you; never refer to the reader as the customer.'
    project_texts.save(output_dir, 'editorial_rules', rule)
    art = store.Repository(output_dir).get(sample.add(output_dir))
    body = '# Buying guide\n\n## Compare choices\n\nYou can compare the choices before selecting a plan for your team.\n\n## Next steps\n\nYou can check the details before making a choice.\n'
    art.write_article('---\ntitle: Buying guide\n---', body)
    fix = ('Rewrite the section "Compare choices" to make the advice clearer.' if path == 'rewrite'
           else 'Write the missing "Review options" section.')
    run = art.run()
    run['review'] = {'reviews': [qm.review_record(id='r2', version=art.version())], 'changes': [],
                     'findings': [qm.finding(id='r2-1', text=fix, review='r2', source='reader',
                                             severity='Major', kind='structure')]}
    art.save_run(run)
    calls = []
    def cached(self, model, base, tail, **kwargs):
        prompt = base + tail + kwargs.get('system', '')
        calls.append(prompt)
        if 'replacing ONE span' in base:
            return json.dumps({'find': '', 'replace': '', 'note': 'Needs a section rewrite.'})
        if 'rewriting ONE SECTION' in base:
            return json.dumps({'section': '## Compare choices\n\nYou can weigh the choices before selecting a plan for your team.'})
        return json.dumps({'after_heading': '## Compare choices', 'section': '## Review options\n\nYou can compare the details before choosing.'})
    monkeypatch.setattr(BaseAgent, 'call_claude_cached', cached)
    monkeypatch.setattr(BaseAgent, 'call_claude_json', lambda *a, **kw: ({'items': [
        {'id': 'r2-1', 'resolved': True, 'why': 'stand-in check', 'new_problem': None}]}, ''))
    monkeypatch.setattr(sys, 'argv', ['cli.py', 'apply', art.slug, 'r2-1', '--version', art.version(),
                                   '--review', 'r2', '--yes', '--json'])
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert calls, result
    assert any(('rewriting ONE SECTION' in p if path == 'rewrite' else 'Write ONE new section' in p) for p in calls), calls
    assert all(rule in p for p in calls), {'path': path, 'calls_with_rules': [rule in p for p in calls], 'cli_result': result}
