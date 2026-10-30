import json
import pytest
from review_e22_support import opened,ask,apply
from test_review_e22 import persist,links

@pytest.mark.parametrize('definition',[
 '- Definition note.\n\n  [ref]: /new',
 '> Definition note.\n>\n> [ref]: /new',
])
def test_nested_definition(studio,output_dir,definition):
 src='Read [source][ref].\n\n[ref]: /old\n'
 reply=definition+'\n\nRead [source][ref].'
 assert links(reply)==['/new']
 with opened(studio,output_dir,src,reply) as (page,art):
  ask(page,{'from':0,'to':page.evaluate('e.state.doc.content.size')},'Change the destination to /new, preserving a working link.')
  apply(page)
  body=persist(studio,page,art)
  print(json.dumps(dict(reply=reply,body=body,links=links(body))))
  assert links(body)==['/new'],body
