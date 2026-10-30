import json
import pytest
from review_dd8_support import opened,ask,apply,saved
from test_review_4ec_explore import bounds

@pytest.mark.parametrize('scope',['inline','blocks','list'])
def test_history(studio,output_dir,scope):
    src={'inline':'Before\n======\n\nFirst.\n\nAfter\n-----\n','blocks':'Before\n======\n\nFirst.\n\nSecond.\n\nAfter\n-----\n','list':'Before\n======\n\n- First\n- Second\n\nAfter\n-----\n'}[scope]
    with opened(studio,output_dir,src,'New.') as (page,art):
        page.wait_for_timeout(600)
        r=bounds(page,'First','First' if scope=='inline' else 'Second')
        if scope=='blocks':r['to']+=1
        ask(page,r);apply(page)
        new=page.evaluate('e.getJSON()')
        page.evaluate('e.commands.undo()')
        old=page.evaluate('e.getJSON()')
        page.evaluate('e.commands.redo()')
        redo=page.evaluate('e.getJSON()')
        print(json.dumps(dict(scope=scope,new=new,old=old,redo=redo)))
        assert new==redo
        assert 'First' in json.dumps(old)
