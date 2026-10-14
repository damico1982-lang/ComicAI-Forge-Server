import base64
import io
import json
import threading
import time
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from comic_server.app import create_app
from comic_server.models import GenerationRequest, StoryPanel, StoryPage, Storyboard
from comic_server.provider import OpenAIProvider, ProviderError
from comic_server.store import Store


def png():
    output = io.BytesIO()
    Image.new("RGB", (160, 240), "purple").save(output, format="PNG")
    return output.getvalue()


class FixtureProvider:
    """Only injected into tests; never selected by production configuration."""
    configured = True

    def __init__(self):
        self.images = 0
        self.fail_after = None

    def plan(self, request):
        panel = StoryPanel(description="A test scene", caption="Exact caption", dialogue="Mico: Test dialogue",
            bubbleType="SPEECH", advanceTrigger="TAP_TO_NEXT", durationMillis=0)
        return Storyboard(characterBible="Consistent test character", pages=[
            StoryPage(panels=[panel.model_copy() for _ in range(request.panelsPerPage)]) for _ in range(request.targetPageCount)])

    def image(self, *args, **kwargs):
        if self.fail_after is not None and self.images >= self.fail_after:
            raise ProviderError("Injected provider failure")
        self.images += 1
        return png()


def payload(pages=1, request_id=None):
    return {"requestId": request_id or str(uuid.uuid4()), "rawScriptInput": "A test story",
        "selectedArtStyle": "Comic", "targetPageCount": pages}


def auth(client, username="mico"):
    response = client.post("/v1/login", json={"username": username, "password": "long-test-password"})
    assert response.status_code == 200
    return {"Authorization": "Bearer " + response.json()["accessToken"]}


def submit(client, data, headers):
    return client.post("/v1/jobs", json=data, headers={**headers, "Idempotency-Key": data["requestId"]})


def finished(client, job_id, headers):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get("/v1/jobs/" + job_id, headers=headers)
        assert response.status_code == 200
        value = response.json()
        if value["status"] not in ("queued", "running"):
            return value
        time.sleep(0.01)
    raise AssertionError("Job did not finish")


@pytest.fixture
def setup(tmp_path):
    provider = FixtureProvider()
    app = create_app(tmp_path, "https://testserver", provider)
    app.state.store.create_user("mico", "long-test-password")
    app.state.store.create_user("other", "long-test-password")
    with TestClient(app) as client:
        yield client, provider


def test_authentication_logout_and_user_isolation(setup):
    client, _ = setup
    assert client.get("/v1/jobs").status_code == 401
    assert client.post("/v1/login", json={"username": "mico", "password": "wrong"}).status_code == 401
    headers = auth(client)
    created = submit(client, payload(), headers).json()
    assert client.get("/v1/jobs/" + created["jobId"], headers=auth(client, "other")).status_code == 404
    assert client.post("/v1/logout", headers=headers).status_code == 204
    assert client.get("/v1/jobs", headers=headers).status_code == 401


def test_end_to_end_contract_images_and_idempotency(setup):
    client, provider = setup
    headers = auth(client)
    data = payload(2)
    response = submit(client, data, headers)
    assert response.status_code == 202
    job_id = response.json()["jobId"]
    result = finished(client, job_id, headers)
    assert result["status"] == "completed"
    assert result["completedPanels"] == 8
    comic = result["result"]
    assert comic["totalPages"] == 2
    assert len(comic["parsedPages"]) == 2
    url = comic["parsedPages"][0]["panels"][0]["imageUrl"]
    dialogue = comic["parsedPages"][0]["panels"][0]["dialogues"][1]
    assert dialogue["speaker"]["name"] == "Mico"
    assert dialogue["textManifest"]["primaryLine"] == "Test dialogue"
    image = client.get(url)
    assert image.status_code == 200
    assert Image.open(io.BytesIO(image.content)).size == (160, 240)
    assert client.get(url.replace("signature=", "signature=invalid")).status_code == 403
    assert submit(client, data, headers).json()["jobId"] == job_id
    assert provider.images == 8
    changed = {**data, "rawScriptInput": "Different story"}
    assert submit(client, changed, headers).status_code == 409


def test_failed_job_resumes_only_missing_panels(setup):
    client, provider = setup
    provider.fail_after = 2
    headers = auth(client)
    job = submit(client, payload(), headers).json()["jobId"]
    result = finished(client, job, headers)
    assert result["status"] == "failed"
    assert result["completedPanels"] == 2
    provider.fail_after = None
    assert client.post(f"/v1/jobs/{job}/resume", headers=headers).status_code == 200
    assert finished(client, job, headers)["status"] == "completed"
    assert provider.images == 4


def test_missing_provider_is_explicit_error(tmp_path):
    app = create_app(tmp_path, "https://testserver", OpenAIProvider(api_key=""))
    app.state.store.create_user("mico", "long-test-password")
    with TestClient(app) as client:
        assert submit(client, payload(), auth(client)).status_code == 503


def test_input_validation_and_idempotency_header(setup):
    client, _ = setup
    headers = auth(client)
    assert submit(client, payload(36), headers).status_code == 422
    data = payload()
    assert client.post("/v1/jobs", json=data, headers=headers).status_code == 400
    assert submit(client, {**data, "rawScriptInput": " "}, headers).status_code == 422


def test_restart_and_password_reset_revoke_old_session(tmp_path):
    store = Store(tmp_path)
    store.create_user("mico", "long-test-password")
    token = store.login("mico", "long-test-password")["accessToken"]
    job = store.create_job("mico", GenerationRequest.model_validate(payload()))
    store.claim()
    reopened = Store(tmp_path)
    reopened.interrupt_running()
    assert reopened.job(job["id"])["status"] == "interrupted"
    reopened.create_user("mico", "new-long-password")
    assert reopened.user_for_token(token) is None


def test_login_lockout(tmp_path):
    store = Store(tmp_path)
    store.create_user("mico", "long-test-password")
    for _ in range(10):
        assert store.login("mico", "wrong") is None
    with pytest.raises(PermissionError):
        store.login("mico", "long-test-password")


def test_real_provider_http_contract_without_paid_request():
    requests = []
    fixture = FixtureProvider()
    request = GenerationRequest.model_validate(payload())
    board = fixture.plan(request)
    def handler(http_request):
        requests.append(json.loads(http_request.content))
        assert http_request.headers["authorization"] == "Bearer unit-test-key"
        if http_request.url.path.endswith("responses"):
            return httpx.Response(200, json={"status": "completed", "output": [{"type": "message",
                "content": [{"type": "output_text", "text": board.model_dump_json()}]}]})
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png()).decode()}]})
    provider = OpenAIProvider(api_key="unit-test-key", transport=httpx.MockTransport(handler))
    planned = provider.plan(request)
    image = provider.image(planned.pages[0].panels[0], planned.characterBible, "Comic")
    assert Image.open(io.BytesIO(image)).size == (160, 240)
    assert requests[0]["text"]["format"]["strict"] is True
    assert requests[1]["model"] == "gpt-image-1"
    assert "response_format" not in requests[1]


def test_provider_failure_has_no_secret_or_prompt():
    provider = OpenAIProvider(api_key="secret-key", transport=httpx.MockTransport(
        lambda request: httpx.Response(429, json={"error": {"message": "Sensitive provider details"}})))
    with pytest.raises(ProviderError) as error:
        provider.plan(GenerationRequest.model_validate(payload()))
    assert "429" in str(error.value)
    assert "secret-key" not in str(error.value)
    assert "Sensitive" not in str(error.value)


def test_cancel_and_resume_preserves_completed_panel(tmp_path):
    class GatedProvider(FixtureProvider):
        def __init__(self):
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()
        def image(self, *args, **kwargs):
            if self.images == 0:
                self.entered.set()
                assert self.release.wait(5)
            return super().image(*args, **kwargs)
    provider = GatedProvider()
    app = create_app(tmp_path, "https://testserver", provider)
    app.state.store.create_user("mico", "long-test-password")
    with TestClient(app) as client:
        headers = auth(client)
        job_id = submit(client, payload(), headers).json()["jobId"]
        assert provider.entered.wait(3)
        assert client.post(f"/v1/jobs/{job_id}/cancel", headers=headers).json()["status"] == "cancelled"
        provider.release.set()
        deadline = time.monotonic() + 3
        while app.state.store.job(job_id)["completed"] < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert app.state.store.job(job_id)["completed"] == 1
        assert client.post(f"/v1/jobs/{job_id}/resume", headers=headers).status_code == 200
        assert finished(client, job_id, headers)["status"] == "completed"
        assert provider.images == 4


def test_expired_session_is_rejected(setup):
    client, _ = setup
    headers = auth(client)
    with client.app.state.store.connect() as db:
        db.execute("UPDATE sessions SET expires=0")
    assert client.get("/v1/jobs", headers=headers).status_code == 401


@pytest.mark.parametrize("count", [3, 4, 5, 6])
@pytest.mark.parametrize("layout", ["GRID", "CINEMATIC"])
def test_panel_choices_persist_download_and_do_not_overlap(setup, count, layout):
    client, provider = setup
    headers = auth(client)
    data = {**payload(2), "panelsPerPage": count, "layoutStyle": layout,
            "continuityNotes": "Mico wears jersey 12. Keep the same face and clothing."}
    created = submit(client, data, headers)
    assert created.status_code == 202
    job_id = created.json()["jobId"]
    done = finished(client, job_id, headers)
    assert done["status"] == "completed"
    assert done["totalPanels"] == done["completedPanels"] == provider.images == 2 * count
    urls = []
    for page in done["result"]["parsedPages"]:
        assert len(page["panels"]) == count
        boxes = [p["boundingBox"] for p in page["panels"]]
        for i, box in enumerate(boxes):
            assert 0 <= box["xMin"] < box["xMax"] <= 1
            assert 0 <= box["yMin"] < box["yMax"] <= 1
            for other in boxes[i + 1:]:
                assert (box["xMax"] <= other["xMin"] or other["xMax"] <= box["xMin"]
                        or box["yMax"] <= other["yMin"] or other["yMax"] <= box["yMin"])
        urls.extend(p["imageUrl"] for p in page["panels"])
    assert len(set(urls)) == 2 * count
    assert client.get(urls[-1]).status_code == 200
    assert submit(client, data, headers).json()["jobId"] == job_id
    assert submit(client, {**data, "panelsPerPage": 3 if count != 3 else 6}, headers).status_code == 409


@pytest.mark.parametrize("value", [2, 7, True, "4"])
def test_panel_count_rejects_invalid_values(setup, value):
    client, _ = setup
    assert submit(client, {**payload(), "panelsPerPage": value}, auth(client)).status_code == 422


def test_legacy_request_serialization_stays_compatible():
    data = payload()
    assert json.loads(GenerationRequest.model_validate(data).model_dump_json(exclude_defaults=True)) == data


def test_long_book_plans_bounded_ranges_with_shared_continuity():
    calls = []
    def handler(http_request):
        body = json.loads(http_request.content)
        source = json.loads(body["input"])
        calls.append(source)
        request = GenerationRequest.model_validate({**payload(source["pageCount"]), "panelsPerPage": source["panelsPerPage"]})
        board = FixtureProvider().plan(request)
        return httpx.Response(200, json={"status": "completed", "output": [{"type": "message",
            "content": [{"type": "output_text", "text": board.model_dump_json()}]}]})
    provider = OpenAIProvider(api_key="fixture", transport=httpx.MockTransport(handler))
    board = provider.plan(GenerationRequest.model_validate({**payload(35), "panelsPerPage": 6}))
    assert len(board.pages) == 35
    assert sum(len(page.panels) for page in board.pages) == 210
    assert [call["firstPage"] for call in calls] == [1, 6, 11, 16, 21, 26, 31]
    assert all(call["pageCount"] == 5 and call["totalBookPages"] == 35 for call in calls)
    assert calls[1]["continuityGuide"] == board.characterBible
    assert calls[1]["previousScene"]


def test_capabilities_and_published_release_do_not_advertise_unsigned_build(setup):
    client, _ = setup
    assert client.get("/v1/capabilities").json()["panelsPerPage"] == [3, 4, 5, 6]
    assert client.get("/v1/mobile-release").json()["versionCode"] == 2


@pytest.mark.parametrize("ratio,size", [(0.7, "1024x1536"), (1.0, "1024x1024"), (1.5, "1536x1024")])
def test_artwork_orientation_matches_panel_shape(ratio, size):
    def handler(request):
        assert json.loads(request.content)["size"] == size
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png()).decode()}]})
    provider = OpenAIProvider(api_key="fixture", transport=httpx.MockTransport(handler))
    panel = FixtureProvider().plan(GenerationRequest.model_validate(payload())).pages[0].panels[0]
    provider.image(panel, "guide", "comic", aspect_ratio=ratio)
