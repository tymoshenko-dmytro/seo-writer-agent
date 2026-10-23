"""The review of 8b9b9eb (seo-writer-agent-reviews/refactoring-review-8b9b9eb.md): its probes for
R8B9-01..02 kept as tests (the check's count against ours, a quote's soft lines)."""
import re

import pytest

from review_9d2_support import article, edit, row, publish_row, raw_answer
from test_review_11f import provider_scan


@pytest.mark.parametrize('case', ['number_tokens', 'thai_words'])
def test_r8b9_01_word_envelope_cannot_certify_uncontracted_provider_units(
        studio, output_dir, monkeypatch, case):
    from core.plagiarism_report import article_text, since_check

    if case == 'number_tokens':
        # A lexical word counter consistently ignores standalone port numbers.
        # This is permitted by the raw response shape; there is no word-unit contract.
        before = ('The supported service ports for this widget are listed here: '
                  '20 21 22 23 25 53 67 68 80 110 123 143 161 389 443 465 587 636 993 995.')
        added = 'Copy this sentence.'
        def words(text):
            return re.findall(r'[A-Za-z]+', text)
    else:
        # A consistent dictionary tokenizer for a space-free Thai phrase. All its
        # lexical entries below consist of letters; the product regex sees one run.
        before = '\n\n'.join(['The original guide explains useful widget features for local teams.'] * 18)
        thai_words = ['ระบบ', 'ภาษา', 'ไทย', 'ราคา', 'ตลาด', 'เวลา', 'คน', 'งาน', 'รถ', 'ทาง']
        added = '\n\n'.join([''.join(thai_words) + '.'] * 3)
        pattern = '|'.join(re.escape(w) for w in thai_words) + '|[A-Za-z]+'
        def words(text):
            return re.findall(pattern, text)

    art = article(output_dir, before)
    def response(text):
        matched = len(words(added)) if added in text else 0
        result = raw_answer(text, matched=matched, total=len(words(text)))
        if matched:
            result['sources'][0]['plagiarismFound'] = [{
                'startIndex': text.index(added), 'endIndex': text.index(added) + len(added),
                'sequence': added}]
        return result

    checked = provider_scan(studio, art, monkeypatch, response)
    assert checked['uniqueness'] == 100
    edit(studio, art, before + '\n\n' + added)
    current, visible = row(art), publish_row(studio, art)
    bound = since_check(art, checked, art.text())
    rescanned = provider_scan(studio, art, monkeypatch, response)
    evidence = {'case': case, 'old_provider_words': checked['total_words'],
                'old_local_words': len(article_text(before).split()), 'bound': bound,
                'row': current, 'visible': visible, 'rescan': rescanned['uniqueness']}
    print(evidence)
    assert rescanned['uniqueness'] < 90
    assert current['state'] == 'check_again', evidence
    assert bound is None or 'lowest' not in bound or bound['lowest'] <= rescanned['uniqueness'], evidence


@pytest.mark.parametrize('case', ['rewritten_sentences', 'reflow_only'])
def test_r8b9_02_quote_soft_lines_are_still_parts_of_one_sentence(
        studio, output_dir, monkeypatch, case):
    from core.plagiarism_report import article_text, since_check

    before = ('> Every local widget team should\n> review the settings before deployment.\n\n'
              '> All new project owners must\n> compare the prices before renewal.')
    from core.tools.markdown_render import parse
    tokens = parse(before)
    assert sum(t.type == 'paragraph_open' for t in tokens) == 2
    assert sum(k.type == 'softbreak' for t in tokens for k in (t.children or [])) == 2
    after = ('> Every local widget team should\n> compare the prices before renewal.\n\n'
             '> All new project owners must\n> review the settings before deployment.')
    if case == 'reflow_only':
        after = before.replace('should\n> ', 'should ').replace('must\n> ', 'must ')
    source_sentences = ['Every local widget team should compare the prices before renewal.',
                        'All new project owners must review the settings before deployment.']
    def response(text):
        normal = ' '.join(text.split())
        matched = sum(len(s.split()) for s in source_sentences if s in normal)
        result = raw_answer(text, matched=matched)
        if matched:
            result['sources'][0]['plagiarismFound'] = [{
                'startIndex': 0, 'endIndex': len(text), 'sequence': text}]
        return result

    art = article(output_dir, before)
    checked = provider_scan(studio, art, monkeypatch, response)
    assert checked['uniqueness'] == 100
    edit(studio, art, after)
    current, visible = row(art), publish_row(studio, art)
    bound = since_check(art, checked, art.text())
    rescanned = provider_scan(studio, art, monkeypatch, response)
    expected = 'check_again' if case == 'rewritten_sentences' else 'clear'
    assert rescanned['uniqueness'] == (0 if case == 'rewritten_sentences' else 100)
    if case == 'reflow_only':
        assert ' '.join(article_text(before).split()) == ' '.join(article_text(after).split())
    evidence = {'case': case, 'row': current, 'bound': bound,
                'visible': visible, 'rescan': rescanned['uniqueness']}
    print(evidence)
    assert current['state'] == expected, evidence
    assert (bound['new_words'] > 0) == (case == 'rewritten_sentences')
