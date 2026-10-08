"""Configuration notes checked against the real publisher with HTTP stand-ins."""
from types import SimpleNamespace
import pytest


@pytest.mark.parametrize('mode', ['acf', 'missing', 'mismatch', 'read_error', 'fallback_error', 'clear', 'omitted'])
def test_wordpress_notes_match_payload_and_faq_result(output_dir, monkeypatch, mode):
    from core.tools import wp_publisher
    from core import store
    article = store.Article(output_dir, 'review-docs')
    faq = [{'question': 'Which option?', 'answer': 'Compare the available options.'}]
    fm = {'title': 'Buying guide', 'slug': 'review-docs', 'meta_title': 'A useful buying guide',
          'meta_description': 'Compare available options.', 'focus_keyword': 'buying guide'}
    if mode != 'omitted':
        fm['faq'] = [] if mode == 'clear' else faq
    article.dir.mkdir()
    article.write_article(store.render_frontmatter(fm), '# Buying guide\n\nUseful body.\n')
    sent = []
    def response(data, code=200):
        return SimpleNamespace(status_code=code, json=lambda: data, raise_for_status=lambda: None)
    def post(url, **kwargs):
        sent.append((url, kwargs['json']))
        if mode == 'fallback_error' and len(sent) == 2:
            return response({}, 500)
        return response({'id': 77, 'status': 'draft', 'link': 'https://blog.example/review-docs/'}, 201)
    def get(url, **kwargs):
        assert url.endswith('/posts/77')
        if mode == 'read_error':
            return response({}, 500)
        if mode in {'acf', 'clear'}:
            return response({'acf': {'faq': [] if mode == 'clear' else faq}})
        if mode == 'mismatch':
            return response({'acf': {'faq': [{'question': 'Old?', 'answer': 'Old.'}]}})
        return response({})
    monkeypatch.setattr(wp_publisher.requests, 'get', get)
    monkeypatch.setattr(wp_publisher.requests, 'post', post)
    wp = wp_publisher.WPPublisher('https://blog.example', 'editor', 'dummy-password', author_id=0)
    result = wp.publish_article(article.dir)
    assert sent[0][1]['meta'] == {'_yoast_wpseo_title': fm['meta_title'],
                                '_yoast_wpseo_metadesc': fm['meta_description'],
                                '_yoast_wpseo_focuskw': 'buying guide'}
    if mode == 'omitted':
        assert 'acf' not in sent[0][1]
        assert result['faq'] == 'unchanged'
    else:
        assert sent[0][1]['acf']['faq'] == fm['faq']
    expected = {'acf': 'acf', 'missing': 'body-fallback', 'mismatch': 'body-fallback',
                'read_error': 'unverified', 'fallback_error': 'not saved', 'clear': 'cleared', 'omitted': 'unchanged'}
    assert result['faq'].startswith(expected[mode]), result
    if mode in {'missing', 'mismatch', 'fallback_error'}:
        assert len(sent) == 2 and '<h2>FAQ</h2>' in sent[1][1]['content']
        assert faq[0]['question'] in sent[1][1]['content']
    else:
        assert len(sent) == 1
    assert bool(result['warnings']) == (mode in {'read_error', 'fallback_error'})
