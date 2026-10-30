"""Real /api/visual-edit, JobRunner and /api/job, with the producer replaced."""
import threading
import time
from pathlib import Path
from contextlib import contextmanager
import pytest
from PIL import Image
from core import store, versions
from core.agents.interactive import VisualProducerService
from ui import jobs
from ui.views import common, system
from test_editor_audit import _picture_article, OLD
from review_dd8_support import opened

@pytest.fixture
def queued(monkeypatch, tmp_path):
    runner = jobs.JobRunner(tmp_path/'jobs.json')
    monkeypatch.setattr(common, 'runner', runner)
    monkeypatch.setattr(system, 'runner', runner)
    monkeypatch.setattr(common, 'missing_keys', lambda *args: [])
    # Let the real worker run, but only after the test arranges a race.
    monkeypatch.setattr(runner, '_ensure_worker', lambda: None)
    return runner

def finish(runner, studio, jid):
    jobs.JobRunner._ensure_worker(runner)
    end = time.monotonic()+4
    while time.monotonic()<end:
        reply=studio.get('/api/job/'+jid).json
        if reply['status'] in ('done','failed'): return reply
        time.sleep(.01)
    raise AssertionError('job did not finish')

@contextmanager
def locked(art):
    ready, stop = threading.Event(), threading.Event()
    def hold():
        with art.lock(holder='reviewer lock'):
            ready.set(); stop.wait(5)
    thread=threading.Thread(target=hold)
    thread.start(); assert ready.wait(2)
    try: yield
    finally: stop.set(); thread.join()

@pytest.mark.parametrize('scenario', ['success','stale_initial','busy_initial','stale_queued','busy_queued','badfield','inline_kind','producer_error'])
def test_redraw_route(studio, output_dir, monkeypatch, queued, scenario):
    art=_picture_article(output_dir, 'reviewer-redraw')
    art.save_run({'review': {'reviews':[{'id':'review-1'}], 'changes':[], 'findings':[
        {'id':'finding-1','review':'review-1','status':'open','target':{'asset':'assets/chart_1.png'},'text':'The labels overlap.'}]}})
    seen=[]
    def producer(self, directory, rel, feedback='', **kwargs):
        seen.append({'feedback':feedback,'alt':kwargs.get('alt')})
        if scenario=='producer_error': return {'error':'stand-in producer failed'}
        Image.new('RGB',(20,20),'blue').save(Path(directory)/rel)
        return {'ok':True, 'alt':'New chart','caption':'New caption.', 'changed_kind':scenario=='inline_kind'}
    monkeypatch.setattr(VisualProducerService,'edit_visual',producer)
    if scenario=='inline_kind': art.write_article('', 'Before ![Old chart](assets/chart_1.png) after.\n')
    oldtext, oldpng, oldversion = art.text(), art.path('assets/chart_1.png').read_bytes(), art.version()
    payload={'slug':art.slug,'path':'assets/chart_1.png','version':oldversion,'feedback':'Make it clear'}
    if scenario=='stale_initial': payload['version']='old'
    if scenario=='badfield': payload['path']='assets/chart_1.png?v=2'
    if scenario=='busy_initial':
        with locked(art): response=studio.post('/api/visual-edit',json=payload)
    else: response=studio.post('/api/visual-edit',json=payload)
    if scenario.endswith('initial'):
        assert response.status_code==409 and response.json['error'] and not seen
        return
    assert response.status_code==202
    if scenario=='stale_queued': art.write_article('', oldtext+'\nNew paragraph.\n')
    if scenario=='busy_queued':
        with locked(art): result=finish(queued,studio,response.json['job'])
    else: result=finish(queued,studio,response.json['job'])
    print(scenario, result)
    if scenario=='success':
        assert result['status']=='done'
        assert result['result']['version']==art.version()!=oldversion
        assert '![New chart]' in art.text() and '*New caption.*' in art.text()
        assert 'The labels overlap.' in seen[0]['feedback'] and seen[0]['alt']=='Old chart'
        finding=art.run()['review']['findings'][0]
        assert finding['status']=='changed' and finding['changed_by']['who']=='the editor'
        assert versions.listing(art)[0]['who']=='the editor'
    else:
        assert result['status']=='failed' and result['error']
        assert art.path('assets/chart_1.png').read_bytes()==oldpng
        assert art.run()['review']['findings'][0]['status']=='open'
        if scenario!='stale_queued': assert art.text()==oldtext
        if scenario in ('stale_queued','busy_queued','badfield'): assert not seen
