"""Acceptance regressions through real editor, Flask API, model stand-in, Apply and Save."""
import json
import pytest
from review_e22_support import opened, ask, apply, saved
from core.tools.markdown_render import parse


def bounds(page, first, last):
    return page.evaluate('''([first,last])=>{let a,b;e.state.doc.descendants((n,p)=>{if(n.isText){if(a===undefined&&n.text.includes(first))a=p+n.text.indexOf(first);if(n.text.includes(last))b=p+n.text.indexOf(last)+last.length}});return {from:a,to:b}}''',[first,last])


def persist(studio,page,art):
    revision=page.locator('input[name=revision]').input_value()
    body=saved(page)
    response=studio.post('/a/'+art.slug+'/edit',data={'body':body,'revision':revision})
    assert response.status_code==302
    assert art.text()==body
    return body


def links(body):
    return [c.attrGet('href') for t in parse(body) for c in (t.children or []) if c.type=='link_open']


@pytest.mark.parametrize('raw,needle,token',[
    ('> Quoted evidence.','Quoted evidence.','blockquote_open'),
    ('| A | B |\n| --- | --- |\n| X | Y |','| X | Y |','table_open'),
    ('<div>Raw evidence.</div>','Raw evidence.',None),
    ('- Intro.\n\n  | A | B |\n  | --- | --- |\n  | X | Y |','| X | Y |','table_open'),
])
def test_re22_01_raw_cannot_silently_land_inside_list(studio,output_dir,raw,needle,token):
    src='- Prefix First.\n- Second.\n\nTail.\n'
    with opened(studio,output_dir,src,raw) as (page,art):
        original=page.evaluate('e.getJSON()')
        ask(page,bounds(page,'First.','Tail.'));apply(page)
        result=page.locator('#ask-result').text_content()
        if 'cannot go inside a list' in result:
            assert page.evaluate('e.getJSON()')==original and art.text()==src
            assert len(page.evaluate('calls'))==1
            return
        doc=page.evaluate('e.getJSON()')
        assert needle in page.locator('.ProseMirror').text_content()
        body=persist(studio,page,art)
        print(json.dumps(dict(doc=doc,saved=body)))
        assert 'Prefix' in body and needle in body,body
        if token:assert any(t.type==token for t in parse(body)),body
    with opened(studio,output_dir,body) as (page,art):
        assert needle in saved(page)


@pytest.mark.parametrize('quoted,first,last,reply',[
    ('  > - First\n  > - Second','First','Second','- One\n- Two'),
    ('  > First.\n  >\n  > Second.','First.','Second.','One.\n\nTwo.'),
])
def test_re22_03_whole_inner_content_keeps_outer_quote(studio,output_dir,quoted,first,last,reply):
    src='- Parent\n\n'+quoted+'\n\n- Tail\n'
    with opened(studio,output_dir,src,reply) as (page,art):
        ask(page,bounds(page,first,last),'Rewrite only the selected words; preserve the enclosing quote and list.')
        apply(page)
        body=persist(studio,page,art)
        print(json.dumps(dict(request=page.evaluate('calls')[0]['body'],saved=body)))
        assert 'Parent' in body and 'Tail' in body and 'One' in body and 'Two' in body
        assert len([t for t in parse(body) if t.type=='blockquote_open'])==1,body
    with opened(studio,output_dir,body) as (page,art):
        assert page.locator('.ProseMirror blockquote').count()==1


@pytest.mark.parametrize('definition',[
 '- Definition note.\n\n  [ref]: /new',
 '> Definition note.\n>\n> [ref]: /new',
])
def test_re22_02_nested_reply_definition_resolves_links(studio,output_dir,definition):
 src='Read [source][ref].\n\n[ref]: /old\n'
 reply=definition+'\n\nRead [source][ref].'
 assert links(reply)==['/new']
 with opened(studio,output_dir,src,reply) as (page,art):
  ask(page,{'from':0,'to':page.evaluate('e.state.doc.content.size')},'Change the destination to /new, preserving a working link.')
  apply(page)
  body=persist(studio,page,art)
  print(json.dumps(dict(reply=reply,body=body,links=links(body))))
  assert links(body)==['/new'],body


@pytest.mark.parametrize('src',[
 '- Parent\n  - First\n  - Second\n- Tail\n',
 '- Parent\n\n  7. First\n  8. Second\n\n- Tail\n',
])
def test_re22_04_nested_ordered_list_survives_markdown(studio,output_dir,src):
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
