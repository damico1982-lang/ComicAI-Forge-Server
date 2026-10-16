import io
import json
import shutil
import time
import uuid
from pathlib import Path

import pytest
import numpy as np
from fastapi.testclient import TestClient
from PIL import Image, ImageCms
from pypdf import PdfReader

from comic_server.app import create_app
from comic_server.models import StoryPanel, StoryPage, Storyboard
from comic_server.print_export import cmyk_pdf
from comic_server.vision import CharacterVision, intersection, suggest_positions


def test_approved_character_baseline_survives_undetectable_original_reference(tmp_path, monkeypatch):
    from comic_server.store import Store
    from comic_server.models import AttachmentReference
    store=Store(tmp_path);vision=CharacterVision(store)
    monkeypatch.setattr(vision,"detect",lambda *args,**kwargs: [])
    ref=AttachmentReference(id=uuid.uuid4(),useForAi=True,role="CHARACTER",label="Mico")
    vector=np.array([1.,0.],dtype=np.float32)
    with store.connect() as db:
        db.execute("INSERT INTO approved_character_faces VALUES (?,?,?,?,?,?)",("mico","Mico",vision.version,vector.tobytes(),"job",0))
        db.execute("INSERT INTO approved_character_faces VALUES (?,?,?,?,?,?)",("other","Mico",vision.version,vector.tobytes(),"job",0))
    values=vision.reference_vectors("mico",[ref])
    assert len(values)==1 and values[0][0]=="Mico"
    assert np.array_equal(values[0][1],vector)
    assert vision.reference_vectors("mico",[])==[]


def test_manual_face_review_requires_opt_in_and_keeps_user_ownership(tmp_path,monkeypatch):
    from test_server import FixtureProvider,auth,payload,submit,finished
    from test_uploads import put,png
    provider=FixtureProvider()
    original_plan=provider.plan
    provider.plan=lambda request,references=None: original_plan(request)
    app=create_app(tmp_path,"https://testserver",provider)
    app.state.store.create_user("mico","long-test-password")
    app.state.store.create_user("other","long-test-password")
    def fake_detect(self,content,embeddings=False):
        result={"rect":[.1,.1,.3,.3],"landmarks":[[.2,.2]],"confidence":.99}
        if embeddings:result["vector"]=np.array([1.,0.],dtype=np.float32)
        return [result]
    monkeypatch.setattr(CharacterVision,"detect",fake_detect)
    with TestClient(app) as client:
        headers=auth(client);other=auth(client,"other")
        ref=put(client,headers,png(),"Mico.png","image/png").json()
        data=payload();data["attachments"]=[{"id":ref["id"],"useForAi":True,"role":"CHARACTER","label":"Mico"}]
        job=submit(client,data,headers).json()["jobId"]
        assert finished(client,job,headers)["status"]=="completed"
        path=f"/v1/jobs/{job}/faces/0"
        assert client.get(path,headers=other).status_code==404
        assert client.post(path+"/analyze",json={},headers=other).status_code==404
        assert client.post(path+"/approve",json={"label":"Mico","faceIndex":0},headers=headers).status_code==422
        assert client.post(path+"/analyze",json={"compareFaces":False},headers=headers).status_code==200
        assert client.post(path+"/approve",json={"label":"Mico","faceIndex":0},headers=headers).status_code==422
        result=client.post(path+"/analyze",json={"compareFaces":True},headers=headers)
        assert result.status_code==200 and result.json()["reviewRequired"]
        assert client.post(path+"/approve",json={"label":"Other character","faceIndex":0},headers=headers).status_code==422
        assert client.post(path+"/approve",json={"label":"Mico","faceIndex":0},headers=other).status_code==404
        assert client.post(path+"/approve",json={"label":"Mico","faceIndex":0},headers=headers).json()["status"]=="approved_by_user"
        assert client.delete("/v1/character-embeddings",headers=other).status_code==204
        assert client.post(path+"/approve",json={"label":"Mico","faceIndex":0},headers=headers).status_code==200
        assert client.delete("/v1/character-embeddings",headers=headers).status_code==204
        assert client.post(path+"/approve",json={"label":"Mico","faceIndex":0},headers=headers).status_code==422
        for table in ("character_embeddings","generated_face_embeddings","approved_character_faces"):
            with app.state.store.connect() as db:assert db.execute("SELECT COUNT(*) FROM "+table).fetchone()[0]==0


class ReviewProvider:
    configured=True
    image_model="gpt-image-1"
    text_model="gpt-4.1-mini"
    def __init__(self):self.images=0
    def plan(self,request):
        panel=StoryPanel(description="A hero on a rooftop",caption="Charlotte.",dialogue="Mico: My turn.",bubbleType="SPEECH",advanceTrigger="TAP_TO_NEXT",durationMillis=0)
        return Storyboard(characterBible="Approved Mico reference.",pages=[StoryPage(panels=[panel.model_copy() for _ in range(3 if request.autoPanelCount else request.panelsPerPage)]) for _ in range(request.targetPageCount)])
    def image(self,*args,**kwargs):
        self.images+=1;out=io.BytesIO();Image.new("RGB",(160,240),"purple").save(out,"PNG");return out.getvalue()


def until(client,job_id,status):
    for _ in range(200):
        data=client.get(f"/v1/jobs/{job_id}").json()
        if data["status"]==status:return data
        if data["status"]=="failed":raise AssertionError(data)
        time.sleep(.02)
    raise AssertionError(data)


def test_artwork_requires_saved_revision_approval_and_stale_edits_are_rejected(tmp_path):
    provider=ReviewProvider();app=create_app(tmp_path,"https://example.com",provider)
    app.state.store.create_user("damico","private-test-password")
    with TestClient(app) as client:
        token=client.post("/v1/login",json={"username":"damico","password":"private-test-password"}).json()["accessToken"]
        client.headers["Authorization"]="Bearer "+token
        key=str(uuid.uuid4());request={"requestId":key,"rawScriptInput":"A hero discovers his powers.","selectedArtStyle":"Comic","targetPageCount":1,"panelsPerPage":4,"autoPanelCount":True,"requireArtworkApproval":True}
        response=client.post("/v1/jobs",json=request,headers={"Idempotency-Key":key});assert response.status_code==202
        job_id=response.json()["jobId"];job=until(client,job_id,"awaiting_approval")
        assert provider.images==0;assert job["totalPanels"]==3;assert job["estimate"]["available"]
        old=job["revision"];board=job["storyboard"];board["pages"][0]["panels"][0]["description"]="A revised approved scene."
        edited=client.post(f"/v1/jobs/{job_id}/storyboard",json={"expectedRevision":old,"storyboard":board});assert edited.status_code==200
        job=edited.json();assert job["revision"]!=old
        assert client.post(f"/v1/jobs/{job_id}/approve",json={"revision":old,"approvedEstimateHigh":"999.00"}).status_code==409
        assert client.post(f"/v1/jobs/{job_id}/approve",json={"revision":job["revision"],"approvedEstimateHigh":"0.00"}).status_code==422
        assert provider.images==0
        approved={"revision":job["revision"],"approvedEstimateHigh":job["estimate"]["estimatedHigh"]}
        assert client.post(f"/v1/jobs/{job_id}/approve",json=approved).status_code==200
        until(client,job_id,"completed");assert provider.images==3
        assert client.post(f"/v1/jobs/{job_id}/approve",json=approved).status_code==200
        assert provider.images==3


def test_phone_storyboard_save_accepts_omitted_kotlin_defaults_without_artwork(tmp_path):
    provider=ReviewProvider();app=create_app(tmp_path,"https://example.com",provider)
    app.state.store.create_user("damico","private-test-password")
    with TestClient(app) as client:
        token=client.post("/v1/login",json={"username":"damico","password":"private-test-password"}).json()["accessToken"]
        client.headers["Authorization"]="Bearer "+token
        key=str(uuid.uuid4());body={"requestId":key,"rawScriptInput":"Mico and Cynika talk.","selectedArtStyle":"Comic","targetPageCount":1,"panelsPerPage":4,"requireArtworkApproval":True}
        job_id=client.post("/v1/jobs",json=body,headers={"Idempotency-Key":key}).json()["jobId"]
        job=until(client,job_id,"awaiting_approval");board=job["storyboard"]
        for panel in board["pages"][0]["panels"]:
            panel.pop("bubbleType");panel.pop("advanceTrigger");panel.pop("durationMillis")
        first=board["pages"][0]["panels"][0]
        first.pop("caption");first.pop("dialogue");first["description"]="Edited silent panel."
        edited=client.post(f"/v1/jobs/{job_id}/storyboard",json={"expectedRevision":job["revision"],"storyboard":board})
        assert edited.status_code==200,edited.text
        panel=edited.json()["storyboard"]["pages"][0]["panels"][0]
        assert panel["bubbleType"]=="SPEECH" and panel["durationMillis"]==0 and panel["caption"]==panel["dialogue"]==""
        assert edited.json()["revision"]!=job["revision"] and provider.images==0


def test_free_space_search_considers_entire_rectangles():
    out=io.BytesIO();Image.new("RGB",(160,240),"white").save(out,"PNG")
    face=[.1,.05,.9,.45]
    suggestions=suggest_positions(out.getvalue(),["Caption.","Mico: A line."],[face])
    rects=[[p["x"],p["y"],p["x"]+p["width"],p["y"]+p["height"]] for p in suggestions]
    assert all(intersection(r,face)==0 for r in rects)
    assert intersection(*rects)==0


def test_no_detected_face_is_unverified_not_a_perfect_match(tmp_path):
    from comic_server.store import Store
    store=Store(tmp_path);vision=CharacterVision(store)
    out=io.BytesIO();Image.new("RGB",(200,200),"white").save(out,"PNG")
    p=StoryPanel(description="Empty",caption="",dialogue="",bubbleType="SPEECH",advanceTrigger="TAP_TO_NEXT",durationMillis=0)
    result=vision.analyze(out.getvalue(),p,"damico",[],True)
    assert result["reviewRequired"] is True;assert result["characterChecks"][0]["status"]=="unverified"
    assert result["protectedRegions"]==[]


def test_cmyk_pdf_embeds_profile_and_contains_four_channel_pixels(tmp_path):
    profile=Path("/usr/share/color/icc/ghostscript/default_cmyk.icc")
    if not profile.is_file():pytest.skip("Local CMYK validation profile is not installed")
    # Full-size 300-DPI A4 source confirms dimensions and strip conversion.
    source=tmp_path/"page.png";Image.new("RGB",(2480,3508),(100,30,180)).save(source)
    target=tmp_path/"print.pdf";report=cmyk_pdf([source],profile,target,300)
    document=PdfReader(target);assert len(document.pages)==1
    image=list(document.pages[0]["/Resources"]["/XObject"].get_object().values())[0].get_object()
    assert image["/ColorSpace"][0]=="/ICCBased";assert image["/ColorSpace"][1].get_object()["/N"]==4
    assert len(image.get_data())==2480*3508*4
    assert report["mode"]=="CMYK";assert report["iccSha256"]
    rgb=tmp_path/"rgb.icc";rgb.write_bytes(ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
    with pytest.raises(ValueError):cmyk_pdf([source],rgb,tmp_path/"bad.pdf",300)
