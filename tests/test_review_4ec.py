"""Independent acceptance assertions through shipped UI -> Flask -> AIEditor -> Apply -> Save.
Model call and queue scheduling only are replaced by the offline support fixture.
"""
import json
import pytest
from review_4ec_support import opened, ask, apply, saved
from core.tools.markdown_render import parse


def bounds(page, first, last):
    return page.evaluate('''([first,last])=>{let a,b; e.state.doc.descendants((n,p)=>{if(n.isText){if(a===undefined&&n.text.includes(first))a=p+n.text.indexOf(first);if(n.text.includes(last))b=p+n.text.indexOf(last)+last.length}});return {from:a,to:b}}''',[first,last])


def persist(studio,page,art):
    revision=page.locator('input[name=revision]').input_value()
    body=saved(page)
    response=studio.post('/a/'+art.slug+'/edit',data={'body':body,'revision':revision})
    assert response.status_code==302
    assert art.text()==body
    return body


def links(body):
    return [c.attrGet('href') for t in parse(body) for c in (t.children or []) if c.type=='link_open']


def test_r4ec_01_unchanged_neighbors_keep_bytes(studio,output_dir):
    src='Before\n======\n\nFirst.\n\nSecond.\n\nAfter\n-----\n'
    with opened(studio,output_dir,src,'New.') as (page,art):
        ask(page,bounds(page,'First.','Second.'));apply(page)
        ids=page.evaluate('e.getJSON().content.map(n=>n.attrs?.srcId)')
        body=persist(studio,page,art)
        print(json.dumps({'ids':ids,'saved':body}))
        assert body.startswith('Before\n======\n\n') and 'After\n-----\n' in body, body


def test_r4ec_02_reply_definition_overrides_removed_definition(studio,output_dir):
    src='Read [source][ref].\n\n[ref]: /old\n'
    reply='Read [source][ref].\n\n[ref]: /new'
    with opened(studio,output_dir,src,reply) as (page,art):
        ask(page,instruction='Change the source destination to /new.');apply(page)
        hrefs=page.locator('.ProseMirror a').evaluate_all('(els)=>els.map(e=>e.getAttribute("href"))')
        body=persist(studio,page,art)
        print(json.dumps({'hrefs':hrefs,'saved':body}))
        assert links(body)==['/new'], body


@pytest.mark.parametrize('raw,kind,needle',[
    ('> Quoted evidence.','blockquote_open','Quoted evidence.'),
    ('| A | B |\n| --- | --- |\n| X | Y |','table_open','| X | Y |'),
    ('<div>Raw evidence.</div>','literal_html','Raw evidence.'),
])
def test_r4ec_03_raw_reply_inside_list_item_survives_save(studio,output_dir,raw,kind,needle):
    src='- First.\n\n  Second.\n- Tail\n'
    reply='First.\n\n'+raw+'\n\nSecond.'
    with opened(studio,output_dir,src,reply) as (page,art):
        before=page.evaluate('e.getJSON()')
        ask(page,bounds(page,'First.','Second.'));apply(page)
        # the criterion allows an explicit refusal before the document changes, the placement
        # declared unsupported (a raw block is not written inside a list item): then nothing changed
        refused=page.locator('#ask-result').text_content()
        if 'cannot go inside a list' in refused:
            assert page.evaluate('e.getJSON()')==before and art.text()==src
            return
        assert needle in page.locator('.ProseMirror').text_content()
        doc=page.evaluate('e.getJSON()')
        body=persist(studio,page,art)
        print(json.dumps({'doc':doc,'saved':body}))
        assert needle in body and (kind=='literal_html' or any(t.type==kind for t in parse(body))), body


def test_r4ec_04_table_nested_in_reply_is_not_silently_dropped(studio,output_dir):
    src='First.\n\nSecond.\n'
    reply='- Intro.\n\n  | A | B |\n  | --- | --- |\n  | X | Y |\n\n  Last.'
    with opened(studio,output_dir,src,reply) as (page,art):
        ask(page,bounds(page,'First.','Second.'));apply(page)
        doc=page.evaluate('e.getJSON()')
        body=persist(studio,page,art)
        print(json.dumps({'doc':doc,'saved':body}))
        assert any(t.type=='table_open' for t in parse(body)) and '| X | Y |' in body, body


def test_r4ec_05_pending_reply_uses_current_range(studio,output_dir):
    src='Long introduction can be deleted.\n\nTarget.\n'
    with opened(studio,output_dir,src,'New.') as (page,art):
        r=bounds(page,'Target.','Target.')
        page.evaluate('''()=>{const original=fetch; window.fetch=async(...args)=>{const response=await original(...args);await new Promise(resolve=>window.releaseReply=resolve);return response}}''')
        page.evaluate('''r=>{e.commands.setTextSelection(r);document.querySelector('[data-mark=ai]').click();document.querySelector('#ask-input').value='Rewrite';document.querySelector('#ask-actions button').click()}''',r)
        page.wait_for_function("typeof window.releaseReply === 'function'")
        page.evaluate('''()=>{let end;e.state.doc.forEach((n,p)=>{if(n.textContent==='Target.')end=p});e.commands.deleteRange({from:0,to:end});releaseReply()}''')
        page.wait_for_function("document.querySelector('#ask-actions').textContent.includes('Apply') || document.querySelector('#ask-result .is-error')")
        error=page.locator('#ask-result').text_content()
        assert page.locator('#ask-actions button').filter(has_text='Apply').count()==1,error
        apply(page)
        body=persist(studio,page,art)
        assert body.strip()=='New.',body


@pytest.mark.parametrize('src,reply,kind,start',[
    ('7. First\n8. Second\n','3. One\n4. Two','ordered_list_open',3),
    ('- First\n- Second\n','3. One\n4. Two','ordered_list_open',3),
    ('7. First\n8. Second\n','- One\n- Two','bullet_list_open',None),
    ('- First\n- Second\n','One paragraph.','paragraph_open',None),
])
def test_r4ec_06_whole_list_replacement_honors_reply_structure(studio,output_dir,src,reply,kind,start):
    with opened(studio,output_dir,src,reply) as (page,art):
        ask(page,bounds(page,'First','Second'),'Replace the entire list with this structure: '+reply);apply(page)
        body=persist(studio,page,art)
        tokens=parse(body)
        lists=[t for t in tokens if t.type in ('bullet_list_open','ordered_list_open')]
        print(json.dumps({'src':src,'reply':reply,'saved':body}))
        if kind=='paragraph_open': assert not lists,body
        else:
            assert len(lists)==1 and lists[0].type==kind,body
            if start is not None: assert int(lists[0].attrGet('start') or 1)==start,body
            assert len([t for t in tokens if t.type=='list_item_open'])==2,body
