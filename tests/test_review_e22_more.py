import json
import pytest
from review_4ec_support import opened, ask, apply
from test_review_4ec import bounds, persist

CASES=[
 ('reference_removed','Read [source][ref].\n\n[ref]: /report\n',None,None,'Read [source][ref].'),
 ('reference_nested_definition','Keep words.\n','Keep words.','Keep words.','- Reference\n\n  [ref]: /new\n\nRead [source][ref].'),
 ('reference_nested_shadow','Keep words.\n\n[ref]: /old\n','Keep words.','Keep words.','- Read [source][ref].\n\n  [ref]: /new'),
 ('code_context','```python\nFirst.\nSecond.\n```\n','First.','Second.','First.\n\n> Evidence.\n\nSecond.'),
 ('cross_item_out','- Intro.\n\n  Prefix First.\n\n  Second.\n\nTail.\n','First.','Tail.','> Evidence.'),
 ('cross_quote_out','- Intro.\n\n  > Prefix First.\n  > Second.\n\nTail.\n','First.','Tail.','> Evidence.'),
 ('last_heading','- Parent\n\n  First.\n\n  ## Second\n- Tail\n','First.','Second','New.'),
 ('single_multiitem_raw','- First.\n\n  Second.\n','First.','Second.','> Evidence.'),
 ('single_nested_retype','- Parent\n  - First\n  - Second\n','First','Second','New paragraph.'),
 ('middle_ordered_change_type','7. Head\n8. First\n9. Second\n10. Tail\n','First','Second','- One\n- Two'),
]
@pytest.mark.parametrize('name,src,first,last,reply',CASES)
def test_probe(studio,output_dir,name,src,first,last,reply):
 with opened(studio,output_dir,src,reply) as (page,art):
  r=bounds(page,first,last) if first else {'from':0,'to':page.evaluate('e.state.doc.content.size')}
  ask(page,r);apply(page)
  doc=page.evaluate('e.getJSON()');result=page.locator('#ask-result').text_content()
  body=persist(studio,page,art)
  print(json.dumps(dict(name=name,bounds=r,doc=doc,result=result,body=body)))
