import json
import pytest
from review_e22_support import opened,ask,apply
from test_review_e22 import bounds,persist
from core.tools.markdown_render import parse

@pytest.mark.parametrize('src',[
 '- Parent\n  - First\n  - Second\n- Tail\n',
 '- Parent\n\n  7. First\n  8. Second\n\n- Tail\n',
])
def test_nested_number(studio,output_dir,src):
 assert len([t for t in parse(src) if t.type in ('ordered_list_open','bullet_list_open')])==2
 with opened(studio,output_dir,src,'3. One\n4. Two') as (page,art):
  ask(page,bounds(page,'First','Second'),'Replace the selected inner list with a numbered list starting at 3.')
  apply(page)
  doc=page.evaluate('e.getJSON()')
  body=persist(studio,page,art)
  tokens=parse(body)
  lists=[t for t in tokens if t.type=='ordered_list_open']
  print(json.dumps(dict(doc=doc,body=body,ordered_starts=[t.attrGet('start') for t in lists])))
  assert len(lists)==1 and int(lists[0].attrGet('start') or 1)==3,body
