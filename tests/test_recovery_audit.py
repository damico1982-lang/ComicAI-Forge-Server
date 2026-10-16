import json
import threading
import time
import uuid

from fastapi.testclient import TestClient
from comic_server.app import create_app
from comic_server.models import GenerationRequest
from comic_server.store import Store
from comic_server.worker import Worker
from test_server import FixtureProvider, auth
from test_uploads import put, png


def test_next_update_manifest_is_separate_from_legacy(tmp_path):
    app=create_app(tmp_path,"https://example.com",FixtureProvider())
    with TestClient(app) as client:
        current=client.get('/v1/mobile-release/next')
        assert current.status_code==200
        data=current.json()
        assert data['applicationId']=='com.example.comicapp.next'
        assert len(data['signerSha256'])==64 and data['versionCode']>=8
        assert data['downloadUrl'].startswith('https://')
        assert client.get('/v1/mobile-release').json()['versionCode']==2


def test_worker_final_state_cannot_overwrite_cancellation(tmp_path):
    store=Store(tmp_path);store.create_user('mico','long-test-password')
    job=store.create_job('mico',GenerationRequest(requestId=uuid.uuid4(),rawScriptInput='Test',selectedArtStyle='Comic',targetPageCount=1))
    store.claim();assert store.cancel(job['id'],'mico')
    worker=Worker(store,FixtureProvider())
    for status in ('completed','failed','interrupted'):
        worker._transition(job['id'],status,'late result')
        assert store.job(job['id'])['status']=='cancelled'


def test_active_print_source_cannot_be_deleted_then_released_after_completion(tmp_path,monkeypatch):
    import comic_server.print_routes as routes
    entered=threading.Event();finish=threading.Event()
    def convert(pages,profile,target,*args):
        entered.set();assert finish.wait(5)
        target.write_bytes(b'%PDF-fixture')
        return {'pages':1}
    monkeypatch.setattr(routes,'cmyk_pdf',convert)
    app=create_app(tmp_path,'https://example.com',FixtureProvider());app.state.store.create_user('mico','long-test-password')
    with TestClient(app) as client:
        headers=auth(client)
        page=put(client,headers,png(),'page.png','image/png').json()['id']
        profile=put(client,headers,b'profile','printer.icc').json()['id']
        request={'requestId':str(uuid.uuid4()),'pages':[page],'iccProfile':profile,'dpi':300}
        result=client.post('/v1/print-exports',headers=headers,json=request)
        assert result.status_code==202 and entered.wait(3)
        try:
            for upload in (page,profile):
                assert client.delete('/v1/uploads/'+upload,headers=headers).status_code==409
        finally:finish.set()
        path='/v1/print-exports/'+result.json()['exportId']
        for _ in range(100):
            if client.get(path,headers=headers).json()['status']=='completed':break
            time.sleep(.01)
        assert client.get(path,headers=headers).json()['status']=='completed'
        assert client.delete('/v1/uploads/'+page,headers=headers).status_code==204
        assert client.get(path+'/content',headers=headers).status_code==200
