"""Additional positive controls, beyond shipped regression tests."""
import pytest
from review_e22_support import opened,ask,apply,saved
from test_review_e22 import bounds,persist,links
from core.tools.markdown_render import parse

TABLE='| A | B |\n| --- | --- |\n| X | Y |'
@pytest.mark.parametrize('inner',[
 TABLE,'<div>Raw evidence.</div>','<!-- Keep this evidence -->',
 '> Evidence.\n>\n> '+TABLE.replace('\n','\n> '),
 '- Child\n\n  '+TABLE.replace('\n','\n  '),
])
@pytest.mark.parametrize('mode',['load','reply'])
def test_raw_list_load_apply_edit_save_reload(studio,output_dir,inner,mode):
    raw='- Intro.\n\n  '+inner.replace('\n','\n  ')+'\n\n  Last.\n'
    src=raw if mode=='load' else 'First.\n\nSecond.\n'
    with opened(studio,output_dir,src,raw) as (page,art):
        if mode=='reply':ask(page,bounds(page,'First.','Second.'));apply(page)
        assert page.locator('.ProseMirror [data-raw-block]').count()==1
        node=page.evaluate('e.getJSON().content.find(n=>n.type==="rawBlock")')
        assert node['attrs']['md'].strip()==raw.strip()
        # Use the actual raw-block Edit as Markdown action, not a direct transaction.
        page.evaluate('''()=>{let pos;e.state.doc.forEach((n,p)=>{if(n.type.name==='rawBlock')pos=p});e.commands.setNodeSelection(pos)}''')
        page.locator('.ProseMirror [data-raw-block]').dblclick()
        page.locator('#ask-text').fill(raw.replace('Intro.','Changed intro.'))
        apply(page)
        body=persist(studio,page,art)
        assert body.strip()==raw.replace('Intro.','Changed intro.').strip()
    with opened(studio,output_dir,body) as (page,art):
        assert page.locator('.ProseMirror [data-raw-block]').count()==1
        assert saved(page).strip()==body.strip()

@pytest.mark.parametrize('raw',['> Quoted evidence.',TABLE,'<div>Raw evidence.</div>'])
def test_refusal_is_atomic_and_top_level_still_works(studio,output_dir,raw):
    src='- First.\n\n  Second.\n- Tail\n'
    with opened(studio,output_dir,src,'First.\n\n'+raw+'\n\nSecond.') as (page,art):
        original=page.evaluate('e.getJSON()');original_saved=saved(page)
        page.locator('[data-view=blocks]').click()
        # Reloading the Markdown view is observable; take the baseline after it.
        original=page.evaluate('e.getJSON()')
        baseline=saved(page)
        page.locator('[data-view=blocks]').click()
        original=page.evaluate('e.getJSON()')
        ask(page,bounds(page,'First.','Second.'));apply(page)
        assert 'cannot go inside a list' in page.locator('#ask-result').text_content()
        assert page.evaluate('e.getJSON()')==original and art.text()==src
        assert len(page.evaluate('calls'))==1
        assert saved(page)==original_saved
    with opened(studio,output_dir,'First.\n\nSecond.\n',raw) as (page,art):
        ask(page,bounds(page,'First.','Second.'));apply(page)
        assert raw in persist(studio,page,art)

@pytest.mark.parametrize('finish',['apply','undo','redo'])
def test_neighbor_source_ids_and_bytes(studio,output_dir,finish):
    src='Before\n======\n\nFirst.\n\nSecond.\n\nAfter\n-----\n'
    with opened(studio,output_dir,src,'New.') as (page,art):
        page.wait_for_timeout(600)
        original=page.evaluate('e.getJSON()')
        baseline=saved(page)
        page.locator('[data-view=blocks]').click()
        original=page.evaluate('e.getJSON()')
        ask(page,bounds(page,'First.','Second.'));apply(page)
        if finish in ('undo','redo'):page.evaluate('e.commands.undo()')
        if finish=='redo':page.evaluate('e.commands.redo()')
        doc=page.evaluate('e.getJSON()')
        if finish=='undo':assert doc==original
        else:
            assert doc['content'][0]['attrs']['srcId']==original['content'][0]['attrs']['srcId']
            assert [n for n in doc['content'] if n['type']=='heading'][-1]['attrs']['srcId']==[n for n in original['content'] if n['type']=='heading'][-1]['attrs']['srcId']
            assert doc['content'][1]['attrs']['srcId'] is None
        body=persist(studio,page,art)
        if finish=='undo':assert body==baseline
        else:assert body==baseline.replace('First.\n\nSecond.','New.')

@pytest.mark.parametrize('deleted',[False,True])
def test_pending_reply_uses_one_call_and_current_target(studio,output_dir,deleted):
    src='Long introduction can be deleted.\n\nTarget.\n'
    with opened(studio,output_dir,src,'New.') as (page,art):
        r=bounds(page,'Target.','Target.')
        page.evaluate('''()=>{const original=fetch;window.fetch=async(...args)=>{const response=await original(...args);await new Promise(resolve=>window.releaseReply=resolve);return response}}''')
        page.evaluate('''r=>{e.commands.setTextSelection(r);document.querySelector('[data-mark=ai]').click();document.querySelector('#ask-input').value='Rewrite';document.querySelector('#ask-actions button').click()}''',r)
        page.wait_for_function("typeof releaseReply === 'function'")
        if deleted:page.evaluate("e.commands.setContent('<p>Unrelated.</p>')")
        else:page.evaluate('''()=>{let end;e.state.doc.forEach((n,p)=>{if(n.textContent==='Target.')end=p});e.commands.deleteRange({from:0,to:end})}''')
        before=page.evaluate('e.getJSON()')
        page.evaluate('releaseReply()')
        page.wait_for_function("document.querySelector('#ask-actions').textContent.includes('Apply')")
        apply(page)
        assert len(page.evaluate('calls'))==1
        if deleted:
            assert 'not in the article' in page.locator('#ask-result').text_content()
            assert page.evaluate('e.getJSON()')==before
        else:assert persist(studio,page,art).strip()=='New.'

@pytest.mark.parametrize('src,reply,kind,start',[
 ('- Parent\n  - First\n  - Second\n- Tail\n','One paragraph.','paragraph_open',None),
 ('7. Head\n8. First\n9. Second\n10. Tail\n','8. One\n9. Two','ordered_list_open',7),
])
def test_nested_and_partial_lists_keep_parent_neighbors(studio,output_dir,src,reply,kind,start):
    with opened(studio,output_dir,src,reply) as (page,art):
        ask(page,bounds(page,'First','Second'));apply(page)
        body=persist(studio,page,art)
        assert ('Parent' if 'Parent' in src else 'Head') in body and 'Tail' in body
        if start:
            lists=[t for t in parse(body) if t.type==kind]
            assert len(lists)==1 and int(lists[0].attrGet('start') or 1)==start
        else:assert '  One paragraph.' in body


def test_partial_quote_keeps_unselected_third_paragraph(studio,output_dir):
    src='- Parent\n\n  > First.\n  >\n  > Second.\n  >\n  > Third.\n\n- Tail\n'
    with opened(studio,output_dir,src,'One.\n\nTwo.') as (page,art):
        ask(page,bounds(page,'First.','Second.'));apply(page)
        body=persist(studio,page,art)
        assert 'Third.' in body and len([t for t in parse(body) if t.type=='blockquote_open'])==1
