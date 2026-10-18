import json
import shutil
import threading

from .media import Cancelled, render


class Worker:
    def __init__(self, projects):
        self.projects = projects
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.loop, name="moment-render", daemon=False)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join()

    def loop(self):
        while not self.stop_event.is_set():
            if not self.run_once():
                self.stop_event.wait(.3)

    def run_once(self):
        job = self.projects.claim()
        if job is None:
            return False
        work = self.projects.directory(job["project"]) / "attempts" / job["token"]
        active = lambda: not self.stop_event.is_set() and self.projects.active(job)
        try:
            snapshot = json.loads(job["snapshot"])
            paths = {asset["id"]: self.projects.checked_source(job["project"], asset) for asset in snapshot["assets"]}
            target = render(snapshot, paths, work, self.projects.config.ffmpeg, active,
                            lambda progress: self.projects.active(job, progress), self.projects.config.render_seconds)
            if not self.projects.complete(job, target):
                raise Cancelled()
        except Cancelled:
            self.projects.fail(job, cancelled=True)
        except Exception:
            self.projects.fail(job, cancelled=not active())
        finally:
            if work.exists():
                shutil.rmtree(work)
        return True
