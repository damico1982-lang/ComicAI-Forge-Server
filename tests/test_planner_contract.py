import copy
import json
import uuid

import httpx
import pytest

from comic_server.models import GenerationRequest, validate_storyboard
from comic_server.planner_contract import output_schema, parse_storyboard
from comic_server.provider import OpenAIProvider, ProviderError


def request(pages=1, auto=False):
    return GenerationRequest(requestId=uuid.uuid4(),rawScriptInput="Mico and Cynika talk on a rooftop.",
        selectedArtStyle="Comic",targetPageCount=pages,panelsPerPage=4,autoPanelCount=auto)


def planned(pages=1):
    panel={"description":"Mico listens to Cynika.","caption":"Charlotte.",
        "dialogues":[{"speaker":"Mico","text":"We?"},{"speaker":"Cynika","text":"Yeah. We."}],
        "bubbleType":"SPEECH","advanceTrigger":"TAP_TO_NEXT","durationMillis":3000}
    return {"characterBible":"Mico and Cynika, approved reference identities.","pages":[{"panels":[copy.deepcopy(panel) for _ in range(4)]} for _ in range(pages)]}


def response(board):
    return httpx.Response(200,json={"status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":json.dumps(board)}]}]})


def test_provider_schema_enforces_counts_text_formats_and_enums():
    schema=output_schema(request(5));assert schema["properties"]["pages"]["minItems"]==5
    assert schema["properties"]["pages"]["maxItems"]==5
    panel=schema["$defs"]["PlannedPage"]["properties"]["panels"]
    assert panel["minItems"]==panel["maxItems"]==4
    auto=output_schema(request(auto=True))["$defs"]["PlannedPage"]["properties"]["panels"]
    assert auto["minItems"]==3 and auto["maxItems"]==6
    assert schema["$defs"]["PlannedPanel"]["properties"]["caption"]["pattern"]==r"^[\s\S]{0,220}$"
    assert schema["$defs"]["PlannedPanel"]["properties"]["dialogues"]["maxItems"]==5
    assert "SHOUT" in schema["$defs"]["PlannedPanel"]["properties"]["bubbleType"]["enum"]
    assert schema["$defs"]["PlannedSpeech"]["additionalProperties"] is False


def test_structured_dialogues_keep_names_and_phone_contract():
    board=parse_storyboard(json.dumps(planned()));validate_storyboard(board,1)
    assert board.pages[0].panels[0].dialogue=="Mico: We?\nCynika: Yeah. We."


def test_provider_validation_reports_field_without_leaking_text_or_retrying():
    calls=[];board=planned();board["pages"][0]["panels"][2]["caption"]="PRIVATE-CAPTION-"*25
    def handler(req):calls.append(req);return response(board)
    provider=OpenAIProvider(api_key="private-test-token",transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as error:provider.plan(request())
    assert len(calls)==1
    assert "page 1, panel 3, caption" in str(error.value)
    assert "PRIVATE-CAPTION" not in str(error.value) and "private-test-token" not in str(error.value)
    assert "No panel images were requested" in str(error.value)


def test_failed_later_section_resumes_without_replanning_paid_completed_sections(tmp_path):
    calls=[];fail=True
    def handler(req):
        nonlocal fail
        body=json.loads(req.content);source=json.loads(body["input"]);calls.append(source["firstPage"])
        if source["firstPage"]==6 and fail:
            board=planned(1);board["pages"][0]["panels"]=[];return response(board)
        return response(planned(source["pageCount"]))
    provider=OpenAIProvider(api_key="fixture",transport=httpx.MockTransport(handler));r=request(6)
    checkpoint=tmp_path/"plan.json"
    with pytest.raises(ProviderError):provider.plan(r,checkpoint_path=checkpoint)
    assert calls==[1,6] and len(json.loads(checkpoint.read_text())["storyboard"]["pages"])==5
    fail=False;board=provider.plan(r,checkpoint_path=checkpoint)
    assert len(board.pages)==6 and calls==[1,6,6]
    provider.plan(r,checkpoint_path=checkpoint)
    assert calls==[1,6,6]


def test_changed_request_does_not_reuse_another_checkpoint(tmp_path):
    calls=[]
    provider=OpenAIProvider(api_key="fixture",transport=httpx.MockTransport(lambda req:(calls.append(req) or response(planned()))))
    checkpoint=tmp_path/"plan.json";r=request();provider.plan(r,checkpoint_path=checkpoint)
    with pytest.raises(ProviderError,match="do not match"):
        provider.plan(r.model_copy(update={"rawScriptInput":"Different story."}),checkpoint_path=checkpoint)
    assert len(calls)==1


def test_server_resume_returns_review_without_requesting_artwork(tmp_path):
    from fastapi.testclient import TestClient
    from comic_server.app import create_app
    from test_server import auth,submit,finished
    fail=True;calls=[]
    def handler(req):
        assert req.url.path.endswith("/responses"), "Artwork must wait for approval"
        calls.append(req);board=planned()
        if fail:board["pages"][0]["panels"][0]["caption"]="Too long. "*30
        return response(board)
    provider=OpenAIProvider(api_key="fixture",transport=httpx.MockTransport(handler))
    app=create_app(tmp_path,"https://testserver",provider);app.state.store.create_user("mico","long-test-password")
    with TestClient(app) as client:
        headers=auth(client);r=request().model_copy(update={"requireArtworkApproval":True})
        job=submit(client,r.model_dump(mode="json"),headers).json()["jobId"]
        failed=finished(client,job,headers)
        assert failed["status"]=="failed" and "caption" in failed["error"] and failed["completedPanels"]==0
        fail=False
        assert client.post(f"/v1/jobs/{job}/resume",headers=headers).status_code==200
        reviewed=finished(client,job,headers)
        assert reviewed["status"]=="awaiting_approval" and reviewed["completedPanels"]==0
        assert reviewed["storyboard"]["pages"][0]["panels"][0]["dialogue"]=="Mico: We?\nCynika: Yeah. We."
        assert len(calls)==2
