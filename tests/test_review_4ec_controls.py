"""Independent positive controls covering range placement, raw tokens and source identity."""
import json
import pytest
from review_4ec_support import opened,ask,apply
from test_review_4ec import bounds,persist,links
from core.tools.markdown_render import parse

@pytest.mark.parametrize('src,first,last,reply,lists,items,start',[
 ('- Head\n- First\n- Second\n- Tail\n','First','Second','- One\n- Two',1,4,None),
 ('- Parent\n  - First\n  - Second\n- Tail\n','First','Second','- One\n- Two',2,4,None),
 ('- First\n  - Child\n- Second\n  - Child2\n- Tail\n','First','Child2','- One\n  - Uno\n- Two\n  - Dos',3,5,None),
 ('- First para.\n\n  Second para.\n- Tail\n','First','Second para.','One.\n\nTwo.',1,2,None),
 ('- First para.\n\n  Second para.\n- Other first.\n\n  Other second.\n- Tail\n','First','Other second.','- One\n\n  Uno\n- Two\n\n  Dos',1,3,None),
 ('7. Head\n8. First\n9. Second\n10. Tail\n','First','Second','8. One\n9. Two',1,4,7),
 ('7. First\n8. Second\n','First','Second','7. One\n8. Two',1,2,7),
])
def test_lists(studio,output_dir,src,first,last,reply,lists,items,start):
 with opened(studio,output_dir,src,reply) as (page,art):
  ask(page,bounds(page,first,last));apply(page)
  body=persist(studio,page,art); tokens=parse(body)
  found=[t for t in tokens if t.type in ('bullet_list_open','ordered_list_open')]
  assert len(found)==lists and len([t for t in tokens if t.type=='list_item_open'])==items,body
  assert 'One' in body and 'Two' in body
  if 'Tail' in src:assert 'Tail' in body
  if start:assert int(found[0].attrGet('start') or 1)==start

@pytest.mark.parametrize('src,first,last,reply,check',[
 ('Prefix old suffix.\n','old','old','**new**','Prefix **new** suffix.'),
 ('## Prefix old suffix.\n','old','old','**new**','## Prefix **new** suffix.'),
 ('- Prefix old suffix.\n','old','old','**new**','- Prefix **new** suffix.'),
 ('Prefix First.\n\nSecond suffix.\n','First.','Second','## New','## New'),
 ('- Parent\n  - Prefix First\n  - Second suffix\n- Tail\n','First','Second','- One\n- Two','suffix'),
])
def test_inline_and_partial_cross_block(studio,output_dir,src,first,last,reply,check):
 with opened(studio,output_dir,src,reply) as (page,art):
  ask(page,bounds(page,first,last));apply(page)
  body=persist(studio,page,art)
  assert 'Prefix' in body and 'suffix' in body and check in body,body

@pytest.mark.parametrize('raw,kind,needle',[
 ('| A | B |\n| --- | --- |\n| X | Y |','table_open','| X | Y |'),
 ('> Quoted.','blockquote_open','> Quoted.'),
 ('<div>Evidence.</div>','literal_html','Evidence.'),
])
def test_top_level_raw_preview_save_reload(studio,output_dir,raw,kind,needle):
 src='First.\n\nSecond.\n'
 with opened(studio,output_dir,src,raw) as (page,art):
  ask(page,bounds(page,'First.','Second.'))
  assert needle in page.locator('#ask-result ins').text_content()
  apply(page)
  assert page.locator('.ProseMirror [data-raw-block]').count()==1
  body=persist(studio,page,art)
  assert needle in body and (kind=='literal_html' or any(t.type==kind for t in parse(body))),body
  # A real reload uses the same shared blockNodes path; raw bytes survive.
 with opened(studio,output_dir,body) as (page,art):
  assert needle in page.locator('.ProseMirror [data-raw-block]').text_content()
  assert persist(studio,page,art).strip()==body.strip()

@pytest.mark.parametrize('reply', ['Read [source][ref].','Read [ref].','Read [ref][].','Read [source](/report).'])
def test_reply_uses_article_definitions(studio,output_dir,reply):
 with opened(studio,output_dir,'Keep words.\n\n[ref]: /report\n',reply) as (page,art):
  ask(page,bounds(page,'Keep words.','Keep words.'));apply(page)
  body=persist(studio,page,art)
  assert links(body)==['/report'],body

@pytest.mark.parametrize('scope,finish',[('inline','undo'),('inline','redo'),('blocks','undo'),('blocks','redo'),('list','undo'),('list','redo')])
def test_undo_redo_restores_source_ids(studio,output_dir,scope,finish):
 src={'inline':'Left.\n\nKeep words.\n\nRight.\n','blocks':'Left.\n\nFirst.\n\nSecond.\n\nRight.\n','list':'Left.\n\n- First\n- Second\n\nRight.\n'}[scope]
 reply='New.' if scope!='list' else '- One\n- Two'
 with opened(studio,output_dir,src,reply) as (page,art):
  page.wait_for_timeout(600)
  original=page.evaluate('e.getJSON()')
  r=bounds(page,'Keep words.','Keep words.') if scope=='inline' else bounds(page,'First','Second')
  if scope=='blocks':r['to']+=1
  ask(page,r);apply(page)
  changed=page.evaluate('e.getJSON()')
  page.evaluate('e.commands.undo()')
  assert page.evaluate('e.getJSON()')==original
  if finish=='redo':
   page.evaluate('e.commands.redo()');assert page.evaluate('e.getJSON()')==changed
  body=persist(studio,page,art)
  if finish=='undo':assert body==src,body
  else:assert ('New.' if scope!='list' else 'One') in body


def test_fallback_reparses_current_textblock(studio,output_dir):
 with opened(studio,output_dir,'Read words here.\n','**Changed.**') as (page,art):
  ask(page,bounds(page,'Read words here.','Read words here.'))
  page.evaluate("e.commands.insertContentAt(1,'Prefix ')")
  apply(page)
  body=persist(studio,page,art)
  assert body.strip()=='Prefix **Changed.**',body
