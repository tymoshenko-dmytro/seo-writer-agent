"""Round 2: deterministic scheduling, real Flask/CLI entry points, no network."""
import json
import inspect
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from test_release_probes import configured
from test_release_controls import cli_json


def response(data=None, code=200):
    return SimpleNamespace(status_code=code, json=lambda: data or {})


@pytest.mark.parametrize('endpoint', ['/settings/wordpress/test', '/settings/keys/test-all'])
def test_wordpress_snapshot_does_not_mix_credentials(studio, output_dir, monkeypatch, endpoint):
    from core import api_keys, connections
    configured(output_dir)
    old_url = 'https://blog.northwind.example'
    new_url = 'https://new-blog.example'
    entered, release = threading.Event(), threading.Event()
    original_read = api_keys._read
    probing = []
    paused = []
    # Only pause the real read; do not alter its value. A second HTTP client saves a whole new
    # WordPress credential right after the snapshot read the saved keys. (Round 2 paused between
    # three separate reads of api_keys.current(); the fix reads the keys once, so the pause moved
    # to that one read. The expectation is unchanged.)
    def read(d):
        value = original_read(d)
        if (probing and threading.get_ident() == probing[0]
                and not paused and any(f.function == 'snapshot' and f.frame.f_locals.get('name') == connections.WORDPRESS
                                      for f in inspect.stack())):
            paused.append(True)
            entered.set()
            assert release.wait(10)
        return value
    monkeypatch.setattr(api_keys, '_read', read)
    for env in connections.REQUIRED:
        monkeypatch.setitem(connections.TESTS, env, lambda key: {'ok': True, 'detail': 'offline'})
    seen = []
    def get(url, **kw):
        seen.append((url, kw.get('auth')))
        if url.endswith('/wp-json'):
            return response({'name': 'Offline site'})
        if url.endswith('/users/me'):
            return response({'name': 'Editor', 'roles': ['editor']})
        return response([])
    monkeypatch.setattr(connections.requests, 'get', get)
    def probe():
        probing.append(threading.get_ident())
        return studio.application.test_client().post(endpoint, headers={'Origin': 'http://localhost'})
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(probe)
        assert entered.wait(10)
        try:
            assert studio.post('/settings/keys', data={
                'WP_BASE_URL': new_url, 'WP_USERNAME': 'new-editor',
                'WP_APP_PASSWORD': 'dummy-new-site-password'}).status_code == 302
        finally:
            release.set()
        assert future.result(timeout=10).status_code == 302
    leaked = [(url, auth) for url, auth in seen if url.startswith(old_url) and
              auth and auth[1] == 'dummy-new-site-password']
    assert not leaked, {'credential_for': new_url, 'sent_to_old_site': leaked}


def test_doctor_does_not_report_pass_for_rotated_key(studio, output_dir, monkeypatch, capsys):
    from core import connections, setup
    configured(output_dir)
    for env in connections.REQUIRED:
        if env != 'ANTHROPIC_API_KEY':
            monkeypatch.setitem(connections.TESTS, env, lambda key: {'ok': True, 'detail': 'offline'})
    monkeypatch.setattr(connections, 'test_wordpress', lambda *args: {'ok': True, 'detail': 'offline'})
    def get(url, **kwargs):
        assert url == 'https://api.anthropic.com/v1/models'
        assert kwargs['headers']['x-api-key'] == 'dummy-ANTHROPIC_API_KEY'
        assert studio.post('/settings/keys', data={'ANTHROPIC_API_KEY': 'dummy-unchecked-new-key'}).status_code == 302
        return response()
    monkeypatch.setattr(connections.requests, 'get', get)
    code, data = cli_json(monkeypatch, capsys, 'doctor')
    assert 'ANTHROPIC_API_KEY' not in connections.recorded(output_dir)
    assert setup.state(output_dir)['untested'] == ['Anthropic']
    assert code == 1 and data['ok'] is False, {'exit_code': code, 'ok': data['ok'], 'state': data['state']}
