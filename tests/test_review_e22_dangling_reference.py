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


@pytest.mark.parametrize('reference',['[source][ref]','[ref]','[ref][]','![Diagram][ref]'])
def test_re22_02_reply_can_use_definition_from_replaced_range(studio,output_dir,reference):
    src='Read [source][ref].\n\n[ref]: /report\n'
    reply='Read '+reference+'.'
    with opened(studio,output_dir,src,reply) as (page,art):
        ask(page,{'from':0,'to':page.evaluate('e.state.doc.content.size')},'Rewrite the words, preserve the reference destination.')
        apply(page)
        body=persist(studio,page,art)
        print(json.dumps(dict(reply=reply,doc=page.evaluate('e.getJSON()'),saved=body)))
        # A definition may be removed once the reply's references have been resolved to inline URLs.
        tokens=parse(body)
        if reference.startswith('!'):
            urls=[c.attrGet('src') for t in tokens for c in (t.children or []) if c.type=='image']
        else:urls=links(body)
        assert urls==['/report'],body


