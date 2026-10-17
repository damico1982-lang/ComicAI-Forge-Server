"""Quote arithmetic and provider-size agreement, without provider network calls."""
import io
import base64
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from PIL import Image
from fastapi.testclient import TestClient

from comic_server.estimates import quote
from comic_server.models import GenerationRequest, Storyboard, StoryPage, StoryPanel
from comic_server.provider import OpenAIProvider
from comic_server.app import create_app
from test_studio import ReviewProvider, until


def request(**changes):
    return GenerationRequest(requestId=uuid4(), rawScriptInput="Mico protects Cynika.", selectedArtStyle="Comic",
                             targetPageCount=1, panelsPerPage=6, requireArtworkApproval=True, **changes)


def board(count=6):
    return Storyboard(characterBible="Mico and Cynika", pages=[StoryPage(panels=[StoryPanel(description="Rooftop") for _ in range(count)])])


def test_quote_has_precise_subtotals_and_displayed_lines_reconcile():
    value=quote(request(),board(),ReviewProvider(),reference_images=2)
    assert value["outputLineItems"]==[{"id":"1024x1024","label":"1024x1024 high-quality image output","quantity":6,"unitPrice":"0.167","subtotal":"1.002"}]
    assert value["imageOutputSubtotal"]=="1.002"
    assert value["roundingAdjustment"]=="0.008"
    assert value["referenceAllowanceLow"]=="0.12" and value["referenceAllowanceHigh"]=="2.40"
    assert value["promptAndVisionAllowance"]=="0.12"
    assert value["estimatedLow"]=="1.13" and value["estimatedHigh"]=="3.53"
    for side in ["Low","High"]:
        parts=Decimal(value["imageOutputSubtotal"])+Decimal(value["roundingAdjustment"])+Decimal(value["referenceAllowance"+side])
        if side=="High":parts+=Decimal(value["promptAndVisionAllowance"])
        assert parts==Decimal(value["estimated"+side])
    assert value["approvalAmount"]==value["estimatedHigh"] and value["totalIsExact"] is False
    assert "not measured" in value["planningNotice"]


def test_cinematic_mixed_sizes_and_completed_prefix_are_not_billed_twice():
    value=quote(request(layoutStyle="CINEMATIC"),board(),ReviewProvider(),completed=2,reference_images=0)
    assert [(l["id"],l["quantity"]) for l in value["outputLineItems"]]==[("1024x1024",1),("1024x1536",3)]
    assert value["imageOutputSubtotal"]=="0.917" and value["remainingImages"]==4 and value["completedImages"]==2
    done=quote(request(),board(),ReviewProvider(),completed=999,reference_images=8)
    assert done["estimatedHigh"]=="0.00" and done["outputLineItems"]==[] and done["remainingImages"]==0
    assert quote(request(),board(),ReviewProvider(),completed=-1)["remainingImages"]==6


@pytest.mark.parametrize("style,count",[("GRID",n) for n in range(3,7)]+[("CINEMATIC",n) for n in range(3,7)])
def test_quoted_sizes_match_actual_provider_request_sizes(style,count):
    from comic_server.layouts import panel_layout,panel_aspect_ratio
    calls=[]
    out=io.BytesIO();Image.new("RGB",(32,32),"purple").save(out,"PNG")
    def handler(req):
        import json
        calls.append(json.loads(req.content)["size"])
        return httpx.Response(200,json={"data":[{"b64_json":base64.b64encode(out.getvalue()).decode()}]})
    provider=OpenAIProvider(api_key="fixture-only",transport=httpx.MockTransport(handler))
    req=request(layoutStyle=style)
    value=quote(req,board(count),provider)
    for box in panel_layout(count,style):provider.image(StoryPanel(description="Rooftop"),"Guide","Comic",aspect_ratio=panel_aspect_ratio(box))
    from collections import Counter
    assert dict(Counter(calls))=={line["id"]:line["quantity"] for line in value["outputLineItems"]}


def test_unknown_model_never_offers_a_fabricated_exact_price():
    provider=ReviewProvider();provider.image_model="unpriced-model"
    value=quote(request(),board(),provider)
    assert value["available"] is False and "estimatedHigh" not in value


def test_review_counts_actual_selected_image_uploads_regardless_of_role(tmp_path):
    from test_uploads import put,png
    provider=ReviewProvider();provider.plan=lambda req,references=None:board()
    app=create_app(tmp_path,"https://testserver",provider)
    app.state.store.create_user("damico","private-test-password")
    with TestClient(app) as client:
        token=client.post("/v1/login",json={"username":"damico","password":"private-test-password"}).json()["accessToken"]
        client.headers["Authorization"]="Bearer "+token
        image=put(client,dict(client.headers),png(),"Reference.png","image/png").json()
        text=put(client,dict(client.headers),b"Style guidance","Style.txt","text/plain").json()
        req=request().model_dump(mode="json")
        req["attachments"]=[{"id":image["id"],"role":"DOCUMENT","useForAi":True},{"id":text["id"],"role":"STYLE","useForAi":True}]
        job=client.post("/v1/jobs",json=req,headers={"Idempotency-Key":req["requestId"]}).json()["jobId"]
        review=until(client,job,"awaiting_approval")
        value=review["estimate"]
        assert value["referenceImages"]==1 and value["referenceAllowanceHigh"]=="1.20"
        assert value["estimatedHigh"]=="2.33"
        rejected=client.post(f"/v1/jobs/{job}/approve",json={"revision":review["revision"],"approvedEstimateHigh":"2.32"})
        assert rejected.status_code==422
        assert client.get(f"/v1/jobs/{job}").json()["status"]=="awaiting_approval"
        assert provider.images==0
