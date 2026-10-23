"""The review of 9d2d7e4 (seo-writer-agent-reviews/refactoring-review-9d2d7e4.md): its fixtures,
the real Studio and CLI with Winston replaced at its boundary."""
from __future__ import annotations
import json
import sys


def article(root, body, keep=True):
    from core import store, versions
    art = store.Repository(root).reserve('review-widget-guide')
    art.write_article('---\ntitle: Widget guide\nslug: review-widget-guide\n---', body)
    if keep:
        with art.lock(holder='review fixture'):
            versions.ensure(art)
    return art


def edit(studio, art, body):
    response = studio.post(f'/a/{art.slug}/edit', data={'revision': art.revision(), 'body': body})
    assert response.status_code == 302, response.get_data(as_text=True)


def raw_answer(text, matched=0, total=None):
    words = len(text.split()) if total is None else total
    sequence = ' '.join(text.split()[:matched])
    sources = [{'url': 'https://source.example/quoted', 'title': 'Synthetic matched source',
                'plagiarismWords': matched, 'identicalWordCounts': matched, 'similarWordCounts': 0,
                'plagiarismFound': [{'startIndex': 0, 'endIndex': len(sequence), 'sequence': sequence}]}] if matched else []
    return {'result': {'textWordCounts': words, 'totalPlagiarismWords': matched,
                       'identicalWordCounts': matched, 'similarWordCounts': 0, 'sourceCounts': int(bool(matched))},
            'sources': sources, 'credits_used': 2 * words, 'credits_remaining': 100000}


def scan(studio, art, monkeypatch, matched=0, total=None):
    from core import store
    from core.tools import plagiarism as api
    from ui.jobs import runner
    monkeypatch.setenv('WINSTON_API_KEY', 'review-stand-in')
    calls = []
    def post(payload, **kw):
        calls.append(payload['text'])
        return raw_answer(payload['text'], matched=matched, total=total)
    monkeypatch.setattr(api, '_post', post)
    monkeypatch.setattr(runner, 'active', lambda *a, **kw: False)
    monkeypatch.setattr(runner, 'submit_task', lambda title, fn, **kw: fn(lambda *a: None))
    response = studio.post(f'/a/{art.slug}/uniqueness')
    assert response.status_code == 302 and len(calls) == 1
    return store.read_json_lenient(art.path('_plagiarism.json'), {})


def row(art):
    from core import publication
    return next(c for c in publication.pre_send_checks(art) if c['check'] == 'uniqueness')


def publish_row(studio, art):
    import html
    import re
    response = studio.get(f'/a/{art.slug}?tab=publish')
    assert response.status_code == 200
    part = '<' + response.get_data(as_text=True).split('>Uniqueness<', 1)[1].split('</li>', 1)[0]
    return ' '.join(html.unescape(re.sub('<[^>]*>', ' ', part)).split())


def cli_json(monkeypatch, capsys, *args):
    import cli
    monkeypatch.setattr(sys, 'argv', ['cli.py', *args, '--json'])
    code = 0
    try:
        cli.main()
    except SystemExit as exc:
        code = exc.code
    return code, json.loads(capsys.readouterr().out)
