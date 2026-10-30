"""Acceptance tests: expected to fail on dd8eda5. Real UI -> Flask -> AIEditor -> UI -> Save.
Only AIEditor.call_claude and background scheduling are substituted by reviewer_ui.opened.
"""
import re
import pytest
from review_dd8_support import opened, ask, apply, saved
from core.tools.markdown_render import parse


def persist(studio, page, art):
    revision=page.locator('input[name=revision]').input_value()
    body=saved(page)
    response=studio.post('/a/'+art.slug+'/edit',data={'body':body,'revision':revision})
    assert response.status_code==302
    assert art.text()==body
    return body


def text_bounds(page):
    return page.evaluate('''() => {let spans=[];e.state.doc.descendants((n,p)=>{if(n.isText)spans.push({from:p,to:p+n.nodeSize})});return {from:spans[0].from,to:spans.at(-1).to}}''')


def test_rdd8_01_table_in_reply_is_preserved(studio, output_dir):
    src='First.\n\n| Name | Value |\n| --- | --- |\n| A | B |\n\nLast.\n'
    with opened(studio,output_dir,src,src) as (page,art):
        ask(page,text_bounds(page))
        assert '| A | B |' in page.evaluate('calls[0].body.selection')
        apply(page)
        body=persist(studio,page,art)
        assert any(t.type=='table_open' for t in parse(body)), body


@pytest.mark.parametrize('marker',['-','1.'])
def test_rdd8_02_rewriting_two_items_does_not_nest_list(studio, output_dir, marker):
    src=f'{marker} First\n{marker} Second\n'
    reply=f'{marker} One\n{marker} Two'
    with opened(studio,output_dir,src,reply) as (page,art):
        ask(page,text_bounds(page))
        apply(page)
        body=persist(studio,page,art)
        tokens=parse(body)
        lists=[t for t in tokens if t.type in ('bullet_list_open','ordered_list_open')]
        assert len(lists)==1, body
        assert len([t for t in tokens if t.type=='list_item_open'])==2, body


@pytest.mark.parametrize('reply,expected',[
    ('## New heading','heading_open'),
    ('> New quote','blockquote_open'),
    ('- Only item','bullet_list_open'),
])
def test_rdd8_03_single_block_structure_survives_cross_block_replacement(studio,output_dir,reply,expected):
    with opened(studio,output_dir,'Intro.\n\n## Header\n\nTail.\n',reply) as (page,art):
        ask(page,{'from':1,'to':15},'Combine this fragment into one block: '+reply)
        apply(page)
        body=persist(studio,page,art)
        assert any(t.type==expected for t in parse(body)), body
        assert 'Tail.' in body


def test_rdd8_04_reference_link_survives_definition_replacement(studio,output_dir):
    src='Read [source][ref].\n\n[ref]: /report\n'
    # The producer preserves exactly the selection sent to it, including its definition.
    reply='Read [source](/report).\n\n[ref]: /report'
    with opened(studio,output_dir,src,reply) as (page,art):
        ask(page)
        assert '[ref]: /report' in page.evaluate('calls[0].body.selection')
        apply(page)
        assert page.locator('.ProseMirror a[href="/report"]').count()==1
        body=persist(studio,page,art)
        links=[c for t in parse(body) for c in (t.children or []) if c.type=='link_open']
        assert any(c.attrGet('href')=='/report' for c in links), body


def test_rdd8_05_escaped_reference_is_not_activated_on_save(studio,output_dir):
    src='Keep words.\n\n[ref]: /target\n'
    reply=r'Use \[label\]\[ref\].'
    with opened(studio,output_dir,src,reply) as (page,art):
        ask(page,{'from':1,'to':12})
        apply(page)
        assert page.locator('.ProseMirror a').count()==0
        body=persist(studio,page,art)
        links=[c for t in parse(body) for c in (t.children or []) if c.type=='link_open']
        assert not links, body


def test_rdd8_06_unsaved_changes_refuse_redraw_before_debounce(studio,output_dir):
    with opened(studio,output_dir,'Text.\n\n![Picture](assets/chart_1.png)\n*Caption.*\n') as (page,art):
        # Catch the paid dispatch; never execute a producer. All UI handlers are unmodified.
        page.evaluate('''() => {
          window.fetch=async (url,opts)=>{calls.push({url,body:JSON.parse(opts.body)});return new Promise(()=>{});};
          let pos; e.state.doc.descendants((n,p)=>{if(n.type.name==='image')pos=p});
          e.commands.setNodeSelection(pos);document.querySelector('[data-pic=redraw]').click();
          e.commands.insertContentAt(1,'Unsaved ');
          document.querySelector('#ask-actions button').click();
        }''')
        assert not page.evaluate('calls'), page.evaluate('calls')
        assert 'Save your changes first' in page.locator('#ask-result').text_content()
