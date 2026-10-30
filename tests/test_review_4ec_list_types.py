import json
import pytest
from review_dd8_support import opened,ask,apply
from test_review_dd8 import persist,text_bounds
@pytest.mark.parametrize('src,reply',[('- First\n- Second\n','3. One\n4. Two'),('7. First\n8. Second\n','- One\n- Two'),('- Head\n- First\n- Second\n- Tail\n','3. One\n4. Two')])
def test_list_types(studio,output_dir,src,reply):
 from test_review_4ec_explore import bounds
 with opened(studio,output_dir,src,reply) as (page,art):
  ask(page,bounds(page,'First','Second'),'Change list type as requested');apply(page)
  print(json.dumps(dict(src=src,reply=reply,doc=page.evaluate('e.getJSON()'),body=persist(studio,page,art))))
