import json
import pytest
from review_dd8_support import opened, ask, apply, saved
from test_review_dd8 import persist
from test_review_4ec_explore import bounds

@pytest.mark.parametrize('scenario',['undo','new_def','nested_raw','nested_quote','pending','boundary','subset_items','nested_whole','same_listitem'])
def test_more(studio,output_dir,scenario):
    cases={
     'undo':('Before\n======\n\nFirst.\n\nSecond.\n\nAfter\n-----\n','New.'),
     'new_def':('Read [source][ref].\n\n[ref]: /old\n','Read [source][ref].\n\n[ref]: /new'),
     'nested_raw':('- First.\n\n  Second.\n- Tail\n','First.\n\n| A | B |\n| --- | --- |\n| X | Y |\n\nSecond.'),
     'nested_quote':('- First.\n\n  Second.\n- Tail\n','First.\n\n> Quoted.\n\nSecond.'),
     'pending':('Long introduction can be deleted.\n\nTarget.\n','New.'),
     'boundary':('Before\n======\n\nFirst.\n\nSecond.\n\nAfter\n-----\n','New.'),
     'subset_items':('- Head\n- First\n- Second\n- Tail\n','- One\n- Two'),
     'nested_whole':('- First\n  - Child\n- Second\n  - Child2\n- Tail\n','- One\n  - Uno\n- Two\n  - Dos'),
     'same_listitem':('- First.\n\n  Second.\n- Tail\n','First.\n\nSecond.'),
    }
    src,reply=cases[scenario]
    with opened(studio,output_dir,src,reply) as (page,art):
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        if scenario=='pending':
            r=bounds(page,'Target.','Target.')
            page.evaluate('''()=>{const original=fetch; window.fetch=async(...args)=>{const response=await original(...args);await new Promise(resolve=>window.releaseReply=resolve);return response}}''')
            page.evaluate('''r=>{e.commands.setTextSelection(r);document.querySelector('[data-mark=ai]').click();document.querySelector('#ask-input').value='Rewrite';document.querySelector('#ask-actions button').click()}''',r)
            page.wait_for_function("typeof window.releaseReply === 'function'")
            page.evaluate('''()=>{let end;e.state.doc.forEach((n,p)=>{if(n.textContent==='Target.')end=p});e.commands.deleteRange({from:0,to:end});releaseReply()}''')
            page.wait_for_function("document.querySelector('#ask-actions').textContent.includes('Apply') || document.querySelector('#ask-result .is-error')")
        else:
            if scenario=='new_def': r={'from':1,'to':page.evaluate('e.state.doc.content.size-1')}
            elif scenario=='nested_whole':r=bounds(page,'First','Child2')
            elif scenario=='subset_items':r=bounds(page,'First','Second')
            else:r=bounds(page,'First.','Second.')
            if scenario=='boundary':r={'from':r['from']-1,'to':r['to']+1}
            ask(page,r)
        result=page.locator('#ask-result').text_content()
        if page.locator('#ask-actions button').filter(has_text='Apply').count():apply(page)
        doc=page.evaluate('e.getJSON()')
        extra={}
        if scenario=='undo':
            page.evaluate('e.commands.undo()')
            extra['undo_doc']=page.evaluate('e.getJSON()')
            extra['undo_body']=saved(page)
            page.locator('[data-view=blocks]').click()
            page.evaluate('e.commands.redo()')
            extra['redo_doc']=page.evaluate('e.getJSON()')
        body=persist(studio,page,art)
        print(json.dumps(dict(scenario=scenario,result=result,doc=doc,body=body,errors=errors,**extra),ensure_ascii=False))
