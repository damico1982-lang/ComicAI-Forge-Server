import json
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

from fastapi import Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import Field

from .models import StrictModel
from .print_export import cmyk_pdf


class PrintRequest(StrictModel):
    requestId: UUID
    pages: list[UUID] = Field(min_length=1,max_length=35)
    iccProfile: UUID
    dpi: int = Field(default=300,ge=300,le=600,strict=True)
    bleedMm: float = Field(default=0,ge=0,le=6)
    renderingIntent: int = Field(default=1,ge=0,le=3,strict=True)


def register_print_routes(app,store,user):
    directory=store.directory/"print-exports";directory.mkdir(exist_ok=True)
    executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix="print-export")
    with store.connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS print_exports (id TEXT PRIMARY KEY, username TEXT NOT NULL, request_id TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, error TEXT, report TEXT, created REAL NOT NULL, UNIQUE(username,request_id))")
        db.execute("UPDATE print_exports SET status='interrupted',error='Server restarted during export; submit a new export from the saved book.' WHERE status IN ('queued','running')")

    def owned(export_id,username):
        with store.connect() as db:row=db.execute("SELECT * FROM print_exports WHERE id=? AND username=?",(str(export_id),username)).fetchone()
        if not row:raise HTTPException(404,"Print export not found")
        return dict(row)

    def summary(row):
        return {"exportId":row["id"],"status":row["status"],"error":row["error"],"report":json.loads(row["report"]) if row["report"] else None}

    def run(export_id,username,request):
        with store.resource_lock:
            return convert(export_id,username,request)

    def convert(export_id,username,request):
        try:
            with store.connect() as db:db.execute("UPDATE print_exports SET status='running' WHERE id=?",(export_id,))
            sources=[]
            for page in request.pages:
                row=store.upload(str(page),username)
                if not row:raise ValueError("A print page is missing")
                sources.append(store.uploads/row["disk_name"])
            profile=store.upload(str(request.iccProfile),username)
            if not profile:raise ValueError("Printer ICC profile is missing")
            report=cmyk_pdf(sources,store.uploads/profile["disk_name"],directory/f"{export_id}.pdf",request.dpi,request.bleedMm,request.renderingIntent)
            with store.connect() as db:db.execute("UPDATE print_exports SET status='completed',report=?,error=NULL WHERE id=?",(json.dumps(report),export_id))
        except Exception as exc:
            message=str(exc) if isinstance(exc,ValueError) else "Print conversion failed. Check the selected profile, source pages and storage."
            with store.connect() as db:db.execute("UPDATE print_exports SET status='failed',error=? WHERE id=?",(message[:300],export_id))

    @app.post("/v1/print-exports",status_code=202)
    def start_print(request:PrintRequest,username=Depends(user)):
        if request.dpi not in (300,600):raise HTTPException(422,"Choose 300 or 600 DPI")
        if len(set(request.pages))!=len(request.pages):raise HTTPException(422,"Print page IDs must be distinct")
        payload=request.model_dump_json()
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing=db.execute("SELECT * FROM print_exports WHERE username=? AND request_id=?",(username,str(request.requestId))).fetchone()
            if existing:
                if existing["payload"]!=payload:raise HTTPException(409,"Print request ID was reused for different content")
                return summary(dict(existing))
            if any(db.execute("SELECT 1 FROM uploads WHERE id=? AND username=?",(str(i),username)).fetchone() is None for i in [*request.pages,request.iccProfile]):
                raise HTTPException(404,"A print page or ICC profile is missing")
            if db.execute("SELECT COUNT(*) FROM print_exports WHERE status IN ('queued','running')").fetchone()[0]>=2:raise HTTPException(429,"Two print exports are already active")
            export_id=str(UUID(bytes=secrets.token_bytes(16),version=4))
            db.execute("INSERT INTO print_exports VALUES (?,?,?,?,'queued',NULL,NULL,?)",(export_id,username,str(request.requestId),payload,time.time()))
        executor.submit(run,export_id,username,request)
        return summary(owned(export_id,username))

    @app.get("/v1/print-exports")
    def list_prints(username=Depends(user)):
        with store.connect() as db:rows=db.execute("SELECT * FROM print_exports WHERE username=? ORDER BY created DESC LIMIT 30",(username,)).fetchall()
        return {"exports":[summary(dict(r)) for r in rows]}

    @app.get("/v1/print-exports/{export_id}")
    def print_status(export_id:UUID,username=Depends(user)):
        return summary(owned(export_id,username))

    @app.get("/v1/print-exports/{export_id}/content")
    def print_content(export_id:UUID,username=Depends(user)):
        row=owned(export_id,username);file=directory/f"{export_id}.pdf"
        if row["status"]!="completed" or not file.is_file():raise HTTPException(409,"Print export is not complete")
        return FileResponse(file,media_type="application/pdf",filename="comic-print.pdf",headers={"Cache-Control":"no-store"})

    app.state.print_executor=executor
