"""The review of 7108e86 (seo-writer-agent-reviews/refactoring-review-7108e86.md): its probes for
R710-01, R710-02 kept as tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import standin
from test_review_c91 import SOURCE, prepare, cached_article

URL = 'https://vendor.example/board'
CLAIM = 'Widget answers in 812 ms in our measurement.'


def citation_fixture(tmp_path, monkeypatch, source=SOURCE, instruction=None):
    from core import qa_changes, qa_model as qm
    _, cfg, project = prepare(tmp_path, monkeypatch, source)
    art = cached_article(tmp_path)
    art.write_article('---\ntitle: "Our measurements"\n---\n', '# Measurements\n\n## Results\n\n' + CLAIM + '\n')
    run = art.run()
    run['review'] = {'reviews': [qm.review_record(id='r1', version=art.version())], 'changes': [],
                    'findings': [qm.finding(id='r1-1', text=instruction or 'Add the citation for our measurement to this claim.',
                                            review='r1', source='reader', severity='major', kind='fact', action='cite', target=CLAIM)]}
    art.save_run(run)
    qa_changes.seed_state(art, '', art.text(), run, project)
    return art, cfg, project


def verifier(monkeypatch):
    from core.agents.base import BaseAgent
    monkeypatch.setattr(BaseAgent, 'call_claude_json', lambda *a, **kw: (
        {'items': [{'id': 'r1-1', 'resolved': True, 'why': 'fixture', 'new_problem': None}]}, ''))


def test_r710_fixer_is_given_the_source_it_is_asked_to_cite(tmp_path, monkeypatch):
    from core import qa_changes
    from core.agents.base import BaseAgent
    from core.agents.editor import Editor
    art, cfg, project = citation_fixture(tmp_path, monkeypatch, SOURCE.replace('return url ==', 'return False and url =='))
    state = qa_changes.seed_state(art, '', art.text(), art.run(), project)
    assert URL in Editor(project, cfg)._user_message(state)
    prompts = []
    def model(self, model, base, tail='', **kw):
        prompts.append(base + tail)
        # Obey the real citation instruction: choose from SOURCES AN EDIT MAY LINK.
        if URL not in base:
            return json.dumps({'declined': True, 'note': 'The measurement source is absent from the supplied link list.'})
        return json.dumps({'find': CLAIM, 'replace': '[' + CLAIM + '](' + URL + ')'})
    monkeypatch.setattr(BaseAgent, 'call_claude_cached', model)
    verifier(monkeypatch)
    result = qa_changes.fix_again(art.slug, 'r1-1', art.version(), config=cfg, project=project)
    assert '](' + URL + ')' in art.text(), {'result': result, 'source_in_requests': [URL in p for p in prompts]}


def test_r710_source_title_is_not_rejected_as_an_invented_name(tmp_path, monkeypatch):
    from core import qa_changes
    from core.agents.base import BaseAgent
    source = SOURCE.replace('return url ==', 'return False and url ==').replace('LEDGER-v1: ', 'Laboratory Atlas: ')
    art, cfg, project = citation_fixture(tmp_path, monkeypatch, source,
                                        'Add the measurement citation from ' + URL + ' to this claim.')
    replacement = CLAIM + ' ([Laboratory Atlas](' + URL + ')).'
    monkeypatch.setattr(BaseAgent, 'call_claude_cached', lambda *a, **kw: json.dumps({'find': CLAIM, 'replace': replacement}))
    verifier(monkeypatch)
    result = qa_changes.fix_again(art.slug, 'r1-1', art.version(), config=cfg, project=project)
    assert replacement in art.text(), result


@pytest.mark.parametrize('operation', ['create', 'recapture'])
def test_r710_excluded_pages_are_neither_offered_nor_read(tmp_path, monkeypatch, operation):
    from core import qa_changes
    from core.agents.interactive import VisualProducerService
    from core.producers import screenshot
    from core.tools.link_index import LinkIndex, LinkTarget
    _, cfg, project = prepare(tmp_path, monkeypatch)
    cfg['pipeline']['visuals']['vision_check'] = False
    art = cached_article(tmp_path)
    art.write_article('---\ntitle: "Vendor"\n---\n', '# Vendor\n\nVendor overview.\n')
    qa_changes.seed_state(art, '', art.text(), art.run(), project)
    index = LinkIndex([LinkTarget(URL, 'Private board'), LinkTarget('https://vendor.example/help', 'Help')])
    monkeypatch.setattr(LinkIndex, 'load', classmethod(lambda cls, root: index))
    prompts, outlines, captures = [], [], []
    def model(self, user, **kw):
        prompts.append(user)
        return {'producer': 'screenshot', 'spec': {'url': URL}, 'alt': 'Board', 'caption': 'Board'}, ''
    monkeypatch.setattr(VisualProducerService, 'call_claude_json', model)
    monkeypatch.setattr(screenshot, 'outline', lambda url, **kw: (outlines.append(url), [{'heading': 'Private board', 'text': '812 ms'}])[1])
    def capture(url, path, **kw):
        captures.append(url)
        Path(path).write_bytes(standin._background_png())
        return str(path)
    monkeypatch.setattr(screenshot, 'capture', capture)
    service = VisualProducerService(project, cfg)
    if operation == 'create':
        result = service.create(art.dir, 'Add a screenshot.', 'Vendor overview.', '')
        assert not captures and 'error' in result, result
        own_pages = prompts[0].split('OUR OWN PAGES', 1)[1]
        assert URL not in own_pages, own_pages
    else:
        art.assets.mkdir(exist_ok=True)
        pic = art.path('assets/screenshot_1.png')
        pic.write_bytes(standin._background_png())
        pic.with_suffix('.json').write_text(json.dumps({'producer': 'screenshot', 'spec': {'url': URL}}))
        old = pic.read_bytes()
        result = service.edit_visual(art.dir, 'assets/screenshot_1.png', 'Show the details.', alt='Board')
        assert not captures and pic.read_bytes() == old and 'error' in result, result
        assert not outlines, {'read_pages': outlines, 'result': result}
