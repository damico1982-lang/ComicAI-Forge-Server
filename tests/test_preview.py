"""Read-only whole-book preview metadata, without paid provider transport."""
import uuid
import pytest
from fastapi.testclient import TestClient
from comic_server.app import create_app
from test_studio import ReviewProvider, until


@pytest.mark.parametrize('layout',['GRID','CINEMATIC'])
def test_full_storyboard_preview_retains_layout_all_pages_and_no_artwork(tmp_path,layout):
    provider=ReviewProvider();app=create_app(tmp_path,'https://testserver',provider)
    app.state.store.create_user('damico','private-test-password')
    with TestClient(app) as client:
        client.headers['Authorization']='Bearer '+client.post('/v1/login',json={'username':'damico','password':'private-test-password'}).json()['accessToken']
        key=str(uuid.uuid4())
        req={'requestId':key,'rawScriptInput':'Mico and Cynika across all 35 pages.','selectedArtStyle':'Comic','targetPageCount':35,'panelsPerPage':6,'layoutStyle':layout,'requireArtworkApproval':True}
        job=client.post('/v1/jobs',json=req,headers={'Idempotency-Key':key}).json()['jobId']
        first=until(client,job,'awaiting_approval')
        again=client.get(f'/v1/jobs/{job}').json()
        assert again['layoutStyle']==layout and again['revision']==first['revision']
        assert len(again['storyboard']['pages'])==35
        assert len(again['storyboard']['pages'][-1]['panels'])==6
        assert again['storyboard']['pages'][-1]['panels'][-1]['dialogue']=='Mico: My turn.'
        assert again['completedPanels']==0 and again['status']=='awaiting_approval'
        assert provider.images==0
