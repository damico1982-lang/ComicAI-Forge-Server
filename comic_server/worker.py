import json
import threading
import fcntl
from pathlib import Path

from .models import GenerationRequest, Storyboard
from .provider import ProviderError
from .uploads import references


class Worker:
    def __init__(self, store, provider):
        self.store, self.provider = store, provider
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
        job_id = job["id"]
        folder = self.store.assets / job_id
        folder.mkdir(exist_ok=True)
        try:
            request = GenerationRequest.model_validate_json(job["payload"])
            reference_files = references(self.store, job["username"], request.attachments)
            if job["plan"]:
                board = Storyboard.model_validate_json(job["plan"])
            else:
                board = self.provider.plan(request, references=reference_files) if reference_files else self.provider.plan(request)
                self.store.update(job_id, plan=board.model_dump_json())
            number = 0
            for page in board.pages:
                for panel in page.panels:
                    if self.stop.is_set():
                        self.store.update(job_id, status="interrupted", error="Server stopped. Resume to continue from saved panels.")
                        return
                    if self.store.job(job_id)["status"] != "running":
                        return
                    destination = folder / f"{number}.png"
                    if not destination.exists():
                        content = self.provider.image(panel, board.characterBible, request.selectedArtStyle, references=reference_files) if reference_files else self.provider.image(panel, board.characterBible, request.selectedArtStyle)
                        temporary = destination.with_suffix(".tmp")
                        temporary.write_bytes(content)
                        temporary.replace(destination)
                    number += 1
                    self.store.update(job_id, completed=number)
            if self.store.job(job_id)["status"] == "running":
                self.store.update(job_id, status="completed", error=None)
        except ProviderError as exc:
            if self.store.job(job_id)["status"] == "running":
                self.store.update(job_id, status="failed", error=str(exc))
        except Exception:
            if self.store.job(job_id)["status"] == "running":
                self.store.update(job_id, status="failed", error="Generation failed. Check server logs and available storage before resuming.")
