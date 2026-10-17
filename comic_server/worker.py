import json
import threading
import logging
import fcntl
from pathlib import Path

from .models import GenerationRequest, Storyboard, validate_storyboard
from .provider import ProviderError, OpenAIProvider
from .uploads import references
from .layouts import panel_layout, panel_aspect_ratio
from .estimates import revision


class Worker:
    def __init__(self, store, provider):
        self.store, self.provider = store, provider
        self.vision = None
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = threading.Thread(target=self.run, name="comic-generation", daemon=True)

    def start(self):
        self.lock = (self.store.directory / "worker.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError("Only one generation server process may use a data directory.")
        self.store.interrupt_running()
        self.thread.start()

    def close(self):
        self.stop.set()
        self.wake.set()
        self.thread.join(timeout=5)
        if not self.thread.is_alive():
            self.lock.close()

    def run(self):
        while not self.stop.is_set():
            job = self.store.claim()
            if job is None:
                self.wake.wait(timeout=1)
                self.wake.clear()
                continue
            self.process(job)

    def process(self, job):
        with self.store.resource_lock:
            return self._process(job)

    def _process(self, job):
        job_id = job["id"]
        folder = self.store.assets / job_id
        folder.mkdir(exist_ok=True)
        try:
            request = GenerationRequest.model_validate_json(job["payload"])
            reference_files = references(self.store, job["username"], request.attachments)
            if job["plan"]:
                board = Storyboard.model_validate_json(job["plan"])
            else:
                if isinstance(self.provider, OpenAIProvider):
                    board = self.provider.plan(request, references=reference_files, checkpoint_path=folder / "storyboard.checkpoint.json")
                else:
                    board = self.provider.plan(request, references=reference_files) if reference_files else self.provider.plan(request)
                validate_storyboard(board, request.targetPageCount, request.panelsPerPage, request.autoPanelCount)
                self.store.update(job_id, plan=board.model_dump_json())
            validate_storyboard(board, request.targetPageCount, request.panelsPerPage, request.autoPanelCount)
            total = sum(len(page.panels) for page in board.pages)
            self.store.update(job_id, total=total)
            if request.requireArtworkApproval:
                approval = self.store.approval(job_id)
                if not approval or approval["revision"] != revision(board.model_dump_json()):
                    # A cancelled planning job must never become actionable again.
                    with self.store.connect() as db:
                        db.execute("UPDATE jobs SET status='awaiting_approval', error=NULL WHERE id=? AND status='running'", (job_id,))
                    return
            number = 0
            for page in board.pages:
                bounds = panel_layout(len(page.panels), request.layoutStyle)
                for panel_index, panel in enumerate(page.panels):
                    if self.stop.is_set():
                        self._transition(job_id,"interrupted","Server stopped. Resume to continue from saved panels.")
                        return
                    if self.store.job(job_id)["status"] != "running":
                        return
                    destination = folder / f"{number}.png"
                    if not destination.exists():
                        box = bounds[panel_index]
                        ratio = panel_aspect_ratio(box)
                        content = self.provider.image(panel, board.characterBible, request.selectedArtStyle,
                                                      references=reference_files, aspect_ratio=ratio)
                        temporary = destination.with_suffix(".tmp")
                        temporary.write_bytes(content)
                        temporary.replace(destination)
                    if request.analyzeBalloonSpace or request.compareCharacterFaces:
                        analysis_file=folder / f"{number}.analysis.json"
                        if not analysis_file.exists():
                            try:
                                from .vision import CharacterVision
                                if self.vision is None:
                                    self.vision=CharacterVision(self.store)
                                analysis=self.vision.analyze(destination.read_bytes(),panel,job["username"],request.attachments,request.compareCharacterFaces,job_id,number)
                            except Exception:
                                analysis={"protectedRegions":[],"balloons":[],"characterChecks":[],"reviewRequired":True,"notice":"Vision analysis was unavailable. Review faces and lettering manually."}
                            tmp=analysis_file.with_suffix(".tmp");tmp.write_text(json.dumps(analysis));tmp.replace(analysis_file)
                    number += 1
                    self.store.update(job_id, completed=number)
            self._transition(job_id,"completed",None)
        except ProviderError as exc:
            self._transition(job_id,"failed",str(exc))
        except Exception as exc:
            # Log the category without script text, credentials or provider bodies.
            logging.getLogger(__name__).error("Generation job %s failed (%s)",job_id,type(exc).__name__)
            self._transition(job_id,"failed","Generation failed. Check server logs and available storage before resuming.")

    def _transition(self,job_id,status,error):
        # Cancellation may happen while a provider request is finishing.
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET status=?,error=? WHERE id=? AND status='running'",(status,error,job_id))

