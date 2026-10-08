"""Independent Round 3 entry-point probes; synthetic credentials, offline transport."""
import inspect
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from test_release_probes import configured
from test_release_controls import cli_json


def pass_services(monkeypatch):
    from core import connections
    for env in connections.REQUIRED:
        monkeypatch.setitem(connections.TESTS, env, lambda key: {'ok': True, 'detail': 'offline'})
    monkeypatch.setattr(connections, 'test_wordpress', lambda *a: {'ok': True, 'detail': 'offline'})


@pytest.mark.parametrize('name', ['ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'PARALLEL_API_KEY',
                                 'AHREFS_API_KEY', 'SCRAPFLY_KEY', 'WINSTON_API_KEY', 'WordPress'])
def test_doctor_rotation_during_provider(studio, output_dir, monkeypatch, capsys, name):
    from core import connections
    configured(output_dir)
    pass_services(monkeypatch)
    def rotate(*args):
        updates = ({'WP_BASE_URL': 'https://new-blog.example', 'WP_USERNAME': 'new-editor',
                    'WP_APP_PASSWORD': 'dummy-new-password'} if name == 'WordPress' else
                   {name: 'dummy-new-key'})
        assert studio.post('/settings/keys', data=updates).status_code == 302
        return {'ok': True, 'detail': 'old credential passed'}
    if name == 'WordPress':
        monkeypatch.setattr(connections, 'test_wordpress', rotate)
    else:
        monkeypatch.setitem(connections.TESTS, name, rotate)
    code, data = cli_json(monkeypatch, capsys, 'doctor')
    assert code == 1 and data['ok'] is False, data
    row = next(c for c in data['checks'] if c['check'] == ('WordPress' if name == 'WordPress' else
                                                        __import__('core.api_keys', fromlist=['label']).label(name)))
    assert row['ok'] is False and 'changed during the test' in row['detail']
    assert name not in connections.recorded(output_dir)


@pytest.mark.parametrize('name', ['ANTHROPIC_API_KEY', 'WordPress'])
def test_doctor_rotation_during_browser(studio, output_dir, monkeypatch, capsys, name):
    from core import connections
    from playwright.sync_api import BrowserType
    configured(output_dir)
    pass_services(monkeypatch)
    original = BrowserType.launch
    def launch(self, *args, **kwargs):
        updates = ({'WP_BASE_URL': 'https://new-blog.example', 'WP_USERNAME': 'new-editor',
                    'WP_APP_PASSWORD': 'dummy-new-password'} if name == 'WordPress' else
                   {name: 'dummy-new-key'})
        assert studio.post('/settings/keys', data=updates).status_code == 302
        return original(self, *args, **kwargs)
    monkeypatch.setattr(BrowserType, 'launch', launch)
    code, data = cli_json(monkeypatch, capsys, 'doctor')
    assert name not in connections.recorded(output_dir)
    assert data['state']['done'] is False
    assert code == 1 and data['ok'] is False, {'code': code, 'answer': data}


@pytest.mark.parametrize('endpoint', ['/settings/keys/test-all', '/settings/wordpress/test'])
def test_connection_flash_after_rotation(studio, output_dir, monkeypatch, endpoint):
    from core import connections
    configured(output_dir)
    pass_services(monkeypatch)
    def rotate(*args):
        assert studio.application.test_client().post('/settings/keys', headers={'Origin': 'http://localhost'}, data={
            'WP_BASE_URL': 'https://new-blog.example', 'WP_USERNAME': 'new-editor',
            'WP_APP_PASSWORD': 'dummy-new-password'}).status_code == 302
        return {'ok': True, 'detail': 'old site passed'}
    monkeypatch.setattr(connections, 'test_wordpress', rotate)
    assert studio.post(endpoint).status_code == 302
    assert 'WordPress' not in connections.recorded(output_dir)
    with studio.session_transaction() as sess:
        messages = [m for _, m in sess.get('_flashes', [])]
    assert not any('all work.' in m or m.startswith('Connected: ') for m in messages), messages


def test_cli_setup_test_after_rotation(studio, output_dir, monkeypatch, capsys):
    from core import connections
    configured(output_dir)
    def rotate(key):
        assert key == 'dummy-entered-key'
        assert studio.post('/settings/keys', data={'ANTHROPIC_API_KEY': 'dummy-new-key'}).status_code == 302
        return {'ok': True, 'detail': 'entered key passed'}
    monkeypatch.setitem(connections.TESTS, 'ANTHROPIC_API_KEY', rotate)
    code, data = cli_json(monkeypatch, capsys, 'setup', '--anthropic-key', 'dummy-entered-key')
    assert 'ANTHROPIC_API_KEY' not in connections.recorded(output_dir)
    assert data['state']['untested'] == ['Anthropic']
    assert data['tests']['Anthropic']['ok'] is not True, data


def test_publish_does_not_mix_environment_during_save(studio, output_dir, monkeypatch, capsys):
    from core import api_keys, sample, store
    from core.tools import wp_publisher
    configured(output_dir)
    slug = sample.add(output_dir)
    version = store.Repository(output_dir).get(slug).version()
    start_save, half_applied, resume_save = threading.Event(), threading.Event(), threading.Event()
    saving = []
    original_set = os._Environ.__setitem__
    original_init = wp_publisher.WPPublisher.__init__
    paused = []
    def setitem(self, key, value):
        if (saving and threading.get_ident() == saving[0] and key == 'WP_APP_PASSWORD'
                and value == 'dummy-new-site-password' and not paused):
            paused.append(True)
            half_applied.set()
            assert resume_save.wait(10)
        return original_set(self, key, value)
    def init(self, *args, **kwargs):
        start_save.set()
        assert half_applied.wait(10)
        try:
            original_init(self, *args, **kwargs)
        finally:
            resume_save.set()
    monkeypatch.setattr(os._Environ, '__setitem__', setitem)
    monkeypatch.setattr(wp_publisher.WPPublisher, '__init__', init)
    seen = []
    def get(url, **kwargs):
        seen.append((url, kwargs.get('auth')))
        # Stop at the first real authenticated request, before media uploads/post writes.
        return SimpleNamespace(status_code=401, json=lambda: {}, raise_for_status=lambda: None)
    monkeypatch.setattr(wp_publisher.requests, 'get', get)
    def save():
        assert start_save.wait(10)
        saving.append(threading.get_ident())
        return studio.application.test_client().post('/settings/keys', headers={'Origin': 'http://localhost'}, data={
            'WP_BASE_URL': 'https://new-blog.example', 'WP_USERNAME': 'new-editor',
            'WP_APP_PASSWORD': 'dummy-new-site-password'})
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(save)
        try:
            response = studio.post(f'/a/{slug}/publish', data={'version': version, 'action': 'draft'})
            assert response.status_code == 302
        finally:
            resume_save.set()
        assert future.result(timeout=10).status_code == 302
    assert paused
    leaked = [(url, auth) for url, auth in seen if url.startswith('https://new-blog.example/')
              and auth and auth[1] == 'dummy-password']
    assert not leaked, {'credential_for': 'https://blog.northwind.example', 'requests': leaked}


def test_generate_does_not_accept_mixed_fingerprint(studio, output_dir, monkeypatch):
    from core import api_keys, connections, setup
    from ui.jobs import runner
    configured(output_dir)  # A was checked: old URL, editor, dummy-password.
    # B has never passed a check: same URL, a different password.
    assert studio.post('/settings/keys', data={'WP_APP_PASSWORD': 'dummy-intermediate-password'}).status_code == 302
    assert 'WordPress' not in connections.recorded(output_dir)
    original = api_keys._read
    rotated = []
    def read(d):
        value = original(d)
        stack = inspect.stack()
        if (not rotated
                and any(frame.function == 'fingerprint' and tuple(frame.frame.f_locals.get('envs', ())) == api_keys.WP_GROUP
                        for frame in stack)
                and any(frame.function == 'ready_to_write' for frame in stack)):
            rotated.append(True)
            # C has never passed either: a new URL with A's user/password. These are synthetic
            # credentials deliberately shared by the two fixture sites, which save() permits.
            assert studio.application.test_client().post('/settings/keys', headers={'Origin': 'http://localhost'}, data={
                'WP_BASE_URL': 'https://new-blog.example', 'WP_USERNAME': 'editor',
                'WP_APP_PASSWORD': 'dummy-password'}).status_code == 302
        return value
    monkeypatch.setattr(api_keys, '_read', read)
    queued = []
    monkeypatch.setattr(runner, 'submit', lambda *args, **kwargs: queued.append((args, kwargs)) or 'offline-job')
    # A correct re-read must test C, then refuse it. No model/pipeline is invoked.
    monkeypatch.setattr(connections, 'test_wordpress', lambda *args: {'ok': False, 'detail': 'new site refused'})
    response = studio.post('/run', data={'topic': 'Recruiting tips'})
    assert response.status_code == 302 and rotated
    assert setup.state(output_dir)['done'] is False
    assert not queued, {'queued': queued, 'state': setup.state(output_dir)}


@pytest.mark.parametrize('endpoint', ['/settings/wordpress/test', '/settings/keys/test-all'])
@pytest.mark.parametrize('binding', ['host', 'user', 'missing_metadata', 'environment_password'])
def test_snapshot_binding_refused_before_transport(studio, output_dir, monkeypatch, endpoint, binding):
    from core import api_keys, connections, store
    configured(output_dir)
    # Legacy/inconsistent saved credentials are synthetic fixtures, never a production folder.
    raw = api_keys._read(output_dir)
    if binding == 'host':
        raw['_meta']['wp_host'] = 'other-site.example'
    elif binding == 'user':
        raw['_meta']['wp_user'] = 'other-editor'
    elif binding == 'missing_metadata':
        raw.pop('_meta', None)
    else:
        raw.pop('WP_APP_PASSWORD')
        raw.pop('_meta', None)
        monkeypatch.setitem(api_keys._ORIGINAL, 'WP_APP_PASSWORD', 'dummy-server-password')
        monkeypatch.setitem(api_keys._ORIGINAL, 'WP_BASE_URL', 'https://server-site.example')
        monkeypatch.setitem(api_keys._ORIGINAL, 'WP_USERNAME', 'server-editor')
    store.write_json(api_keys.file_path(output_dir), raw, mode=0o600)
    for env in connections.REQUIRED:
        monkeypatch.setitem(connections.TESTS, env, lambda key: {'ok': True, 'detail': 'offline'})
    sent = []
    monkeypatch.setattr(connections.requests, 'get', lambda *a, **kw: sent.append((a, kw)))
    assert studio.post(endpoint).status_code == 302
    assert not sent
    assert connections.recorded(output_dir)['WordPress']['ok'] is False
