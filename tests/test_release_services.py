"""Provider transport stand-ins through CLI doctor; no provider function is replaced."""
import json
from types import SimpleNamespace
import pytest
from test_release_probes import configured
from test_release_controls import cli_json


@pytest.mark.parametrize('refused', [None, 'ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'PARALLEL_API_KEY',
                                    'AHREFS_API_KEY', 'SCRAPFLY_KEY', 'WINSTON_API_KEY', 'WordPress'])
def test_doctor_interprets_free_provider_responses(output_dir, monkeypatch, capsys, refused):
    from core import connections
    configured(output_dir)
    seen = []
    def response(code=200, data=None):
        return SimpleNamespace(status_code=code, json=lambda: {} if data is None else data)
    def get(url, **kw):
        seen.append(('GET', url))
        if 'api.anthropic.com/' in url:
            return response(401 if refused == 'ANTHROPIC_API_KEY' else 200)
        if 'api.openai.com/' in url:
            return response(401 if refused == 'OPENAI_API_KEY' else 200)
        if 'api.parallel.ai/' in url:
            return response(401 if refused == 'PARALLEL_API_KEY' else 404)
        if 'api.scrapfly.io/' in url:
            return response(401 if refused == 'SCRAPFLY_KEY' else 200)
        assert url.startswith('https://blog.northwind.example/wp-json')
        if url.endswith('/wp-json'):
            return response(data={'name': 'Northwind'})
        if url.endswith('/users/me'):
            return response(401 if refused == 'WordPress' else 200,
                            {'id': 4, 'name': 'Editor', 'roles': ['editor']})
        if url.endswith('/categories'):
            return response(data=[{'id': 3, 'name': 'Hiring'}])
        assert url.endswith('/users')
        return response(data=[{'id': 4, 'name': 'Editor', 'roles': ['editor']}])
    def post(url, **kw):
        assert url == 'https://api.gowinston.ai/v2/plagiarism'
        assert kw['json']['text'] == 'test'
        seen.append(('POST-short', url))
        return response(401 if refused == 'WINSTON_API_KEY' else 400)
    class Session:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kw):
            assert url == 'https://api.ahrefs.com/mcp/mcp'
            body = kw['json']
            seen.append((body['method'], url))
            if body['method'] == 'initialize':
                return response(401 if refused == 'AHREFS_API_KEY' else 200, {'result': {}})
            assert body['params']['name'] == 'subscription-info-limits-and-usage'
            return response(data={'result': {'content': [{'type': 'text', 'text': json.dumps({
                'limits_and_usage': {'units_usage_workspace': 12, 'units_limit_workspace': 1000}})}]}})
    monkeypatch.setattr(connections.requests, 'get', get)
    monkeypatch.setattr(connections.requests, 'post', post)
    monkeypatch.setattr(connections.requests, 'Session', Session)
    code, data = cli_json(monkeypatch, capsys, 'doctor')
    assert code == (0 if refused is None else 1), data
    expected = [] if refused is None else [connections.WORDPRESS if refused == 'WordPress' else __import__('core.api_keys', fromlist=['label']).label(refused)]
    assert [c['check'] for c in data['checks'] if not c['ok']] == expected
    assert len(connections.recorded(output_dir)) == 7
    assert data['state']['done'] == (refused is None)
    assert len(seen) >= 8
