import hmac
import hashlib
import json
import os
import time
import asyncio
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool
from . import uploads

from .models import GenerationRequest, LoginRequest, Storyboard, StoryboardEdit, ArtworkApproval, validate_storyboard
from .estimates import quote, revision
from .provider import OpenAIProvider
from .store import Store
from .worker import Worker
from .layouts import panel_layout


def create_app(directory=None, public_url=None, provider=None):
    store = Store(directory or os.getenv("COMIC_DATA_DIR", "./data"))
    provider = provider or OpenAIProvider()
    public_url = (public_url or os.getenv("COMIC_PUBLIC_URL", "")).rstrip("/")
    worker = Worker(store, provider)
    upload_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        worker.start()
        yield
        worker.close()
        app.state.print_executor.shutdown(wait=False,cancel_futures=True)

    app = FastAPI(title="ComicAI Forge", version="1.3.0", lifespan=lifespan)
    app.state.store = store
    app.state.worker = worker

    def token(authorization: str = Header(default="")):
        if not authorization.startswith("Bearer ") or not store.user_for_token(authorization[7:]):
            raise HTTPException(401, "Invalid or expired session", headers={"WWW-Authenticate": "Bearer"})
        return authorization[7:]

    def user(session=Depends(token)):
        username = store.user_for_token(session)
        if username is None:
            raise HTTPException(401, "Session expired")
        return username

    def owned(job_id, username):
        job = store.job(str(job_id), username)
        if job is None:
            raise HTTPException(404, "Job not found")
        return job

    def result(job):
        board = Storyboard.model_validate_json(job["plan"])
        expires = int(time.time()) + 86400 * 7
        pages = []
        request = GenerationRequest.model_validate_json(job["payload"])
        number = 0
        for page_index, page in enumerate(board.pages):
            panels = []
            bounds = panel_layout(len(page.panels), request.layoutStyle)
            for index, panel in enumerate(page.panels):
                signature = store.image_signature(job["id"], number, expires)
                analysis_file=store.assets/job["id"]/f"{number}.analysis.json"
                analysis=json.loads(analysis_file.read_text()) if analysis_file.is_file() else {}
                dialogues = []
                def add_line(text, speaker, bubble_type, y, trigger="TAP_TO_NEXT", delay=0, slot=0):
                    order = len(dialogues)
                    suggested=(analysis.get("balloons") or [None,None])
                    placement=suggested[slot] if len(suggested)>slot and suggested[slot] else {}
                    x=placement.get("x",.05); y=placement.get("y",y)
                    dialogues.append({"id": f"{job['id']}-{number}-{order}",
                        "speaker": {"speakerId": hashlib.sha256(speaker.casefold().encode()).hexdigest()[:24], "name": speaker},
                        "textManifest": {"primaryLine": text, "expandedNarration": panel.description},
                        "telemetry": {"order": order, "layoutStyle": bubble_type, "xRatio": x, "yRatio": y, "autoPlace": bool(placement), "widthRatio": placement.get("width",.72)},
                        "advanceCues": {"triggerType": trigger, "autoDelayMs": delay, "stepIndex": order}})
                if panel.caption.strip():
                    add_line(panel.caption, "Narrator", "THOUGHT", 0.04)
                if panel.dialogue.strip():
                    speaker, separator, line = panel.dialogue.partition(":")
                    add_line(line.strip() if separator else panel.dialogue, speaker.strip() if separator else "Speaker",
                        panel.bubbleType, 0.65, panel.advanceTrigger, panel.durationMillis, slot=1)
                panels.append({"panelIndex": index, "description": panel.description, "dialogues": dialogues,
                    "imageUrl": f"{public_url}/v1/images/{job['id']}/{number}.png?expires={expires}&signature={signature}",
                    "boundingBox": bounds[index], "protectedRegions": analysis.get("protectedRegions",[]),
                    "qualityNotes": ([analysis.get("notice","")] if analysis.get("reviewRequired") else []) +
                        [f"{c['character']}: {c['status']}" for c in analysis.get("characterChecks",[])]})
                number += 1
            pages.append({"pageNumber": page_index + 1, "panels": panels})
        request = GenerationRequest.model_validate_json(job["payload"])
        attachments = [{**uploads.metadata(store.upload(str(ref.id), job["username"])),
            "label": ref.label, "notes": ref.notes, "role": ref.role, "useForAi": ref.useForAi} for ref in request.attachments]
        return {"projectId": job["id"], "totalPages": len(pages), "targetAspectRatio": 595 / 842,
            "parsedPages": pages, "attachments": attachments}

    def summary(job, include_result=False):
        data = {"jobId": job["id"], "status": job["status"],
            "completedPanels": job["completed"], "totalPanels": job["total"], "error": job["error"],
            "title": json.loads(job["payload"])["rawScriptInput"][:60]}
        request = GenerationRequest.model_validate_json(job["payload"])
        if include_result and job["plan"] and request.requireArtworkApproval:
            board = Storyboard.model_validate_json(job["plan"])
            data.update(storyboard=board.model_dump(), revision=revision(job["plan"]), estimate=quote(request,board,provider,job["completed"]))
        if include_result and job["status"] == "completed":
            data["result"] = result(job)
        return data

    @app.get("/health")
    def health():
        return {"status": "ok", "generationConfigured": provider.configured}

    @app.get("/v1/capabilities")
    def capabilities():
        return {"serverVersion": "1.3.0", "storyboardApproval": True, "autoPanelCount": True, "styleReferences": True, "cmykPrintExport": True, "characterVision": True, "panelsPerPage": [3, 4, 5, 6],
                "layouts": ["GRID", "CINEMATIC"], "maximumPages": 35}

    @app.get("/v1/mobile-release")
    def mobile_release():
        # Advance only after verifying the APK's signature matches the published app.
        return {"versionCode": 2, "versionName": "0.2.0",
                "downloadUrl": "https://chatgpt.com/api/library/files/libfile_d9397f8f3fa881919a152912b1128a1a/download"}

    @app.post("/v1/login")
    def login(request: LoginRequest):
        try:
            session = store.login(request.username, request.password)
        except PermissionError as exc:
            raise HTTPException(429, str(exc)) from exc
        if not session:
            raise HTTPException(401, "Incorrect username or password")
        return session

    @app.post("/v1/logout", status_code=204)
    def logout(session=Depends(token)):
        store.logout(session)
        return Response(status_code=204)


    @app.put("/v1/uploads/{upload_id}")
    async def upload(upload_id: UUID, request: Request, filename: str = Query(min_length=1, max_length=180),
                     username=Depends(user), x_content_sha256: str = Header(default="")):
        if not re.fullmatch(r"[a-f0-9]{64}", x_content_sha256):
            raise HTTPException(400, "Supply the file SHA-256 checksum")
        try:
            if int(request.headers.get("content-length", "0")) > uploads.MAX_FILE_BYTES:
                raise HTTPException(413, "Each file must be 50 MB or smaller")
        except ValueError:
            raise HTTPException(400, "Invalid content length")
        name = filename.replace("\\", "/").rsplit("/", 1)[-1]
        name = "".join(c for c in name if ord(c) >= 32 and ord(c) != 127).strip() or "attachment"
        mime = request.headers.get("content-type", "application/octet-stream").split(";", 1)[0].lower()
        if not re.fullmatch(r"[\w.+-]+/[\w.+-]+", mime, flags=re.ASCII):
            mime = "application/octet-stream"
        async with upload_lock:
            existing = store.upload(str(upload_id), username)
            if existing:
                if existing["sha256"] != x_content_sha256 or existing["name"] != name or existing["mime"] != mime:
                    raise HTTPException(409, "This upload ID already identifies a different file")
                if (store.uploads / existing["disk_name"]).is_file():
                    return uploads.metadata(existing)
            temporary = store.uploads / (secrets.token_hex(24) + ".part")
            disk_name = str(upload_id) + ".bin"
            destination = store.uploads / disk_name
            try:
                digest = hashlib.sha256(); size = 0
                with temporary.open("xb") as output:
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > uploads.MAX_FILE_BYTES:
                            raise HTTPException(413, "Each file must be 50 MB or smaller")
                        digest.update(chunk); output.write(chunk)
                if digest.hexdigest() != x_content_sha256:
                    raise HTTPException(422, "File checksum did not match; retry the upload")
                kind, _, notice = await run_in_threadpool(uploads.inspect_file, temporary, name, mime)
                with store.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    other = db.execute("SELECT * FROM uploads WHERE id=?", (str(upload_id),)).fetchone()
                    if other and other["username"] != username:
                        raise HTTPException(409, "Use a new upload ID")
                    used = db.execute("SELECT COALESCE(SUM(size),0) FROM uploads WHERE username=? AND id!=?", (username, str(upload_id))).fetchone()[0]
                    count = db.execute("SELECT COUNT(*) FROM uploads WHERE username=? AND id!=?", (username, str(upload_id))).fetchone()[0]
                    if used + size > uploads.MAX_USER_BYTES or count >= 200:
                        raise HTTPException(413, "Server attachment storage quota reached (1 GB or 200 files per account)")
                    temporary.replace(destination)
                    try:
                        db.execute("INSERT OR REPLACE INTO uploads VALUES (?,?,?,?,?,?,?,?,?)",
                            (str(upload_id), username, name, mime, size, x_content_sha256, disk_name, kind, notice))
                    except Exception:
                        destination.unlink(missing_ok=True)
                        raise
                return uploads.metadata(store.upload(str(upload_id), username))
            finally:
                temporary.unlink(missing_ok=True)

    @app.get("/v1/uploads/{upload_id}")
    def upload_metadata(upload_id: UUID, username=Depends(user)):
        row = store.upload(str(upload_id), username)
        if row is None:
            raise HTTPException(404, "File not found")
        return uploads.metadata(row)

    @app.get("/v1/uploads/{upload_id}/content")
    def upload_content(upload_id: UUID, username=Depends(user)):
        row = store.upload(str(upload_id), username)
        if row is None or not (store.uploads / row["disk_name"]).is_file():
            raise HTTPException(404, "File not found")
        return FileResponse(store.uploads / row["disk_name"], filename=row["name"], media_type="application/octet-stream",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.delete("/v1/uploads/{upload_id}", status_code=204)
    async def delete_upload(upload_id: UUID, username=Depends(user)):
        async with upload_lock:
            with store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM uploads WHERE id=? AND username=?", (str(upload_id), username)).fetchone()
                if row is None:
                    raise HTTPException(404, "File not found")
                for job in db.execute("SELECT payload FROM jobs WHERE username=?", (username,)):
                    if any(str(ref.id) == str(upload_id) for ref in GenerationRequest.model_validate_json(job["payload"]).attachments):
                        raise HTTPException(409, "This file is retained by a generation job")
                db.execute("DELETE FROM uploads WHERE id=?", (str(upload_id),))
                (store.uploads / row["disk_name"]).unlink(missing_ok=True)
        return Response(status_code=204)

    @app.post("/v1/jobs", status_code=202)
    def create_job(request: GenerationRequest, username=Depends(user), idempotency_key: str = Header(default="")):
        if idempotency_key != str(request.requestId):
            raise HTTPException(400, "Idempotency-Key must match requestId")
        if not provider.configured:
            raise HTTPException(503, "OPENAI_API_KEY has not been configured on the server")
        if not public_url.startswith("https://"):
            raise HTTPException(503, "COMIC_PUBLIC_URL must be the deployed HTTPS base URL")
        try:
            for ref in request.attachments:
                if store.upload(str(ref.id), username) is None:
                    raise HTTPException(404, "An attachment is missing or belongs to another account")
                if ref.useForAi and ref.role == "CHARACTER" and not ref.label.strip():
                    raise HTTPException(422, "Name each character reference photo before generating")
            uploads.references(store, username, request.attachments)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        try:
            job = store.create_job(username, request)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(429, str(exc)) from exc
        worker.wake.set()
        return summary(job, True)

    @app.get("/v1/jobs")
    def jobs(username=Depends(user)):
        with store.connect() as db:
            records = db.execute("SELECT * FROM jobs WHERE username=? ORDER BY created DESC LIMIT 50", (username,)).fetchall()
        return {"jobs": [summary(dict(record)) for record in records]}

    @app.get("/v1/jobs/{job_id}")
    def get_job(job_id: UUID, username=Depends(user)):
        return summary(owned(job_id, username), True)

    @app.post("/v1/jobs/{job_id}/storyboard")
    def edit_storyboard(job_id: UUID, body: StoryboardEdit, username=Depends(user)):
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=? AND username=?", (str(job_id),username)).fetchone()
            if not row:
                raise HTTPException(404,"Job not found")
            job=dict(row)
            if job["status"] != "awaiting_approval" or not job["plan"] or job["completed"]:
                raise HTTPException(409,"Only an unstarted storyboard waiting for approval can be edited")
            if revision(job["plan"]) != body.expectedRevision:
                raise HTTPException(409,"Storyboard changed. Refresh before saving")
            request=GenerationRequest.model_validate_json(job["payload"])
            try:
                validate_storyboard(body.storyboard,request.targetPageCount,request.panelsPerPage,request.autoPanelCount)
            except ValueError as exc:
                raise HTTPException(422,str(exc)) from exc
            db.execute("DELETE FROM artwork_approvals WHERE job_id=?",(str(job_id),))
            db.execute("UPDATE jobs SET plan=?, total=? WHERE id=?",(body.storyboard.model_dump_json(),sum(len(p.panels) for p in body.storyboard.pages),str(job_id)))
        return summary(owned(job_id,username),True)

    @app.post("/v1/jobs/{job_id}/approve")
    def approve_artwork(job_id: UUID, body: ArtworkApproval, username=Depends(user)):
        from decimal import Decimal
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row=db.execute("SELECT * FROM jobs WHERE id=? AND username=?",(str(job_id),username)).fetchone()
            if not row:
                raise HTTPException(404,"Job not found")
            job=dict(row)
            if not job["plan"] or revision(job["plan"]) != body.revision:
                raise HTTPException(409,"Storyboard changed. Review the new estimate")
            existing=db.execute("SELECT * FROM artwork_approvals WHERE job_id=?",(str(job_id),)).fetchone()
            if existing and existing["revision"]==body.revision and job["status"] in {"queued","running","completed"}:
                return summary(job,True)
            if job["status"] != "awaiting_approval":
                raise HTTPException(409,"This job is not awaiting artwork approval")
            request=GenerationRequest.model_validate_json(job["payload"])
            estimate=quote(request,Storyboard.model_validate_json(job["plan"]),provider,job["completed"])
            if not estimate["available"]:
                raise HTTPException(422,estimate["reason"])
            if Decimal(body.approvedEstimateHigh)<Decimal(estimate["estimatedHigh"]):
                raise HTTPException(422,"Approved estimate is below the current estimate")
            active=db.execute("SELECT COUNT(*) FROM jobs WHERE username=? AND status IN ('queued','running')",(username,)).fetchone()[0]
            if active>=3:
                raise HTTPException(429,"Three generation jobs are already active")
            db.execute("INSERT OR REPLACE INTO artwork_approvals VALUES (?,?,?,?)",(str(job_id),body.revision,json.dumps(estimate),time.time()))
            db.execute("UPDATE jobs SET status='queued', error=NULL WHERE id=?",(str(job_id),))
        worker.wake.set()
        return summary(owned(job_id,username),True)

    from pydantic import BaseModel, Field
    class FaceReviewRequest(BaseModel):
        label: str = Field(min_length=1,max_length=100)
        faceIndex: int = Field(ge=0,le=11,strict=True)
    class FaceAnalysisRequest(BaseModel):
        compareFaces: bool = False

    @app.get("/v1/jobs/{job_id}/faces/{index}")
    def get_face_review(job_id: UUID,index:int,username=Depends(user)):
        owned(job_id,username)
        if index<0 or index>210:raise HTTPException(404,"Panel not found")
        path=store.assets/str(job_id)/f"{index}.analysis.json"
        return json.loads(path.read_text()) if path.is_file() else {"protectedRegions":[],"characterChecks":[],"characterNames":[],"notice":"Checks have not been run for this panel.","reviewRequired":True}

    @app.post("/v1/jobs/{job_id}/faces/{index}/analyze")
    async def run_face_review(job_id:UUID,index:int,body:FaceAnalysisRequest,username=Depends(user)):
        job=owned(job_id,username)
        if job["status"]!="completed":raise HTTPException(409,"Wait for completed artwork before analysis")
        board=Storyboard.model_validate_json(job["plan"]);panels=[p for page in board.pages for p in page.panels]
        if not 0<=index<len(panels):raise HTTPException(404,"Panel not found")
        request=GenerationRequest.model_validate_json(job["payload"])
        from .vision import CharacterVision
        if store.resource_lock.locked():raise HTTPException(409,"Wait for generation or print conversion before manual face analysis")
        # Manual checks own their detector; the worker owns a separate stateful network.
        async with upload_lock:
            def analyze():
                with store.resource_lock:
                    return analyze_locked()
            def analyze_locked():
                vision=CharacterVision(store)
                result=vision.analyze((store.assets/str(job_id)/f"{index}.png").read_bytes(),panels[index],username,request.attachments,body.compareFaces,str(job_id),index)
                target=store.assets/str(job_id)/f"{index}.analysis.json";temporary=target.with_suffix(".review.tmp");temporary.write_text(json.dumps(result));temporary.replace(target)
                return result
            return await run_in_threadpool(analyze)

    @app.post("/v1/jobs/{job_id}/faces/{index}/approve")
    def approve_face_review(job_id:UUID,index:int,body:FaceReviewRequest,username=Depends(user)):
        job=owned(job_id,username);request=GenerationRequest.model_validate_json(job["payload"])
        names={r.label for r in request.attachments if r.useForAi and r.role=="CHARACTER"}
        if body.label not in names:raise HTTPException(422,"Choose a character named in this job's references")
        with store.connect() as db:
            exists=db.execute("SELECT 1 FROM sqlite_master WHERE name='generated_face_embeddings'").fetchone()
            row=db.execute("SELECT * FROM generated_face_embeddings WHERE username=? AND job_id=? AND panel_index=? AND face_index=?",(username,str(job_id),index,body.faceIndex)).fetchone() if exists else None
            if not row:raise HTTPException(422,"Run the opt-in face comparison before approving a detected face")
            db.execute("INSERT OR REPLACE INTO approved_character_faces VALUES (?,?,?,?,?,?)",(username,body.label,row["model"],row["vector"],str(job_id),body.faceIndex))
        return {"status":"approved_by_user","character":body.label,"notice":"Saved as your approved reference for future similarity review; this is your judgment, not a model verification."}

    @app.delete("/v1/character-embeddings",status_code=204)
    def delete_embeddings(username=Depends(user)):
        with store.connect() as db:
            found=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='character_embeddings'").fetchone()
            if found:
                db.execute("DELETE FROM character_embeddings WHERE username=?",(username,))
            for table in ("generated_face_embeddings","approved_character_faces"):
                if db.execute("SELECT 1 FROM sqlite_master WHERE name=?",(table,)).fetchone():
                    db.execute("DELETE FROM "+table+" WHERE username=?",(username,))
        return Response(status_code=204)

    @app.get("/v1/storage")
    def storage(username=Depends(user)):
        import shutil
        disk=shutil.disk_usage(store.directory)
        with store.connect() as db:
            uploads_bytes=db.execute("SELECT COALESCE(SUM(size),0) FROM uploads WHERE username=?",(username,)).fetchone()[0]
        return {"diskTotalBytes":disk.total,"diskUsedBytes":disk.used,"diskFreeBytes":disk.free,
                "accountUploadBytes":uploads_bytes,"accountUploadQuotaBytes":uploads.MAX_USER_BYTES,
                "checkedAt":int(time.time()),"creditBalance":None,
                "notice":"Server disk usage is separate from AI credits. No live provider credit balance is available through this API."}

    @app.post("/v1/jobs/{job_id}/cancel")
    def cancel(job_id: UUID, username=Depends(user)):
        owned(job_id, username)
        store.cancel(str(job_id), username)
        return summary(owned(job_id, username))

    @app.post("/v1/jobs/{job_id}/resume")
    def resume(job_id: UUID, username=Depends(user)):
        owned(job_id, username)
        if not provider.configured:
            raise HTTPException(503, "AI provider is not configured")
        try:
            store.resume(str(job_id), username)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(429, str(exc)) from exc
        worker.wake.set()
        return summary(owned(job_id, username))

    @app.get("/v1/images/{job_id}/{index}.png")
    def image(job_id: UUID, index: int, expires: int = Query(), signature: str = Query()):
        if expires < time.time() or not hmac.compare_digest(signature, store.image_signature(str(job_id), index, expires)):
            raise HTTPException(403, "Image link is invalid or expired")
        if index < 0:
            raise HTTPException(404, "Image not found")
        file = store.assets / str(job_id) / f"{index}.png"
        if not file.is_file():
            raise HTTPException(404, "Image not found")
        return FileResponse(file, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})

    from .print_routes import register_print_routes
    register_print_routes(app,store,user)
    return app

