import json
import pytest
from review_4ec_support import opened, ask, apply
from test_review_4ec import bounds, persist

CASES=[
 ('cross_list_out','- Prefix First.\n- Second.\n\nTail.\n','First.','Tail.','> Evidence.'),
 ('cross_list_out_full','- First.\n- Second.\n\nTail.\n','First.','Tail.','> Evidence.'),
 ('cross_list_out_partial','- Prefix First.\n- Second.\n\nTail. suffix\n','First.','Tail.','First.\n\n> Evidence.\n\nLast.'),
 ('cross_list_into','Start.\n\n- First.\n- Second. suffix\n','Start.','Second.','> Evidence.'),
 ('cross_lists','- Prefix First.\n- Second.\n\nDivider\n\n1. Third.\n2. Tail. suffix\n','First.','Tail.','> Evidence.'),
 ('nested_list_to_para','- Parent\n  - First\n  - Second\n- Tail\n','First','Second','New paragraph.'),
 ('nested_list_type','- Parent\n  - First\n  - Second\n- Tail\n','First','Second','3. One\n4. Two'),
 ('item_heading','- First.\n\n  Second.\n- Tail\n','First.','Second.','## Heading'),
 ('middle_change_type','- Head\n- First\n- Second\n- Tail\n','First','Second','3. One\n4. Two'),
 ('middle_raw','- Head\n- First\n- Second\n- Tail\n','First','Second','> Evidence.'),
 ('cross_list_raw_nested','- Prefix First.\n- Second.\n\nTail.\n','First.','Tail.','- Intro\n\n  | A | B |\n  | --- | --- |\n  | X | Y |'),
 ('nested_def','Keep words.\n','Keep words.','Keep words.','- Read [source][ref].\n\n  [ref]: /new'),
]
@pytest.mark.parametrize('name,src,first,last,reply',CASES)
def test_probe(studio,output_dir,name,src,first,last,reply):
 with opened(studio,output_dir,src,reply) as (page,art):
  r=bounds(page,first,last)
  original=page.evaluate('e.getJSON()')
  ask(page,r);apply(page)
  doc=page.evaluate('e.getJSON()');result=page.locator('#ask-result').text_content()
  body=persist(studio,page,art)
  print(json.dumps(dict(name=name,bounds=r,original=original,doc=doc,result=result,body=body)))
