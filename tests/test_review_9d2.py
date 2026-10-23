"""The review of 9d2d7e4: its probes for R9D2-01..04 kept as tests (a repeated matched sentence,
a rounded percentage, a small rewrite, the version a first check read)."""
from __future__ import annotations
import pytest
from review_9d2_support import article, edit, scan, row, publish_row, raw_answer, cli_json


@pytest.mark.parametrize('fresh', [False, True])
def test_r9d2_01_repeated_matched_sentence_cannot_become_clear(studio, output_dir, monkeypatch, fresh):
    """A 10-word matched sentence copied 10 times yields 100 matched words, not 10."""
    copied = 'This quoted sentence contains ten words about widgets and teams.'
    assert len(copied.split()) == 10
    unique = [f'Original detail {i} explains safe widget handling for local teams.' for i in range(19)]
    art = article(output_dir, '\n\n'.join([copied] + unique))
    checked = scan(studio, art, monkeypatch, matched=10)
    assert checked['verdict'] == 'ok' and checked['uniqueness'] == 95.0
    added = ['A fresh sentence adds new useful information about widgets today.'] if fresh else []
    edit(studio, art, '\n\n'.join([copied] * 10 + unique + added))
    current = row(art)
    visible = publish_row(studio, art)
    exact_uniqueness = 100 * 190 / (290 + (10 if fresh else 0))
    assert exact_uniqueness < 90
    assert current['state'] == 'check_again', {'row': current, 'visible': visible, 'possible_uniqueness': exact_uniqueness}


def test_r9d2_02_rounded_percentage_is_not_an_exact_match_budget(studio, output_dir, monkeypatch):
    """4445 original words, 2 matched; 99.96% display must not imply only 1.778 matches."""
    kept = ' '.join(f'kept{i}' for i in range(19)) + '.'
    removed = ' '.join(f'removed{i}' for i in range(4426)) + '.'
    art = article(output_dir, kept + '\n\n' + removed)
    checked = scan(studio, art, monkeypatch, matched=2)
    assert checked['total_words'] == 4445 and checked['plagiarised_words'] == 2
    assert checked['uniqueness'] == 99.96
    edit(studio, art, kept)
    current = row(art)
    visible = publish_row(studio, art)
    exact_lower_bound = 100 * 17 / 19
    assert exact_lower_bound < 90
    assert current['state'] == 'check_again', {'row': current, 'visible': visible, 'exact_lower_bound': exact_lower_bound}


def test_r9d2_03_nonzero_rewrite_must_not_say_no_sentence_is_new(studio, output_dir, monkeypatch):
    sentences = [f'Original sentence {i} gives one useful detail about widgets today.' for i in range(250)]
    art = article(output_dir, '\n\n'.join(sentences))
    scan(studio, art, monkeypatch)
    edit(studio, art, '\n\n'.join(sentences[:-1] + ['This sentence was rewritten with new words about widgets today.']))
    current = row(art)
    assert current['state'] == 'clear'
    visible = publish_row(studio, art)
    assert 'No sentence is new' not in visible, {'row': current, 'visible': visible}


@pytest.mark.parametrize('entry', ['studio', 'cli'])
def test_r9d2_04_check_without_existing_history_records_the_version(studio, output_dir, monkeypatch, capsys, entry):
    from core import store, versions
    from core.tools import plagiarism as api
    art = article(output_dir, 'The widget guide explains several useful details for a small team. ' * 10, keep=False)
    assert versions.index(art) == []
    if entry == 'studio':
        checked = scan(studio, art, monkeypatch)
    else:
        monkeypatch.setattr(api, '_post', lambda payload, **kw: raw_answer(payload['text']))
        code, checked = cli_json(monkeypatch, capsys, 'uniqueness', art.slug, '--yes')
        assert code == 0, checked
    saved = store.read_json_lenient(art.path('_plagiarism.json'), {})
    # The first normal SEO save retains the checked text as v1 and creates v2 with the same
    # prose. After an actual prose edit, the result must still identify what was checked.
    from core.article_edits import save_seo
    save_seo(art, art.version(), meta_title='New search title', meta_description='New search description', faq=[])
    edit(studio, art, art.read()[1] + '\n\nOne more sentence with new information for the reader.')
    current = row(art)
    assert checked.get('version_n') == saved.get('version_n') == 1 and current['detail'].startswith('v1 was checked:'), {
        'version_n': saved.get('version_n'), 'row': current, 'versions': [v['n'] for v in versions.index(art)]}


def test_r9d2_04_pipeline_scan_records_the_first_version(tmp_path, monkeypatch):
    import standin
    from core import store, versions
    from core.orchestrator import generate_article
    from core.tools import plagiarism as api
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'profile.yaml').write_text('name: "Northwind Widgets"\nbrand:\n  domains: ["widgets.example"]\nlinks:\n  site_sitemap: ""\n')
    monkeypatch.setenv('CONTENT_PROJECT_DIR', str(project))
    monkeypatch.setenv('CONTENT_OUTPUT_DIR', str(tmp_path / 'output'))
    monkeypatch.setenv('OPENAI_API_KEY', 'review-stand-in')
    monkeypatch.setenv('WINSTON_API_KEY', 'review-stand-in')
    standin.install(monkeypatch)
    monkeypatch.setattr(api, '_post', lambda payload, **kw: raw_answer(payload['text']))
    result = generate_article(standin.TOPIC)
    assert result['status'] == 'success', result.get('error')
    art = store.Repository(tmp_path / 'output').get(result['slug'])
    saved = store.read_json_lenient(art.path('_plagiarism.json'), {})
    from core.plagiarism_report import text_sha
    assert saved['text_sha'] == text_sha(versions.text_of(art, 1))
    assert saved.get('version_n') == 1, {'version_n': saved.get('version_n'), 'versions': [v['n'] for v in versions.index(art)]}
