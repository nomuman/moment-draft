import json
import logging
import shutil
import threading

from .media import Cancelled, render
from .music import ensure_track
from .thumbnails import create_thumbnail


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
        with self.projects.native_slot() as acquired:
            if not acquired:
                return False
            return self.run_native_work()

    def run_native_work(self):
        job = self.projects.claim()
        if job is None:
            return self.thumbnail_once()
        work = self.projects.directory(job["project"]) / "attempts" / job["token"]
        active = lambda: not self.stop_event.is_set() and self.projects.active(job)
        try:
            snapshot = json.loads(job["snapshot"])
            paths = {asset["id"]: self.projects.checked_source(job["project"], asset) for asset in snapshot["assets"]}
            music_path = ensure_track(snapshot["music"]["id"], self.projects.root / "music") if snapshot["music"]["id"] != "off" else None
            target = render(snapshot, paths, work, self.projects.config.ffmpeg, active,
                            lambda progress: self.projects.active(job, progress), self.projects.config.render_seconds,
                            music_path=music_path, lease_fd=self.projects.volume_lock.fileno())
            if not self.projects.complete(job, target):
                raise Cancelled()
        except Cancelled:
            self.projects.fail(job, cancelled=True)
        except Exception:
            self.projects.fail(job, cancelled=not active())
        finally:
            self.remove_work(work)
        return True

    def thumbnail_once(self):
        task = self.projects.claim_thumbnail()
        if task is None:
            return False
        work = self.projects.directory(task["project"]) / "attempts" / ("thumbnail-" + task["token"])
        active = lambda: not self.stop_event.is_set() and self.projects.thumbnail_active(task)
        try:
            source = self.projects.checked_source(task["project"], task["asset"])
            target = create_thumbnail(source, task["frame"], work, self.projects.config.ffmpeg, active,
                                      self.projects.config.probe_seconds, self.projects.volume_lock.fileno())
            self.projects.finish_thumbnail(task, target)
        except Exception:
            self.projects.finish_thumbnail(task)
        finally:
            self.remove_work(work)
        return True

    def remove_work(self, work):
        with self.projects.lock:
            try:
                if work.exists():
                    shutil.rmtree(work)
            except OSError as error:
                logging.getLogger(__name__).error("attempt cleanup failed: %s", type(error).__name__)
