"""Positive controls and observed pre-existing limits, through the shipped editor."""
import json
import pytest
from review_dd8_support import opened,ask,apply,saved
from test_review_dd8 import text_bounds,persist

@pytest.mark.parametrize('src,reply,bounds,expect',[
 ('Read [source](https://example.com/report) now.\n','Read **the source** at [report](https://example.com/report).',None,'<strong>the source</strong>'),
 ('## Old heading\n','## **New** heading',{'from':1,'to':12},'<h2><strong>New</strong> heading</h2>'),
 ('- Old item\n','**New** item',{'from':3,'to':11},'<li><p><strong>New</strong> item</p></li>'),
 ('One.\n\nTwo.\n','**First.**\n\nSecond.',None,'<p><strong>First.</strong></p><p>Second.</p>'),
 # a quote is a raw block in this editor, in the reply as on load (mdblocks RAW_TYPES): shown as
 # its Markdown, saved as a quote (the reviewer's check of a <blockquote> node, 2026-10-30, moved
 # to the saved text in test_quote_in_reply_is_saved_as_a_quote)
 ('First.\n\n> Quoted.\n\nLast.\n','First.\n\n> Reworded.\n\nLast.',None,'&gt; Reworded.'),
 ('Before ![Pic](assets/chart_1.png) after.\n','Before ![Pic](assets/chart_1.png) afterward.',None,'alt="Pic"'),
 ('Read [source][ref].\n\n[ref]: /report\n','Read [report](/report).',{'from':1,'to':13},'href="/report"'),
 ('Keep words.\n','```markdown\n**New** [source](/report)\n```',None,'<strong>New</strong>'),
])
def test_supported_rewrites(studio,output_dir,src,reply,bounds,expect):
    with opened(studio,output_dir,src,reply) as (page,art):
        ask(page,bounds or text_bounds(page)); apply(page)
        assert expect in page.locator('.ProseMirror').inner_html()
        body=persist(studio,page,art)
        assert body.strip()

def test_quote_in_reply_is_saved_as_a_quote(studio,output_dir):
    from core.tools.markdown_render import parse
    with opened(studio,output_dir,'First.\n\n> Quoted.\n\nLast.\n','First.\n\n> Reworded.\n\nLast.') as (page,art):
        ask(page,text_bounds(page)); apply(page)
        body=persist(studio,page,art)
        assert '> Reworded.' in body and any(t.type=='blockquote_open' for t in parse(body)), body

@pytest.mark.parametrize('reply',['','   ','```markdown\n\n```'])
def test_empty_rewrite_refused(studio,output_dir,reply):
    with opened(studio,output_dir,'Keep words.\n',reply) as (page,art):
        ask(page)
        assert 'came back empty' in page.locator('#ask-result').text_content()
        assert saved(page)=='Keep words.\n'

@pytest.mark.parametrize('src,expect',[
 ('Prefix old suffix.\n','<p> suffix.</p>'),
 ('## Prefix old suffix.\n','<h2> suffix.</h2>'),
 ('- Prefix old suffix.\n','<p> suffix.</p></li>'),
])
def test_several_blocks_into_partial_selection(studio,output_dir,src,expect):
    with opened(studio,output_dir,src,'One.\n\nTwo.') as (page,art):
        bounds=page.evaluate("() => {let p;e.state.doc.descendants((n,pos)=>{if(n.isText&&n.text.includes('old'))p=pos+n.text.indexOf('old')});return {from:p,to:p+3}}")
        ask(page,bounds);apply(page)
        html=page.locator('.ProseMirror').inner_html()
        assert expect in html and 'Prefix ' in html and '<p>One.</p><p>Two.</p>' in html
        persist(studio,page,art)

@pytest.mark.parametrize('marked',[False,True])
def test_apply_fallback_documents_existing_limit(studio,output_dir,marked):
    src='Read **bold** and [source](/report).\n' if marked else 'Read words here.\n'
    with opened(studio,output_dir,src,'Changed.') as (page,art):
        ask(page,text_bounds(page))
        page.evaluate("e.commands.insertContentAt(1,'Prefix ')")
        apply(page)
        if marked:
            assert 'not in the article any more' in page.locator('#ask-result').text_content()
            assert 'Read' in page.locator('.ProseMirror').text_content()
        else: assert page.locator('.ProseMirror').text_content()=='Prefix Changed.'


def test_dirty_redraw_refused_after_refresh(studio,output_dir):
    with opened(studio,output_dir,'Text.\n\n![Picture](assets/chart_1.png)\n') as (page,art):
        page.evaluate("e.commands.insertContentAt(1,'Unsaved ')")
        page.wait_for_timeout(300)
        page.evaluate("() => {let p;e.state.doc.descendants((n,pos)=>{if(n.type.name==='image')p=pos});e.commands.setNodeSelection(p);document.querySelector('[data-pic=redraw]').click();document.querySelector('#ask-actions button').click()}")
        assert not page.evaluate('calls')
        assert 'Save your changes first' in page.locator('#ask-result').text_content()
