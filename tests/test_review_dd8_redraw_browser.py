"""Browser routing to Flask's in-process test client; no socket or HTTP server is opened."""
import time
from pathlib import Path
from contextlib import ExitStack
from urllib.parse import urlsplit
import pytest
from PIL import Image
from playwright.sync_api import sync_playwright
from core.agents.interactive import VisualProducerService
from ui import jobs
from test_editor_audit import _picture_article
from test_review_dd8_redraw import queued, locked

@pytest.mark.parametrize('scenario',['success_twice','stale_initial','stale_queued','busy_initial','busy_queued','badfield','inline_kind','producer_error'])
def test_redraw_messages_and_reload(studio,output_dir,monkeypatch,queued,scenario):
    art=_picture_article(output_dir,'redraw-browser')
    if scenario=='badfield': art.write_article('', '![Old chart](assets/chart_1.png?v=2)\n')
    if scenario=='inline_kind': art.write_article('', 'Before ![Old chart](assets/chart_1.png) after.\n')
    calls=[]
    def producer(self,directory,rel,*args,**kwargs):
        calls.append(rel)
        if scenario=='producer_error': return {'error':'The stand-in could not draw the picture.'}
        Image.new('RGB',(20,20),'blue' if len(calls)==1 else 'green').save(Path(directory)/rel)
        return {'ok':True,'alt':f'New chart {len(calls)}','caption':f'New caption {len(calls)}.', 'changed_kind':scenario=='inline_kind'}
    monkeypatch.setattr(VisualProducerService,'edit_visual',producer)
    with ExitStack() as locks, sync_playwright() as p:
        browser=p.chromium.launch(args=['--no-sandbox','--single-process','--no-zygote'],chromium_sandbox=False)
        try:
            page=browser.new_page()
            navigations=[]
            page.on('framenavigated', lambda frame:navigations.append(frame.url) if frame==page.main_frame else None)
            def route(reqroute):
                req=reqroute.request
                u=urlsplit(req.url)
                assert u.hostname=='reviewer.invalid', req.url
                path=u.path+('?' +u.query if u.query else '')
                if req.method=='POST':
                    response=studio.post(path,json=req.post_data_json)
                    if path=='/api/visual-edit' and response.status_code==202:
                        if scenario=='stale_queued': art.write_article('',art.text()+'\nA concurrent edit.\n')
                        if scenario=='busy_queued': locks.enter_context(locked(art))
                        jobs.JobRunner._ensure_worker(queued)
                else: response=studio.get(path)
                reqroute.fulfill(status=response.status_code,body=response.get_data(),headers={'Content-Type':response.content_type})
            page.route('**/*',route)
            page.goto('http://reviewer.invalid/a/redraw-browser/edit')
            page.wait_for_selector('.ProseMirror')
            initial=page.locator('#editor-ui').get_attribute('data-version')
            if scenario=='stale_initial': art.write_article('',art.text()+'\nA concurrent edit.\n')
            if scenario=='busy_initial': locks.enter_context(locked(art))
            for n in range(2 if scenario=='success_twice' else 1):
                page.locator('.ProseMirror img[src]').click()
                page.locator('[data-pic=redraw]').click()
                page.locator('#ask-actions button').filter(has_text='Make it again').click()
                if scenario=='success_twice':
                    page.wait_for_function("n => document.querySelector('.ProseMirror img')?.alt === 'New chart '+n",arg=n+1)
                    assert page.locator('#editor-ui').get_attribute('data-version')==art.version()!=initial
                    assert f'New caption {n+1}.' in page.locator('.ProseMirror').text_content()
                    assert len(navigations)==n+2, navigations
                else:
                    page.wait_for_selector('#ask-result .is-error')
                    text=page.locator('#ask-result').text_content()
                    wanted={'busy_initial':'a job is working','busy_queued':'being worked on','stale_initial':'changed after','stale_queued':'changed after','badfield':'not one of','inline_kind':'Put it on a line of its own','producer_error':'stand-in could not draw'}[scenario]
                    assert wanted in text,text
                    assert len(navigations)==1
        finally: browser.close()
