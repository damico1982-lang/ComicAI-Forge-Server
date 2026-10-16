import uuid

import pytest
from pydantic import ValidationError

from comic_server.models import GenerationRequest, Storyboard, StoryPage, StoryPanel, validate_storyboard


def test_long_script_contract_keeps_tail_and_rejects_oversize():
    text = "Mico keeps going.\n" * 5000 + "END BOOK THREE."
    request = GenerationRequest(requestId=uuid.uuid4(), rawScriptInput=text, selectedArtStyle="Comic", targetPageCount=35)
    assert request.rawScriptInput == text
    assert request.rawScriptInput.endswith("END BOOK THREE.")
    with pytest.raises(ValidationError):
        GenerationRequest(requestId=uuid.uuid4(), rawScriptInput="a" * 200001, selectedArtStyle="Comic", targetPageCount=35)


def test_multiple_speaker_turns_become_separate_lettering_nodes(tmp_path):
    from fastapi.testclient import TestClient
    from comic_server.app import create_app
    from test_server import FixtureProvider, auth, payload, submit, finished
    class Provider(FixtureProvider):
        def plan(self, request):
            board = super().plan(request)
            board.pages[0].panels[0].dialogue = "Mico: We?\nCynika: Yeah. We."
            return board
    app = create_app(tmp_path, "https://testserver", Provider())
    app.state.store.create_user("mico", "long-test-password")
    with TestClient(app) as client:
        headers = auth(client)
        job = submit(client,payload(),headers).json()["jobId"]
        result = finished(client,job,headers)
        nodes = result["result"]["parsedPages"][0]["panels"][0]["dialogues"]
        assert [n["speaker"]["name"] for n in nodes] == ["Narrator","Mico","Cynika"]
        assert [n["textManifest"]["primaryLine"] for n in nodes][1:] == ["We?","Yeah. We."]
        assert len({n["advanceCues"]["stepIndex"] for n in nodes}) == 3


def test_balloon_capacity_is_validated_before_artwork():
    panel = StoryPanel(description="Two people talking.",caption="Charlotte.",dialogue="\n".join(f"Mico: Line {i}." for i in range(6)),bubbleType="SPEECH",advanceTrigger="TAP_TO_NEXT",durationMillis=0)
    board = Storyboard(characterBible="Mico and Cynika.",pages=[StoryPage(panels=[panel]*4)])
    with pytest.raises(ValueError,match="five"):
        validate_storyboard(board,1)


def test_legacy_wrapped_dialogue_is_still_readable():
    from comic_server.models import dialogue_lines
    assert dialogue_lines("Mico: I am still\nhere.") == [("Mico","I am still here.")]
