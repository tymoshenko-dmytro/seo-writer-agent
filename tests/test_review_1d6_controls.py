"""Round 4 independent checks through the real UI and offline Flask bridge."""
import json
import pytest
from review_1d6_support import opened,ask,apply,saved
from test_review_4ec import bounds,persist,links
from core.tools.markdown_render import parse

RAWS=['> Quoted evidence.','| A | B |\n| --- | --- |\n| X | Y |','<div>Raw evidence.</div>',
      '- Intro.\n\n  | A | B |\n  | --- | --- |\n  | X | Y |']

@pytest.mark.parametrize('raw',RAWS)
@pytest.mark.parametrize('redo_pending',[False,True])
def test_refusal_keeps_history_and_saved_text(studio,output_dir,raw,redo_pending):
 src='- Prefix First.\n- Second.\n\nTail.\n'
 with opened(studio,output_dir,src,raw) as (page,art):
  page.wait_for_timeout(600)  # isolate the test edit from the initial load's history group
  initial=page.evaluate('e.getJSON()')
  page.evaluate("e.commands.insertContentAt(3,'Edited ')")
  edited=page.evaluate('e.getJSON()')
  if redo_pending:page.evaluate('e.commands.undo()')
  ask(page,bounds(page,'First.','Tail.'))
  before=page.evaluate('e.getJSON()')
  page.evaluate('window.beforeApply=e.state')
  apply(page)
  assert 'cannot go inside a list' in page.locator('#ask-result').text_content()
  assert page.evaluate('e.getJSON()')==before and art.text()==src
  assert page.evaluate('e.state.plugins.filter(p=>p.key.startsWith("history")).every(p=>p.getState(e.state)===p.getState(beforeApply))')
  assert len(page.evaluate('calls'))==1
  if redo_pending:
   assert page.evaluate('e.commands.redo()')
   assert page.evaluate('e.getJSON()')==edited
  else:
   assert page.evaluate('e.commands.undo()')
   assert page.evaluate('e.getJSON()')==initial
   assert page.evaluate('e.commands.redo()')
   assert page.evaluate('e.getJSON()')==edited
  body=persist(studio,page,art)
  assert 'Edited Prefix First.' in body and 'Second.' in body and 'Tail.' in body
  assert 'evidence' not in body.lower()

@pytest.mark.parametrize('src',[
 '- Parent\n  - First\n  - Second\n- Tail\n',
 '- Parent\n\n  7. First\n  8. Second\n\n- Tail\n',
 '- Parent\n\n  > Intro.\n  >\n  > - First\n  > - Second\n\n- Tail\n',
])
def test_nested_ordered_reload(studio,output_dir,src):
 with opened(studio,output_dir,src,'3. One\n4. Two') as (page,art):
  ask(page,bounds(page,'First','Second'));apply(page)
  body=persist(studio,page,art)
  lists=[t for t in parse(body) if t.type in ('ordered_list_open','bullet_list_open')]
  assert len(lists)==2 and lists[1].type=='ordered_list_open' and lists[1].attrGet('start')==3
  assert sum(t.type=='list_item_open' for t in parse(body))==4
  assert 'Parent' in body and 'Tail' in body
 with opened(studio,output_dir,body) as (page,art):
  orders=page.evaluate('()=>{let out=[];e.state.doc.descendants(n=>{if(n.type.name==="orderedList")out.push({start:n.attrs.start,count:n.childCount,text:n.textContent})});return out}')
  assert orders==[{'start':3,'count':2,'text':'OneTwo'}]
  assert saved(page).strip()==body.strip()

@pytest.mark.parametrize('definition',[
 '- Definition note.\n\n  [ref]: /new',
 '> Definition note.\n>\n> [ref]: /new',
])
def test_nested_definition_reload(studio,output_dir,definition):
 reply=definition+'\n\nRead [source][ref].'
 with opened(studio,output_dir,'Read [source][ref].\n\n[ref]: /old\n',reply) as (page,art):
  ask(page,{'from':0,'to':page.evaluate('e.state.doc.content.size')});apply(page)
  body=persist(studio,page,art)
  assert links(body)==['/new'] and 'Definition note.' in body
 with opened(studio,output_dir,body) as (page,art):
  assert page.locator('.ProseMirror a').get_attribute('href')=='/new'
  assert links(saved(page))==['/new']

@pytest.mark.parametrize('ref',['[source][ref]','[ref]','[ref][]','![Diagram][ref]'])
@pytest.mark.parametrize('own',[False,True])
def test_definition_priority(studio,output_dir,ref,own):
 src='Read [source][ref].\n\n[ref]: /inside\n\nKeep.\n\n[other]: /outside\n'
 reply='Read '+ref+'.'+('\n\n[ref]: /own' if own else '')
 with opened(studio,output_dir,src,reply) as (page,art):
  page.evaluate('''()=>{let pos;e.state.doc.forEach((n,p)=>{if(n.type.name==='rawBlock'&&n.attrs.md.includes('/outside'))pos=p});e.commands.setNodeSelection(pos)}''')
  page.locator('.ProseMirror [data-raw-block]').last.dblclick()
  page.locator('#ask-text').fill('[ref]: /outside')
  apply(page)
  end=page.evaluate('()=>{let end;e.state.doc.forEach((n,p)=>{if(n.type.name==="rawBlock"&&n.attrs.md.includes("/inside"))end=p+n.nodeSize});return end}')
  ask(page,{'from':0,'to':end});apply(page)
  body=persist(studio,page,art)
  expected='/own' if own else '/outside'
  if ref.startswith('!'):
   actual=[c.attrGet('src') for t in parse(body) for c in (t.children or []) if c.type=='image']
  else:actual=links(body)
  assert actual==[expected]
  assert 'Keep.' in body

@pytest.mark.parametrize('middle',['ordinary','~~~','```not-a-close','    ```'])
def test_unchanged_fence_keeps_code_content(studio,output_dir,middle):
 src='Intro.\n\n```text\nalpha\n'+middle+'\n3. literal\nomega\n```\n'
 with opened(studio,output_dir,src) as (page,art):
  assert [t.content for t in parse(saved(page)) if t.type=='fence']==[t.content for t in parse(src) if t.type=='fence']
