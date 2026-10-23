"""The review of 11ffa18: its controls kept as tests."""
import math
import pytest
from review_9d2_support import article, edit, scan, row, publish_row, cli_json, raw_answer


# ported (the operator, 2026-10-23: a measure, not a proof): no bound is promised. Keeping 19-21
# of 4,445 words leaves nearly all the checked text gone, so the row asks to check again
@pytest.mark.parametrize('keep_words', [19, 20, 21])
@pytest.mark.parametrize('legacy', [False, True])
def test_rounded_counts_and_exact_warn_boundary(studio, output_dir, monkeypatch, keep_words, legacy):
    from core import store
    from core.plagiarism_report import since_check
    kept = ' '.join(f'kept{i}' for i in range(keep_words)) + '.'
    removed = ' '.join(f'removed{i}' for i in range(4445 - keep_words)) + '.'
    art = article(output_dir, kept + '\n\n' + removed)
    checked = scan(studio, art, monkeypatch, matched=2)
    assert checked['uniqueness'] == 99.96
    if legacy:
        checked.pop('total_words')
        checked.pop('plagiarised_words')
        store.write_json(art.path('_plagiarism.json'), checked)
    edit(studio, art, kept)
    bound = since_check(art, checked, art.text())
    assert 'lowest' not in bound and bound['gone'] > 99 and bound['new_words'] == 0
    assert row(art)['state'] == 'check_again', row(art)


@pytest.mark.parametrize('copies', [1, 2, 10])
@pytest.mark.parametrize('fresh', [False, True])
def test_multiplicity_budget_is_conservative(studio, output_dir, monkeypatch, copies, fresh):
    from core.plagiarism_report import since_check
    copied = 'This quoted sentence contains ten words about widgets and teams.'
    originals = [f'Original detail {i} explains safe widget handling for local teams.' for i in range(19)]
    art = article(output_dir, '\n\n'.join([copied] + originals))
    checked = scan(studio, art, monkeypatch, matched=10)
    extra = ['A fresh sentence adds new useful information about widgets today.'] if fresh else []
    edit(studio, art, '\n\n'.join(list(reversed(originals)) + [copied] * copies + extra))
    bound = since_check(art, checked, art.text())
    # ported (the operator, 2026-10-23: a measure, not a proof): each copy beyond the checked one
    # counts as new, and the row follows the 5% rule
    assert bound['new_words'] == 10 * (copies - 1 + fresh)
    assert row(art)['state'] == ('clear' if bound['new'] < 5 and bound['gone'] < 5 else 'check_again')
    assert row(art)['state'] == ('check_again' if copies == 10 else row(art)['state'])


@pytest.mark.parametrize('change', ['tiny', 'delete'])
def test_nonzero_share_and_exact_zero_are_distinct(studio, output_dir, monkeypatch, change):
    from core.plagiarism_report import since_check
    sentences = [f'Original sentence {i} gives one useful detail about widgets today.' for i in range(250)]
    art = article(output_dir, '\n\n'.join(sentences))
    checked = scan(studio, art, monkeypatch)
    replacement = ['This sentence was rewritten with new words about widgets today.'] if change == 'tiny' else []
    edit(studio, art, '\n\n'.join(sentences[:-1] + replacement))
    s = since_check(art, checked, art.text())
    assert row(art)['state'] == 'clear'
    text = publish_row(studio, art)
    if change == 'tiny':
        # ported (2026-10-23): the share is of letters now, not words; still under 1%
        assert 0 < s['new'] < 1 and 'Under 1%' in text and 'No sentence is new' not in text
    else:
        assert s['new_words'] == 0 and 'No sentence is new' in text


@pytest.mark.parametrize('source', ['article.md', '04_draft.md'])
def test_cli_dry_run_creates_neither_history_nor_result(output_dir, monkeypatch, capsys, source):
    from core import store, versions
    from core.tools import plagiarism as api
    art = store.Repository(output_dir).reserve('dry-widget-guide')
    store.atomic_write_text(art.path(source), 'Useful original words describe the available widgets for local teams. ' * 20)
    monkeypatch.setattr(api, '_post', lambda *a, **kw: pytest.fail('Dry run reached provider'))
    code, result = cli_json(monkeypatch, capsys, 'uniqueness', art.slug, '--dry-run')
    assert code == 0 and result['dry_run']
    assert versions.index(art) == [] and not art.path('_plagiarism.json').exists()


def test_restored_prose_new_scan_binds_to_latest_version(studio, output_dir, monkeypatch):
    from core import versions
    body = 'The checked guide describes important widget features for small teams. ' * 20
    art = article(output_dir, body)
    scan(studio, art, monkeypatch)
    edit(studio, art, body + '\n\nThe editor added another useful detail about all the widgets.')
    versions.restore(art, 1, art.version(), 'reviewer')
    checked = scan(studio, art, monkeypatch)
    assert checked['version_n'] == 3
    edit(studio, art, body + '\n\nOne additional useful sentence now describes the available support plans.')
    assert row(art)['detail'].startswith('v3 was checked:')


def test_markup_wraps_only_the_uniqueness_row(studio, output_dir):
    import re
    art = article(output_dir, 'An original useful article explains available widget features. ' * 20)
    response = studio.get(f'/a/{art.slug}?tab=publish')
    assert response.status_code == 200
    markup = response.get_data(as_text=True)
    rows = re.findall(r'<li><span class="label">([^<]+)</span>(.*?)</li>', markup, re.S)
    assert any(label == 'Uniqueness' for label, _ in rows)
    for label, contents in rows:
        if label == 'Uniqueness':
            assert contents.count('<form ') == 1 and contents.lstrip().startswith('<span><span class="hint">')
        else:
            assert '<form ' not in contents and contents.lstrip().startswith('<span class="hint">')
