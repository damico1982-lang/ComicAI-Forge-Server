import base64
import hashlib
import io
import json
import uuid
import zipfile
from email.parser import BytesParser
from email.policy import default

import httpx
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
from comic_server import uploads
from comic_server.models import GenerationRequest, AttachmentReference
from comic_server.provider import OpenAIProvider
from test_server import setup, auth, png, payload, submit, finished, FixtureProvider


def put(client, headers, data, name="file.bin", mime="application/octet-stream", upload_id=None):
    upload_id = upload_id or str(uuid.uuid4())
    return client.put(f"/v1/uploads/{upload_id}", params={"filename": name}, content=data,
        headers={**headers, "Content-Type": mime, "X-Content-SHA256": hashlib.sha256(data).hexdigest()})


def test_any_file_photo_roundtrip_idempotency_and_ownership(setup):
    client, _ = setup; headers = auth(client); other = auth(client, "other")
    for data, name, mime in [(b"\x00arbitrary", "archive.unknown", "application/octet-stream"),
                             (png(), "portrait.png", "image/png"), (b"A true story", "script.txt", "text/plain")]:
        response = put(client, headers, data, name, mime)
        assert response.status_code == 200
        row = response.json(); path = f"/v1/uploads/{row['id']}"
        assert row["sha256"] == hashlib.sha256(data).hexdigest()
        assert client.get(path + "/content", headers=headers).content == data
        assert client.get(path + "/content").status_code == 401
        assert client.get(path + "/content", headers=other).status_code == 404
        assert client.delete(path, headers=other).status_code == 404
        assert put(client, headers, data, name, mime, row["id"]).json() == row
        assert put(client, other, data, name, mime, row["id"]).status_code == 409
        assert put(client, headers, b"different", name, mime, row["id"]).status_code == 409
        assert client.get(path + "/content", headers=headers).content == data
        assert client.delete(path, headers=headers).status_code == 204
        assert client.get(path, headers=headers).status_code == 404


def test_hash_stream_size_quota_and_partial_cleanup(setup, monkeypatch):
    client, _ = setup; headers = auth(client)
    monkeypatch.setattr(uploads, "MAX_FILE_BYTES", 32)
    assert put(client, headers, b"x" * 33).status_code == 413
    chunks = [b"x" * 20, b"x" * 20]
    response = client.put(f"/v1/uploads/{uuid.uuid4()}", params={"filename": "large.bin"}, content=iter(chunks),
        headers={**headers, "X-Content-SHA256": hashlib.sha256(b"".join(chunks)).hexdigest()})
    assert response.status_code == 413
    response = client.put(f"/v1/uploads/{uuid.uuid4()}", params={"filename": "wrong.bin"}, content=b"abc",
        headers={**headers, "X-Content-SHA256": "0" * 64})
    assert response.status_code == 422
    assert not list(client.app.state.store.uploads.iterdir())
    monkeypatch.setattr(uploads, "MAX_USER_BYTES", 4)
    assert put(client, headers, b"abc").status_code == 200
    assert put(client, headers, b"de").status_code == 413
    assert len(list(client.app.state.store.uploads.iterdir())) == 1


def test_likeness_location_documents_reach_generation_and_restore(setup):
    client, provider = setup; headers = auth(client)
    calls = []; images = []
    original_plan, original_image = provider.plan, provider.image
    def plan(request, references=None):
        calls.extend(references or []); return original_plan(request)
    def image(*args, references=None):
        images.append(references); return original_image(*args)
    provider.plan, provider.image = plan, image
    portrait = put(client, headers, png(), "Mico-age12.png", "image/png").json()
    script = put(client, headers, b"Keep middle school jersey number 26.", "canon.txt", "text/plain").json()
    location = {"name": "Revolution Park", "address": "Charlotte NC", "mapsUrl": "https://www.google.com/maps/search/?api=1&query=Revolution+Park+Charlotte", "notes": "Night, during the origin chapter."}
    place = put(client, headers, json.dumps(location).encode(), "park.location.json", "application/json").json()
    data = payload(); data["attachments"] = [
        {"id": portrait["id"], "useForAi": True, "label": "Damico Walker", "notes": "Mico at age 12; preserve facial geometry", "role": "CHARACTER"},
        {"id": script["id"], "useForAi": True},
        {"id": place["id"], "useForAi": True, "role": "LOCATION", "label": "Revolution Park"}]
    response = submit(client, data, headers)
    assert response.status_code == 202, response.text
    job = finished(client, response.json()["jobId"], headers)
    assert job["status"] == "completed"
    assert calls[0]["label"] == "Damico Walker" and calls[0]["role"] == "CHARACTER"
    assert Image.open(io.BytesIO(calls[0]["image"])).format == "JPEG"
    assert "26" in calls[1]["text"] and "Charlotte" in calls[2]["text"]
    assert len(images) == 4 and all(refs[0]["label"] == "Damico Walker" for refs in images)
    assert len(job["result"]["attachments"]) == 3
    assert job["result"]["attachments"][0]["label"] == "Damico Walker"
    assert job["result"]["attachments"][0]["notes"].startswith("Mico at age 12")
    assert job["result"]["attachments"][2]["role"] == "LOCATION"
    assert client.delete(f"/v1/uploads/{portrait['id']}", headers=headers).status_code == 409
    assert submit(client, data, headers).json()["jobId"] == job["jobId"] and provider.images == 4


def test_missing_bad_unnamed_and_archive_references(setup):
    client, provider = setup; headers = auth(client)
    data = payload(); data["attachments"] = [{"id": str(uuid.uuid4()), "useForAi": True}]
    assert submit(client, data, headers).status_code == 404
    bad = put(client, headers, b"not an image", "bad.png", "image/png").json()
    data["attachments"] = [{"id": bad["id"], "useForAi": True}]
    assert submit(client, data, headers).status_code == 422
    photo = put(client, headers, png(), "photo.png", "image/png").json()
    data["attachments"] = [{"id": photo["id"], "useForAi": True, "role": "CHARACTER"}]
    assert submit(client, data, headers).status_code == 422 and provider.images == 0
    data["attachments"] = [{"id": bad["id"], "useForAi": False}]
    job = submit(client, data, headers).json()
    assert finished(client, job["jobId"], headers)["status"] == "completed"


def text_pdf(text):
    writer = PdfWriter(); page = writer.add_blank_page(300, 300)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    stream = DecodedStreamObject(); stream.set_data(f"BT /F1 12 Tf 10 200 Td ({text}) Tj ET".encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO(); writer.write(output); return output.getvalue()


def test_pdf_docx_and_scanned_documents(setup):
    client, _ = setup; headers = auth(client)
    pdf = put(client, headers, text_pdf("Damico Walker exact script"), "script.pdf", "application/pdf").json()
    assert pdf["kind"] == "text"
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Cynika speaks first.</w:t></w:r></w:p></w:body></w:document>')
    doc = put(client, headers, content.getvalue(), "script.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document").json()
    assert doc["kind"] == "text"
    refs = uploads.references(client.app.state.store, "mico", [AttachmentReference(id=pdf["id"], useForAi=True), AttachmentReference(id=doc["id"], useForAi=True)])
    assert "Damico Walker" in refs[0]["text"] and "Cynika speaks first." in refs[1]["text"]
    writer = PdfWriter(); writer.add_blank_page(100, 100); out = io.BytesIO(); writer.write(out)
    blank = put(client, headers, out.getvalue(), "scan.pdf", "application/pdf").json()
    assert blank["kind"] == "attachment" and "No readable text" in blank["notice"]


def test_provider_uses_labeled_photos_and_high_fidelity_edits():
    board = FixtureProvider().plan(GenerationRequest.model_validate(payload()))
    seen = []
    refs = [{"kind": "image", "name": "portrait.jpg", "label": "Damico Walker", "role": "CHARACTER", "notes": "Age 12, preserve face", "image": png()}]
    def handle(request):
        seen.append(request)
        if request.url.path.endswith("responses"):
            body = json.loads(request.content)
            assert body["input"][0]["content"][2]["type"] == "input_image"
            assert "Damico Walker" in body["input"][0]["content"][1]["text"]
            return httpx.Response(200, json={"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": board.model_dump_json()}]}]})
        assert request.url.path.endswith("images/edits")
        parsed = BytesParser(policy=default).parsebytes(b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content)
        values = {p.get_param("name", header="content-disposition"): p.get_payload(decode=True) for p in parsed.iter_parts()}
        assert values["image[]"] == png() and values["input_fidelity"] == b"high"
        assert b"Damico Walker" in values["prompt"] and b"facial geometry" in values["prompt"]
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png()).decode()}]})
    provider = OpenAIProvider(api_key="unit-test-key", transport=httpx.MockTransport(handle))
    planned = provider.plan(GenerationRequest.model_validate(payload()), refs)
    assert provider.image(planned.pages[0].panels[0], planned.characterBible, "Comic", refs) == png()
    assert len(seen) == 2
