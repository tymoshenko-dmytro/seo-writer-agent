"""Offline real editor probes. No product code is modified."""
import json
import re
from pathlib import Path
from contextlib import contextmanager
import pytest
from core import store
from playwright.sync_api import sync_playwright
ROOT = Path(__file__).resolve().parents[1]

@contextmanager
def opened(studio, output_dir, src, reply='Rewritten.'):
    art = store.Article(output_dir, 'reviewer-editor')
    art.dir.mkdir(exist_ok=True)
    art.write_article('', src)
    html = studio.get('/a/reviewer-editor/edit').get_data(as_text=True)
    html = re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.S)
    html = re.sub(r'<link\b[^>]*>', '', html)
    from core.agents.interactive import AIEditor
    from ui.views import visuals
    with pytest.MonkeyPatch.context() as patch, sync_playwright() as p:
        patch.setattr(AIEditor, 'call_claude', lambda self, *a, **kw: reply)
        patch.setattr(visuals, 'queue_json', lambda label, kind, fn, **kw: fn(lambda msg: None))
        browser = p.chromium.launch(args=['--no-sandbox','--single-process','--no-zygote'], chromium_sandbox=False)
        try:
            page = browser.new_page()
            page.set_default_timeout(3000)
            page.route('**/*', lambda route: route.abort())
            page.set_content(html)
            page.add_script_tag(content=(ROOT/'ui/static/vendor/tiptap/tiptap.js').read_text())
            page.add_script_tag(content=(ROOT/'ui/static/js/mdblocks.js').read_text())
            page.evaluate('''() => {const Original = TT.Editor; TT.Editor = class extends Original {constructor(opts) {super(opts); window.e = this;}};}''')
            def request(url, body):
                response = studio.post(url, json=body)
                return {'status': response.status_code, 'data': response.json}
            page.expose_function('_request', request)
            page.evaluate('''() => {window.calls=[]; window.fetch=async (url,opts) => {const body=JSON.parse(opts.body);calls.push({url,body});const r=await _request(url,body);return {ok:r.status<400,status:r.status,json:async()=>r.data};};}''')
            page.add_script_tag(content=(ROOT/'ui/static/js/block_editor.js').read_text())
            yield page, art
        finally:
            browser.close()

def ask(page, bounds=None, instruction='Rewrite, preserving the structure'):
    page.evaluate('''({bounds,instruction}) => {e.commands.setTextSelection(bounds || {from:1,to:e.state.doc.content.size-1}); document.querySelector('[data-mark=ai]').click(); document.querySelector('#ask-input').value=instruction; document.querySelector('#ask-actions button').click();}''', {'bounds':bounds,'instruction':instruction})
    page.wait_for_function("document.querySelector('#ask-actions').textContent.includes('Apply') || document.querySelector('#ask-result .is-error')")

def apply(page):
    page.locator('#ask-actions button').filter(has_text=re.compile('^Apply$')).click()

def saved(page):
    page.locator('[data-view=markdown]').click()
    return page.locator('#editor-md').input_value()

@pytest.mark.parametrize('src,reply', [
    ('First paragraph.\n\nSecond paragraph.\n','Rewritten paragraph.'),
    ('First.\n\n| Name | Value |\n| --- | --- |\n| A | B |\n\nLast.\n','First.\n\n| Name | Value |\n| --- | --- |\n| A | B |\n\nLast.'),
    ('First.\n\n> Quoted text.\n\nLast.\n','First.\n\n> Quoted text.\n\nLast.'),
    ('## Heading\n','## Rewritten'),
    ('- First\n- Second\n','- One\n- Two'),
    ('Read [source][ref].\n\n[ref]: https://example.com/report\n','Read [source][ref].\n\n[ref]: https://example.com/report'),
    ('Before ![Pic](assets/chart_1.png) after.\n','Before ![Pic](assets/chart_1.png) after.'),
    ('Keep words.\n',''),
    ('Keep words.\n','```markdown\n**New** [source](https://example.com)\n```'),
    ('Keep words.\n','- Only item'),
    ('Keep words.\n','> Only quote'),
])
def test_explore(studio, output_dir, src, reply):
    with opened(studio, output_dir, src, reply) as (page, art):
        ask(page)
        before = page.evaluate('({calls, doc:e.getJSON(), result:document.querySelector("#ask-result").textContent})')
        if page.locator('#ask-actions button').filter(has_text=re.compile('^Apply$')).count():
            apply(page)
        print(json.dumps({'src':src, 'reply':reply,'before':before,'html':page.locator('.ProseMirror').inner_html(),'saved':saved(page)},ensure_ascii=False))

@pytest.mark.parametrize('src,reply,mode', [
    ('- First\n- Second\n','- One\n- Two','text_bounds'),
    ('## First\n\nSecond.\n','## New\n\nOther.','text_bounds'),
    ('Prefix old suffix.\n','One.\n\nTwo.','partial'),
    ('## Prefix old suffix.\n','One.\n\nTwo.','partial'),
    ('- Prefix old suffix.\n','One.\n\nTwo.','partial'),
    ('Prefix old suffix.\n','## New heading','partial'),
    ('Read [source][ref].\n\n[ref]: https://example.com/report\n','Read [source](https://example.com/report).','text_bounds'),
    ('Keep words.\n',r'Use \[literal\](https://example.com).','text_bounds'),
    ('Read **bold** and [source](https://example.com).\n','Changed.','shift'),
    ('Keep words.\n\nTail.\n','Changed.','shorten'),
])
def test_explore_more(studio, output_dir, src, reply, mode):
    with opened(studio, output_dir, src, reply) as (page, art):
        bounds = page.evaluate('''mode => {let ts=[];e.state.doc.descendants((n,p)=>{if(n.isText)ts.push({from:p,to:p+n.nodeSize,text:n.text})}); if(mode==='partial'){let t=ts.find(t=>t.text.includes('old')); return {from:t.from+t.text.indexOf('old'),to:t.from+t.text.indexOf('old')+3}}return {from:ts[0].from,to:ts.at(-1).to}}''',mode)
        ask(page,bounds)
        if mode == 'shift': page.evaluate("e.commands.insertContentAt(1, 'Prefix ')")
        if mode == 'shorten': page.evaluate("e.commands.setContent('<p>X</p>')")
        errors=[]
        page.on('pageerror', lambda err: errors.append(str(err)))
        apply(page)
        print(json.dumps({'mode':mode,'src':src,'reply':reply,'bounds':bounds,'calls':page.evaluate('calls'),'errors':errors,'result':page.locator('#ask-result').text_content(),'html':page.locator('.ProseMirror').inner_html(),'saved':saved(page)},ensure_ascii=False))

@pytest.mark.parametrize('src,reply,bounds',[
    ('Keep words.\n\n[ref]: /target\n',r'Use \[label\]\[ref\].',{'from':1,'to':12}),
    ('Read [source][ref].\n\n[ref]: /report\n','Read [source](/report).',None),
    ('- Only item\n','- Rewritten item',{'from':3,'to':12}),
    ('Intro.\n\n## Header\n\nTail.\n','## New header',{'from':1,'to':15}),
    ('Keep words.\n','| A | B |\n| --- | --- |\n| X | Y |',{'from':1,'to':12}),
])
def test_explore_edges(studio, output_dir, src, reply, bounds):
    with opened(studio, output_dir, src, reply) as (page, art):
        ask(page,bounds)
        if page.locator('#ask-actions button').filter(has_text=re.compile('^Apply$')).count(): apply(page)
        print(json.dumps({'src':src,'reply':reply,'calls':page.evaluate('calls'),'result':page.locator('#ask-result').text_content(),'html':page.locator('.ProseMirror').inner_html(),'saved':saved(page)},ensure_ascii=False))

@pytest.mark.parametrize('settled',[False,True])
def test_explore_dirty_redraw(studio, output_dir, settled):
    with opened(studio, output_dir, 'Text.\n\n![Picture](assets/chart_1.png)\n*Caption.*\n') as (page, art):
        page.evaluate('''() => {window.fetch=async (url,opts)=>{calls.push({url,body:JSON.parse(opts.body)});return new Promise(()=>{});}; e.commands.insertContentAt(1,'Unsaved ');}''')
        if settled: page.wait_for_timeout(300)
        page.evaluate('''() => {let pos; e.state.doc.descendants((n,p)=>{if(n.type.name==='image')pos=p});e.commands.setNodeSelection(pos);document.querySelector('[data-pic=redraw]').click();document.querySelector('#ask-actions button').click();}''')
        print({'settled':settled,'calls':page.evaluate('calls'),'result':page.locator('#ask-result').text_content()})
