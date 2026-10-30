import json
import pytest
from review_dd8_support import opened, ask, apply, saved
from test_review_dd8 import persist


def bounds(page, first, last):
    return page.evaluate('''([first,last])=>{let a,b; e.state.doc.descendants((n,p)=>{if(n.isText){if(a===undefined&&n.text.includes(first))a=p+n.text.indexOf(first);if(n.text.includes(last))b=p+n.text.indexOf(last)+last.length}});return {from:a,to:b}}''',[first,last])

CASES=[
('neighbors','Before\n======\n\nFirst.\n\nSecond.\n\nAfter\n-----\n','First.','Second.','New.'),
('defs','Read [source][ref].\n\n[ref]: /old\n','Read','.', 'Read [source][ref].\n\n[ref]: /new'),
('nested','- Parent\n  - First\n  - Second\n- Tail\n','First','Second','- One\n- Two'),
('nested_partial','- Parent\n  - Prefix First\n  - Second suffix\n- Tail\n','First','Second','- One\n- Two'),
('multipara','- First para.\n\n  Second para.\n\n- Tail\n','First','Second para.','One.\n\nTwo.'),
('multipara_single','- First para.\n\n  Second para.\n\n- Tail\n','First','Second para.','## Heading'),
('multipara_list','- First para.\n\n  Second para.\n\n- Tail\n','First','Second para.','- One\n- Two'),
('multipara_nested','- First para.\n\n  Second para.\n\n- Other first.\n\n  Other second.\n\n- Tail\n','First','Other second.','- One\n\n  Uno\n\n- Two\n\n  Dos'),
('ordered','7. First\n8. Second\n9. Tail\n','First','Second','7. One\n8. Two'),
('ordered_change','7. First\n8. Second\n','First','Second','3. One\n4. Two'),
('ordered_subset','7. Head\n8. First\n9. Second\n10. Tail\n','First','Second','8. One\n9. Two'),
('partial','Prefix First.\n\nSecond suffix.\n','First.','Second','## New'),
# 'raw_nested' (words selected inside a list holding a table) moved to
# test_list_with_a_table_loads_as_one_raw_block: since R4EC-04 such a list is one raw block of its
# Markdown, edited as text, so its words cannot be selected and the case no longer arises
('raw_in_list_reply','- Old.\n- Next.\n','Old.','Next.','- Intro.\n\n  | A | B |\n  | --- | --- |\n  | X | Y |\n\n  Last.'),
('quote_nested','- First.\n- Second.\n','First.','Second.','- Intro.\n\n  > Quote.\n\n  Last.'),
('raw_edges','First.\n\n| A | B |\n| --- | --- |\n| X | Y |\n\nLast.\n','First.','Last.','First.\n\n| A | B |\n| --- | --- |\n| X | Y |\n\nLast.'),
]
@pytest.mark.parametrize('name,src,first,last,reply',CASES)
def test_explore(studio,output_dir,name,src,first,last,reply):
    with opened(studio,output_dir,src,reply) as (page,art):
        errors=[]; page.on('pageerror',lambda e:errors.append(str(e)))
        r=bounds(page,first,last)
        original=page.evaluate('e.getJSON()')
        ask(page,r)
        preview=page.locator('#ask-result').text_content()
        apply(page)
        current=page.evaluate('e.getJSON()')
        body=persist(studio,page,art)
        print(json.dumps(dict(name=name,bounds=r,original=original,current=current,body=body,errors=errors,preview=preview),ensure_ascii=False))


def test_list_with_a_table_loads_as_one_raw_block(studio,output_dir):
    src='- Intro.\n\n  | A | B |\n  | --- | --- |\n  | X | Y |\n\n  Last.\n'
    with opened(studio,output_dir,src,'unused') as (page,art):
        doc=page.evaluate('e.getJSON()')
        raw=[n for n in doc['content'] if n['type']=='rawBlock']
        assert raw and '| X | Y |' in raw[0]['attrs']['md'] and raw[0]['attrs']['md'].startswith('- Intro.')
        assert saved(page).split()==src.split()     # the same words and table (blank lines as load leaves them)
