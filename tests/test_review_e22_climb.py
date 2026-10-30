import json
import pytest
from review_e22_support import opened,ask,apply
from test_review_e22 import bounds,persist

@pytest.mark.parametrize('src,first,last,reply',[
 ('- Parent\n\n  > - First\n  > - Second\n\n- Tail\n','First','Second','- One\n- Two'),
 ('- Parent\n\n  > First.\n  >\n  > Second.\n\n- Tail\n','First.','Second.','One.\n\nTwo.'),
 ('- Parent\n\n  > First.\n  >\n  > Second.\n  >\n  > Third.\n\n- Tail\n','First.','Second.','One.\n\nTwo.'),
 ('- Parent\n  7. First\n  8. Second\n- Tail\n','First','Second','3. One\n4. Two'),
])
def test_climb(studio,output_dir,src,first,last,reply):
 with opened(studio,output_dir,src,reply) as (page,art):
  original=page.evaluate('e.getJSON()')
  ask(page,bounds(page,first,last));apply(page)
  body=persist(studio,page,art)
  print(json.dumps(dict(src=src,original=original,body=body)))
