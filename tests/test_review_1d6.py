"""Confirmed Round 4 regressions; run explicitly, never part of the full suite."""
import json
import pytest
from review_1d6_support import opened,ask,apply
from test_review_4ec import bounds,persist
from core.tools.markdown_render import parse

@pytest.mark.parametrize('middle',['~~~','```not-a-close','    ```'])
def test_r1d6_01_fence_content_survives_apply_save(studio,output_dir,middle):
 reply='Intro.\n\n```text\nalpha\n'+middle+'\n3. literal\nomega\n```\n\nEnd.'
 expected=[t.content for t in parse(reply) if t.type=='fence']
 assert len(expected)==1
 with opened(studio,output_dir,'Old.\n\nText.\n',reply) as (page,art):
  ask(page,bounds(page,'Old.','Text.'));apply(page)
  code=page.evaluate('e.getJSON().content.filter(n=>n.type==="codeBlock").map(n=>n.content.map(c=>c.text).join(""))')
  assert [x+'\n' for x in code]==expected
  body=persist(studio,page,art)
  actual=[t.content for t in parse(body) if t.type=='fence']
  print(json.dumps(dict(reply=reply,code_after_apply=code,saved=body,expected=expected,actual=actual)))
  assert actual==expected,body
 with opened(studio,output_dir,body) as (page,art):
  assert page.locator('.ProseMirror pre code').text_content()==expected[0].rstrip('\n')

@pytest.mark.parametrize('reference',['[source][ref]','![Diagram][ref]'])
def test_r1d6_02_reply_reference_keeps_decoded_destination(studio,output_dir,reference):
 # Two literal backslashes in Markdown encode one backslash in the address.
 reply='Definition.\n\n[ref]: /a\\\\*b\n\nRead '+reference+'.'
 expected='/a\\*b'
 with opened(studio,output_dir,'Old.\n',reply) as (page,art):
  lexer=page.evaluate('(md)=>TT.marked.lexer(md).links.ref.href',reply)
  assert lexer==expected
  ask(page,bounds(page,'Old.','Old.'));apply(page)
  if reference.startswith('!'):
   actual=page.locator('.ProseMirror img').get_attribute('src')
  else:actual=page.locator('.ProseMirror a').get_attribute('href')
  body=persist(studio,page,art)
  print(json.dumps(dict(reply=reply,decoded_by_lexer=lexer,destination_after_apply=actual,saved=body)))
  # This checks the newly introduced second unescape on Apply. The base has a separate
  # pre-existing inline Markdown writer escaping limitation; do not claim base Save is correct.
  assert actual==expected
