"""The editor's three faults an outside audit found (2026-10-30): Edit with AI sent the selection
as plain text, so a rewrite lost every link's address; it pasted the model's Markdown as typed
characters, so **bold** stayed stars; and the editor's Redraw kept the old alt text and caption
under the new picture, where the Pictures tab writes the new ones."""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from core import article_edits, store
from core.agents.interactive import VisualProducerService
from core.config import load_core_config, load_project
from ui.views import visuals

ROOT = Path(__file__).resolve().parents[1]
OLD = '# Example\n\n![Old chart](assets/chart_1.png)\n*Old chart caption.*\n'


def _picture_article(output_dir, slug):
    art = store.Article(output_dir, slug)
    art.dir.mkdir()
    art.assets.mkdir()
    Image.new('RGB', (20, 20), 'red').save(art.assets / 'chart_1.png')
    art.write_article('', OLD)
    return art


@pytest.fixture
def new_chart(monkeypatch):
    def make_new(self, directory, rel, *args, **kwargs):
        Image.new('RGB', (20, 20), 'blue').save(Path(directory) / rel)
        return {'ok': True, 'path': rel, 'alt': 'New chart', 'caption': 'New chart caption.'}

    monkeypatch.setattr(VisualProducerService, 'edit_visual', make_new)


def test_editor_redraw_writes_the_new_pictures_words(studio, output_dir, monkeypatch, new_chart):
    art = _picture_article(output_dir, 'audit-picture')
    monkeypatch.setattr(visuals, 'queue_json', lambda label, kind, fn, **kwargs: fn(lambda msg: None))
    initial = art.version()
    r = studio.post('/api/visual-edit', json={
        'slug': art.slug, 'path': 'assets/chart_1.png', 'version': initial, 'feedback': 'New chart'})
    assert r.status_code == 200 and r.json['version'] != initial
    assert '![New chart](assets/chart_1.png)' in art.text() and '*New chart caption.*' in art.text()


def test_editor_redraw_refuses_a_page_older_than_the_article(studio, output_dir, new_chart):
    art = _picture_article(output_dir, 'audit-stale')
    r = studio.post('/api/visual-edit', json={'slug': art.slug, 'path': 'assets/chart_1.png', 'version': 'old'})
    assert r.status_code == 409 and art.text() == OLD


def test_pictures_tab_redraw_is_the_same_path(output_dir, new_chart):
    art = _picture_article(output_dir, 'audit-control')
    article_edits.redraw(art, art.version(), 'assets/chart_1.png', load_core_config(), load_project())
    assert '![New chart]' in art.text() and '*New chart caption.*' in art.text()


EDITOR_PAGE = """<!doctype html><div id="host"></div>
<script>{tiptap}</script><script>{mdblocks}</script>"""

PROBE = """(src) => {
  const e = new TT.Editor({ element: document.getElementById('host'), extensions: MdBlocks.extensions() });
  const state = MdBlocks.load(e, src);
  const end = e.state.doc.content.size - 1;
  const sent = MdBlocks.selectionMarkdown(e, 1, end);
  e.chain().insertContentAt({ from: 1, to: end },
    MdBlocks.parseReplacement(e, 'Read **the source** at [this report](https://example.com/report).')).run();
  const out = { sent, html: e.getHTML(), saved: MdBlocks.save(e, state), blocks: e.getJSON().content.length };
  e.destroy();
  return out;
}"""


def test_ai_edit_sends_markdown_and_applies_it_as_marks():
    from playwright.sync_api import sync_playwright

    page_html = EDITOR_PAGE.format(
        tiptap=(ROOT / 'ui/static/vendor/tiptap/tiptap.js').read_text(encoding='utf-8'),
        mdblocks=(ROOT / 'ui/static/js/mdblocks.js').read_text(encoding='utf-8'))
    with sync_playwright() as p:
        browser = p.chromium.launch(args=['--no-sandbox', '--single-process', '--no-zygote'], chromium_sandbox=False)
        try:
            page = browser.new_page()
            page.route('**/*', lambda route: route.abort())
            page.set_content(page_html)
            out = page.evaluate(PROBE, 'Read [the source](https://example.com/report) now.\n')
        finally:
            browser.close()
    assert out['sent'] == 'Read [the source](https://example.com/report) now.'
    assert '<strong>the source</strong>' in out['html'] and '**' not in out['html']
    assert out['blocks'] == 1                       # a rewrite inside a paragraph does not split it
    assert out['saved'] == 'Read **the source** at [this report](https://example.com/report).\n'
