"""The review of 9d2d7e4: its controls kept as tests (current and earlier results, history,
sentence matching, the row's check, its price, the send dialog, the CLI)."""
from __future__ import annotations
import math
import re
import sys
import pytest
from review_9d2_support import article, edit, scan, row, publish_row, cli_json, raw_answer

SENTENCES = [f'Original sentence {i} gives one useful detail about widgets today.' for i in range(100)]
BODY = '\n\n'.join(SENTENCES)


def fixture_result(art, uniqueness=99, verdict='ok', **extra):
    from core import store
    from core.plagiarism_report import text_sha
    data = {'uniqueness': uniqueness, 'verdict': verdict, 'text_sha': text_sha(art.text()),
            'thresholds': {'warn_below': 90, 'fail_below': 80}, **extra}
    store.write_json(art.path('_plagiarism.json'), data)
    return data


@pytest.mark.parametrize('case,expected', [
    # ported (5c79a91, after the reviewed commit): a result the Studio cannot carry over to this text
    # says "check again"; "not checked" is kept for an article never checked. Neither is clear.
    ('absent', 'not_checked'), ('current_ok', 'clear'), ('current_warn', 'found'),
    ('current_fail', 'found'), ('current_unknown', 'failed'), ('legacy_no_sha', 'check_again'),
    ('stale_unknown', 'check_again'), ('stale_warn', 'found'), ('stale_fail', 'found'),
    ('stale_unknown_numeric', 'check_again'), ('not_kept', 'check_again'),
    ('missing_blob', 'check_again'), ('damaged_blob', 'check_again'),
    ('small_edit', 'clear'), ('large_edit', 'check_again'), ('large_deletion', 'check_again'),
])
def test_all_result_states_and_dialog_counts(studio, output_dir, case, expected):
    from core import store, publication, versions
    from core.plagiarism_report import text_sha
    art = article(output_dir, BODY)
    if case != 'absent':
        u, verdict = (85, 'warn') if case in ('current_warn', 'stale_warn') else (75, 'fail') if case in ('current_fail', 'stale_fail') else (None, 'unknown') if case in ('current_unknown', 'stale_unknown') else (99, 'unknown') if case == 'stale_unknown_numeric' else (99, 'ok')
        data = fixture_result(art, u, verdict)
        if case == 'legacy_no_sha':
            data.pop('text_sha')
        if case == 'not_kept':
            data['text_sha'] = text_sha('A different version which is not kept.')
        store.write_json(art.path('_plagiarism.json'), data)
    if case.startswith('stale_') or case in ('not_kept', 'missing_blob', 'damaged_blob', 'small_edit', 'large_edit', 'large_deletion'):
        body = '\n\n'.join(SENTENCES[:-1]) + '\n\nA fresh sentence adds new useful information about widgets today.'
        if case == 'large_edit':
            body = '\n\n'.join(f'Completely rewritten section {i} gives other suggestions for local businesses.' for i in range(100))
        if case == 'large_deletion':
            body = SENTENCES[0]
        edit(studio, art, body)
    if case in ('missing_blob', 'damaged_blob'):
        blob = art.path('_versions/blobs/' + versions.get(art, 1)['files']['article.md'])
        if case == 'missing_blob':
            blob.unlink()
        else:
            blob.write_text('Damaged synthetic blob')
    current = row(art)
    assert current['state'] == expected, current
    page = studio.get(f'/a/{art.slug}?tab=publish').get_data(as_text=True)
    assert current['detail'] in page
    assert page.count(f'action="/a/{art.slug}/uniqueness"') == 1
    from core.plagiarism_report import article_text
    assert current['again_cost'] == f'about {2 * len(article_text(art.text()).split()):,} Winston credits'
    checks = publication.pre_send_checks(art)
    found = sum(c['state'] == 'found' for c in checks)
    unchecked = sum(c['state'] in ('not_checked', 'check_again') for c in checks)
    failed = sum(c['state'] == 'failed' for c in checks)
    assert f'The checks above: {found} found, {unchecked} not checked, {failed} could not run.' in page


@pytest.mark.parametrize('known', [True, False, 'wrong_named'])
def test_named_version_is_verified_and_legacy_result_uses_prose_hash(studio, output_dir, monkeypatch, known):
    from core import store
    from core.article_edits import save_seo
    art = article(output_dir, BODY)
    checked = scan(studio, art, monkeypatch)
    assert checked['version_n'] == 1
    save_seo(art, art.version(), meta_title='Search title changed', meta_description='Other description', faq=[])
    if not known:
        checked.pop('version_n')
    if known == 'wrong_named':
        checked['version_n'] = 3
    store.write_json(art.path('_plagiarism.json'), checked)
    edit(studio, art, BODY + '\n\nA fresh sentence adds new useful information about widgets today.')
    expected = 1 if known is True else 2
    assert row(art)['detail'].startswith(f'v{expected} was checked:'), row(art)


@pytest.mark.parametrize('new_body', [
    '\n\n'.join(reversed(SENTENCES)),
    '\n\n'.join(SENTENCES[:-1]),
    '\n\n'.join('  '.join(s.split()) for s in SENTENCES),
    '\n\n'.join(s.upper() for s in SENTENCES),
])
def test_unchanged_sentence_content_is_recognised(studio, output_dir, monkeypatch, new_body):
    art = article(output_dir, BODY)
    scan(studio, art, monkeypatch)
    edit(studio, art, new_body)
    assert row(art)['state'] == 'clear', row(art)


@pytest.mark.parametrize('before,after', [
    ('The widget works well.', 'The widget works poorly.'),
    ('How does this widget work?', 'How does this widget fail?'),
    ('It handles widgets!', 'It handles widgets?'),
    ('The plan costs 12.50 dollars.', 'The plan costs 13.50 dollars.'),
    ('Dr. Smith evaluates widgets.', 'Dr. Smith evaluates gadgets.'),
    ('"The widget works."', '"The widget fails."'),
    ('Widgets help teams without a final period', 'Widgets harm teams without a final period'),
    ('Виджет помогает команде.', 'Виджет мешает команде.'),
])
def test_rewritten_sentence_edges_cannot_be_clear(studio, output_dir, before, after):
    art = article(output_dir, before)
    fixture_result(art, 100)
    edit(studio, art, after)
    assert row(art)['state'] == 'check_again', row(art)


def test_row_action_runs_the_real_worker_and_new_check_replaces_stale_result(studio, output_dir, monkeypatch):
    art = article(output_dir, BODY)
    first = scan(studio, art, monkeypatch)
    edit(studio, art, 'Entirely new words replace the original article for this independent control. ' * 15)
    assert row(art)['state'] == 'check_again'
    second = scan(studio, art, monkeypatch)
    assert first['version_n'] == 1 and second['version_n'] == 2
    assert row(art)['state'] == 'clear' and row(art)['detail'] == '100.0% unique'


def test_row_action_without_key_or_while_busy_does_not_run(studio, output_dir, monkeypatch):
    from ui.jobs import runner
    art = article(output_dir, BODY)
    calls = []
    monkeypatch.setattr(runner, 'submit_task', lambda *a, **kw: calls.append((a, kw)))
    response = studio.post(f'/a/{art.slug}/uniqueness', follow_redirects=True)
    assert response.status_code == 200 and not calls
    monkeypatch.setenv('WINSTON_API_KEY', 'review-stand-in')
    monkeypatch.setattr(runner, 'active', lambda *a, **kw: True)
    response = studio.post(f'/a/{art.slug}/uniqueness', follow_redirects=True)
    assert response.status_code == 200 and not calls


def test_cli_estimate_dry_run_real_scan_and_publish_json(studio, output_dir, monkeypatch, capsys):
    from core.tools import plagiarism as api
    from core.tools import wp_publisher
    from core.plagiarism_report import article_text
    art = article(output_dir, BODY)
    def forbidden(*a, **kw):
        raise AssertionError('An estimate must not call Winston')
    monkeypatch.setattr(api, '_post', forbidden)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False)
    code, estimated = cli_json(monkeypatch, capsys, 'uniqueness', art.slug)
    assert code == 1 and estimated['refused'] == 'needs_yes'
    code, dry = cli_json(monkeypatch, capsys, 'uniqueness', art.slug, '--dry-run')
    assert code == 0 and dry['dry_run'] and dry['version_n'] == 1
    assert dry['estimated_credits'] == 2 * len(article_text(art.text()).split())
    monkeypatch.setattr(api, '_post', lambda payload, **kw: raw_answer(payload['text']))
    code, checked = cli_json(monkeypatch, capsys, 'uniqueness', art.slug, '--yes')
    assert code == 0 and checked['version_n'] == 1
    edit(studio, art, 'The completely different text now has other words for the new widget guide. ' * 15)
    class WordPress:
        configured = True
        refused = ''
        site = 'https://blog.widgets.example'
        def find_by_slug(self, slug): return None
        def publish_article(self, *a, **kw):
            return {'post_id': 123, 'status': kw['status'], 'link': self.site + '/guide'}
    monkeypatch.setattr(wp_publisher, 'WPPublisher', WordPress)
    code, sent = cli_json(monkeypatch, capsys, 'publish', art.slug)
    assert code == 0 and sent['outcome'] == 'sent'
    c = next(c for c in sent['checks'] if c['check'] == 'uniqueness')
    assert c == row(art) and c['state'] == 'check_again'
    # The ordinary CLI consumes the same details and does not need an enum update.
    import cli
    monkeypatch.setattr(sys, 'argv', ['cli.py', 'publish', art.slug])
    cli.main()
    text = capsys.readouterr().out
    assert c['detail'] in text and 'SENT post #123' in text


@pytest.mark.parametrize('width', [1366, 390])
def test_browser_row_button_and_send_dialog(studio, output_dir, width):
    from pathlib import Path
    from playwright.sync_api import sync_playwright
    from core import publication
    art = article(output_dir, BODY)
    fixture_result(art)
    edit(studio, art, 'A completely different article has entirely new words about the changed product. ' * 20)
    assert row(art)['state'] == 'check_again'
    markup = studio.get(f'/a/{art.slug}?tab=publish').get_data(as_text=True)
    root = Path(__file__).resolve().parents[1]
    checks = publication.pre_send_checks(art)
    expected_count = sum(c['state'] in ('not_checked', 'check_again') for c in checks)
    with sync_playwright() as p:
        browser = p.chromium.launch(args=['--no-sandbox', '--single-process', '--no-zygote'], chromium_sandbox=False)
        try:
            page = browser.new_page(viewport={'width': width, 'height': 1000})
            page.route('**/*', lambda route: route.abort())
            page.set_content(markup)
            page.add_style_tag(path=str(root / 'ui/static/tokens.css'))
            page.add_style_tag(path=str(root / 'ui/static/ui.css'))
            page.add_script_tag(path=str(root / 'ui/static/js/ui.js'))
            page.evaluate("""() => { window.submitted = []; document.addEventListener('submit', e => {
                e.preventDefault(); window.submitted.push({action: e.target.getAttribute('action'), method: e.target.method});
            }); }""")
            form = page.locator(f'form[action="/a/{art.slug}/uniqueness"]')
            assert form.count() == 1 and form.locator('button').is_visible()
            assert row(art)['again_cost'] in form.inner_text()
            form.locator('button').click()
            assert page.evaluate('window.submitted') == [{'action': f'/a/{art.slug}/uniqueness', 'method': 'post'}]
            page.locator('[data-dialog="send-draft"]').click()
            dialog = page.locator('#send-draft')
            assert dialog.is_visible() and f'{expected_count} not checked' in dialog.inner_text()
        finally:
            browser.close()


def test_cost_is_for_submitted_prose_including_headings_and_link_text(studio, output_dir, monkeypatch, capsys):
    body = '''# Widget buying guide

## Compare choices carefully

Read [the detailed pricing guide](https://widgets.example/a/very/long/link) before selecting a widget for your team.

![An excluded image alt](assets/example.png)

```python
these_words_are_not_article_prose = True
```

| Option | Details |
|---|---|
| Basic | Choose this plan for a small team |
'''
    art = article(output_dir, body)
    before = row(art)
    checked = scan(studio, art, monkeypatch)
    assert before['again_cost'] == f"about {checked['estimated_credits']:,} Winston credits"
    assert checked['estimated_credits'] == 2 * checked['total_words']
    code, dry = cli_json(monkeypatch, capsys, 'uniqueness', art.slug, '--dry-run')
    assert code == 0 and dry['estimated_credits'] == checked['estimated_credits']
