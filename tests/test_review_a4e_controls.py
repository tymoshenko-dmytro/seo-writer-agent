"""Full UI controls for both touched functions. No network/model calls."""
import json
import pytest
from review_e22_support import opened,ask,apply,saved
from test_review_e22 import bounds,persist
from core.tools.markdown_render import parse

DEFS=[
    r'[ref]: /a\\*b',r'[ref]: /a\<b',r'[ref]: /a\>b',r'[ref]: /a\\(b',
    r'[ref]: </a\\>',r'[ref]: <>',r'[ref]: </a b>',r'[ref]: /a\\\>b',
    r'[ref]: /new "a\\*b"',r'[ref]: /new "a\\\\b"',r'[ref]: /new "a\"b"',
    r'[ref]: /new "C:\\"',r'[ref]: /new "a\\\"b"',r'[ref]: /new "a\\!b"',
    '[ref]: /new "a\nb"',r'[ref]: /new "a &quot;b&quot;"',
]
@pytest.mark.parametrize('definition',DEFS)
@pytest.mark.parametrize('reference',['[source][ref]','![Diagram][ref]'])
def test_definition_values_match_single_parse_at_apply(studio,output_dir,definition,reference):
    reply='Definition.\n\n'+definition+'\n\nRead '+reference+'.'
    with opened(studio,output_dir,'Old.\n',reply) as (page,art):
        expected=page.evaluate('(md)=>TT.marked.lexer(md).links.ref',reply)
        assert expected is not None
        ask(page,bounds(page,'Old.','Old.'));apply(page)
        element=page.locator('.ProseMirror img' if reference.startswith('!') else '.ProseMirror a')
        assert element.count()==1
        assert element.get_attribute('src' if reference.startswith('!') else 'href')==expected['href']
        assert element.get_attribute('title')==expected['title']
        # Apply is the contract here; the separate pre-existing writer bug on Save is excluded.
        print(json.dumps(dict(definition=definition,reference=reference,expected=expected)))

@pytest.mark.parametrize('label',[r'a\]b',r'a\[b',r'a\\b','Mixed CASE','two   spaces',r'a\*b'])
def test_definition_labels(studio,output_dir,label):
    reply='- Definition note.\n\n  ['+label+']: /new "a\\\\*b"\n\nRead [source]['+label+'].'
    with opened(studio,output_dir,'Old.\n',reply) as (page,art):
        ask(page,bounds(page,'Old.','Old.'));apply(page)
        assert page.locator('.ProseMirror a').get_attribute('href')=='/new'
        assert page.locator('.ProseMirror a').get_attribute('title')=='a\\*b'

@pytest.mark.parametrize('middle',['ordinary','~~~','~~~info','```not-a-close','    ```','``'])
@pytest.mark.parametrize('nested',[False,True])
def test_code_and_following_ordered_list(studio,output_dir,middle,nested):
    code='```text\nalpha\n'+middle+'\n3. literal\nomega\n```'
    if nested:
        body='- Parent\n\n'+'\n'.join('  '+l for l in code.splitlines())+'\n\n  Next.\n\n  3. One\n  4. Two\n- Tail'
    else:
        body=code+'\n\n3. One\n4. Two'
    reply='Intro.\n\n'+body+'\n\nEnd.'
    expected=[t.content for t in parse(reply) if t.type=='fence']
    with opened(studio,output_dir,'Old.\n\nText.\n',reply) as (page,art):
        ask(page,bounds(page,'Old.','Text.'));apply(page)
        saved_body=persist(studio,page,art)
        assert [t.content for t in parse(saved_body) if t.type=='fence']==expected
        orders=[t for t in parse(saved_body) if t.type=='ordered_list_open']
        assert len(orders)==1 and orders[0].attrGet('start')==3
    with opened(studio,output_dir,saved_body) as (page,art):
        assert page.locator('.ProseMirror pre code').text_content()+'\n'==expected[0]
        assert page.locator('.ProseMirror ol[start="3"] > li').count()==2

@pytest.mark.parametrize('middle',['\t```','```\u00a0','```\u2003','\u00a0```'])
def test_unchanged_whitespace_code_bytes(studio,output_dir,middle):
    src='Intro.\n\n```text\nalpha\n'+middle+'\n3. literal\nomega\n```\n\nEnd.\n'
    with opened(studio,output_dir,src) as (page,art):
        body=persist(studio,page,art)
        assert body==src
