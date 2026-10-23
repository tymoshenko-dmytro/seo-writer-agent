"""The review of 8b9b9eb: its controls kept as tests."""
import pytest

from review_9d2_support import article, edit, row, publish_row, scan, raw_answer, cli_json
from test_review_11f import provider_scan


@pytest.mark.parametrize('entry', ['cli', 'studio'])
def test_draft_check_preserves_exact_markdown_without_any_version(output_dir, studio, monkeypatch, capsys, entry):
    from core import store, versions
    from core.plagiarism_report import text_sha, _checked_text
    from core.tools import plagiarism as api
    art = store.Repository(output_dir).reserve('synthetic-unfinished-guide')
    md = '# Draft guide\n\n' + 'The useful draft describes available widgets and their pricing for teams. ' * 10
    store.atomic_write_text(art.path('04_draft.md'), md)
    if entry == 'cli':
        monkeypatch.setattr(api, '_post', lambda payload, **kw: raw_answer(payload['text']))
        code, checked = cli_json(monkeypatch, capsys, 'uniqueness', art.slug, '--yes')
        assert code == 0
    else:
        checked = provider_scan(studio, art, monkeypatch, lambda text: raw_answer(text))
    assert checked['checked_markdown'] == md
    assert checked['text_sha'] == text_sha(md)
    assert versions.index(art) == [] and not art.path('article.md').exists()
    assert _checked_text(art, checked) == (None, md)


@pytest.mark.parametrize('case', ['intact', 'bad_hash', 'missing'])
def test_unversioned_checked_text_is_verified_before_comparison(output_dir, studio, monkeypatch, capsys, case):
    from core import store
    from core.tools import plagiarism as api
    art = store.Repository(output_dir).reserve('synthetic-draft-fallback')
    md = 'The original draft gives useful widget advice to small local teams. ' * 20
    store.atomic_write_text(art.path('04_draft.md'), md)
    monkeypatch.setattr(api, '_post', lambda payload, **kw: raw_answer(payload['text']))
    code, checked = cli_json(monkeypatch, capsys, 'uniqueness', art.slug, '--yes')
    assert code == 0
    if case == 'bad_hash':
        checked['checked_markdown'] = 'This is a different text.'
    elif case == 'missing':
        checked.pop('checked_markdown')
    store.write_json(art.path('_plagiarism.json'), checked)
    # Fixture for the finished article, deliberately different from the checked draft.
    art.write_article('title: Finished widget guide', md + '\n\nOne useful new sentence.')
    current = row(art)
    assert current['state'] == ('clear' if case == 'intact' else 'check_again')
    if case == 'intact':
        assert current['detail'].startswith('An earlier text was checked:')
    else:
        assert 'no longer keeps it' in current['detail']


@pytest.mark.parametrize('layout', ['paragraph', 'list', 'quote_whole_sentences', 'table'])
def test_whole_structural_units_reorder_without_new_words(studio, output_dir, monkeypatch, layout):
    from core.plagiarism_report import since_check
    first = 'Every local widget team should review the settings before deployment.'
    second = 'All new project owners must compare the prices before renewal.'
    def wrap(a, b):
        if layout == 'list':
            return '- ' + a + '\n- ' + b
        if layout == 'quote_whole_sentences':
            return '> ' + a + '\n\n> ' + b
        if layout == 'table':
            return '| Advice |\n|---|\n| ' + a + ' |\n| ' + b + ' |'
        return a + '\n\n' + b
    art = article(output_dir, wrap(first, second))
    checked = scan(studio, art, monkeypatch)
    edit(studio, art, wrap(second, first))
    bound = since_check(art, checked, art.text())
    assert bound['new_words'] == 0 and row(art)['state'] == 'clear'
    assert 'No sentence is new since' in publish_row(studio, art)


@pytest.mark.parametrize('body', ['', '![Only picture](assets/illustration.png)\n\n```python\npass\n```'])
def test_empty_prose_reason_is_explicit_and_history_remains(studio, output_dir, monkeypatch, body):
    from core import versions
    from core.plagiarism_report import text_sha
    art = article(output_dir, 'The original guide describes useful widget features for local teams. ' * 20)
    checked = scan(studio, art, monkeypatch)
    edit(studio, art, body)
    visible = publish_row(studio, art)
    assert row(art)['state'] == 'check_again'
    assert 'the text now has no prose to compare with it' in visible
    assert text_sha(versions.text_of(art, checked['version_n'])) == checked['text_sha']
