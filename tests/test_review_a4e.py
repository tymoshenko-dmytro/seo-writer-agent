"""Remaining fence cases: real AI API -> Apply -> Save, no services."""
import json
import pytest
from review_e22_support import opened, ask, apply
from test_review_e22 import bounds, persist
from core.tools.markdown_render import parse

@pytest.mark.parametrize('middle',['\t```','```\u00a0','```\u2003','\u00a0```'])
def test_ra4e_01_code_whitespace_is_not_a_closing_fence(studio,output_dir,middle):
    reply='Intro.\n\n```text\nalpha\n'+middle+'\n3. literal\nomega\n```\n\nEnd.'
    expected=[t.content for t in parse(reply) if t.type=='fence']
    assert expected==['alpha\n'+middle+'\n3. literal\nomega\n']
    with opened(studio,output_dir,'Old.\n\nText.\n',reply) as (page,art):
        ask(page,bounds(page,'Old.','Text.'));apply(page)
        code=page.locator('.ProseMirror pre code').text_content()
        assert code+'\n'==expected[0]
        writer=page.evaluate('e.markdown.serialize(e.getJSON())')
        assert [t.content for t in parse(writer) if t.type=='fence']==expected
        body=persist(studio,page,art)
        actual=[t.content for t in parse(body) if t.type=='fence']
        print(json.dumps(dict(middle=middle,reply=reply,code=code,writer=writer,saved=body,expected=expected,actual=actual)))
        assert actual==expected,body
    with opened(studio,output_dir,body) as (page,art):
        assert page.locator('.ProseMirror pre code').text_content()+'\n'==expected[0]
