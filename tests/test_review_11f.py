"""The review of 11ffa18 (seo-writer-agent-reviews/refactoring-review-11ffa18.md): its probes for
R11F-01..04 kept as tests (the check's own word units, soft line breaks, a draft checked from
the CLI, a text with no prose now)."""
import re
import pytest
from review_9d2_support import article, edit, scan, row, publish_row, raw_answer, cli_json


def provider_scan(studio, art, monkeypatch, responder):
    from core import store
    from core.tools import plagiarism as api
    from ui.jobs import runner
    calls = []
    def post(payload, **kw):
        calls.append(payload['text'])
        return responder(payload['text'])
    monkeypatch.setenv('WINSTON_API_KEY', 'review-only')
    monkeypatch.setattr(api, '_post', post)
    monkeypatch.setattr(runner, 'active', lambda *a, **kw: False)
    monkeypatch.setattr(runner, 'submit_task', lambda title, fn, **kw: fn(lambda *a: None))
    response = studio.post(f'/a/{art.slug}/uniqueness')
    assert response.status_code == 302 and len(calls) == 1
    return store.read_json_lenient(art.path('_plagiarism.json'), {})


@pytest.mark.parametrize('case', ['equal_old_counts', 'unequal_old_counts'])
def test_r11f_01_bound_uses_the_providers_units(studio, output_dir, monkeypatch, case):
    from core.plagiarism_report import article_text, since_check
    # This stand-in consistently counts lexical words, including both sides of a slash.
    # No claim is made about Winston's live tokenizer: the product accepts these counters
    # and has no contract that a provider word is a whitespace-delimited token.
    body = '\n\n'.join(f'Original sentence {i} gives useful information about widgets today.' for i in range(20))
    if case == 'unequal_old_counts':
        body += '\n\nAn optional read/write setting helps local administrators configure widgets.'
    art = article(output_dir, body)
    added = '\n\n'.join(['alpha/beta/gamma/delta/epsilon/zeta/eta/theta/iota/kappa.'] * 3)
    def response(text):
        total = len(re.findall(r'\w+', text))
        matched = 30 if added in text else 0
        result = raw_answer(text, matched=matched, total=total)
        if matched:
            result['sources'][0]['plagiarismFound'] = [{
                'startIndex': text.index(added), 'endIndex': text.index(added) + len(added), 'sequence': added}]
        return result
    checked = provider_scan(studio, art, monkeypatch, response)
    edit(studio, art, body + '\n\n' + added)
    current = row(art)
    visible = publish_row(studio, art)
    bound = since_check(art, checked, art.text())
    # The same responder through the real check establishes the allowed counterexample.
    rescanned = provider_scan(studio, art, monkeypatch, response)
    assert rescanned['uniqueness'] < 90
    # ported (the operator, 2026-10-23: a measure, not a proof): no bound is promised; what was
    # added is more than 5% of the text, so the row asks to check again
    assert current['state'] == 'check_again' and 'lowest' not in bound and bound['new'] >= 5, {
        'old_provider_words': checked['total_words'], 'old_local_words': len(article_text(body).split()),
        'bound': bound, 'visible': visible, 'row': current, 'rescan': rescanned['uniqueness']}


@pytest.mark.parametrize('case', ['rewritten_sentences', 'reflow_only'])
def test_r11f_02_soft_linebreaks_are_not_sentence_boundaries(studio, output_dir, monkeypatch, case):
    from core.plagiarism_report import since_check
    before = ('Every local widget team should\nreview the settings before deployment.\n\n'
              'All new project owners must\ncompare the prices before renewal.')
    after = ('Every local widget team should\ncompare the prices before renewal.\n\n'
             'All new project owners must\nreview the settings before deployment.')
    if case == 'reflow_only':
        after = before.replace('should\n', 'should ').replace('must\n', 'must ')
    art = article(output_dir, before)
    # One fixed synthetic source holds the two rewritten full sentences. Its matcher
    # normalises soft breaks; the five-word fragments alone are not a matched passage.
    source_sentences = ['Every local widget team should compare the prices before renewal.',
                        'All new project owners must review the settings before deployment.']
    def response(text):
        normal = ' '.join(text.split())
        matched = sum(len(sentence.split()) for sentence in source_sentences if sentence in normal)
        result = raw_answer(text, matched=matched)
        if matched:
            result['sources'][0]['plagiarismFound'] = [{
                'startIndex': 0, 'endIndex': len(text), 'sequence': text}]
        return result
    checked = provider_scan(studio, art, monkeypatch, response)
    assert checked['uniqueness'] == 100
    edit(studio, art, after)
    bound = since_check(art, checked, art.text())
    current = row(art)
    visible = publish_row(studio, art)
    expected = 'check_again' if case == 'rewritten_sentences' else 'clear'
    rescanned = provider_scan(studio, art, monkeypatch, response)
    assert rescanned['uniqueness'] == (0 if case == 'rewritten_sentences' else 100)
    assert current['state'] == expected, {'row': current, 'bound': bound, 'visible': visible}
    assert (bound['new_words'] > 0) == (case == 'rewritten_sentences')


def test_r11f_03_cli_draft_check_keeps_the_text_it_read(output_dir, monkeypatch, capsys):
    from core import store, versions
    from core.tools import plagiarism as api
    art = store.Repository(output_dir).reserve('unfinished-widget-guide')
    body = 'The draft explains useful widget features and prices for small teams. ' * 10
    store.atomic_write_text(art.path('04_draft.md'), body)
    monkeypatch.setattr(api, '_post', lambda payload, **kw: raw_answer(payload['text']))
    code, checked = cli_json(monkeypatch, capsys, 'uniqueness', art.slug, '--yes')
    assert code == 0 and checked['uniqueness'] == 100
    saved = store.read_json_lenient(art.path('_plagiarism.json'), {})
    # ported (decided after the review): a draft is no version (the history holds article.md, and
    # a check must not add a version of something else). The result keeps the text it read
    # instead, and no history entry is made, empty or not. The finding's purpose holds: what was
    # checked can be compared later, by its hash.
    from core.plagiarism_report import text_sha, _checked_text
    assert versions.index(art) == [] and saved.get('version_n') is None
    assert text_sha(saved['checked_markdown']) == saved['text_sha']
    assert _checked_text(art, saved) == (None, body)


@pytest.mark.parametrize('body', ['', '![Only an illustration](assets/illustration.png)\n\n```python\nprint("example")\n```'])
def test_r11f_04_empty_current_prose_does_not_claim_the_old_version_is_lost(studio, output_dir, monkeypatch, body):
    from core import versions
    from core.plagiarism_report import article_text, text_sha
    art = article(output_dir, 'The original guide describes useful widget features for local teams. ' * 20)
    checked = scan(studio, art, monkeypatch)
    edit(studio, art, body)
    assert article_text(art.text()) == ''
    # The exact checked version still exists and verifies against the stored result.
    assert text_sha(versions.text_of(art, checked['version_n'])) == checked['text_sha']
    current = row(art)
    visible = publish_row(studio, art)
    assert current['state'] == 'check_again'
    assert 'no longer keeps it' not in visible, {'row': current, 'visible': visible}
