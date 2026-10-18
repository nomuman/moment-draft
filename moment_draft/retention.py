import logging
import threading


class Retention:
    def __init__(self, projects, interval=60):
        self.projects = projects
        self.interval = interval
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.run, name="moment-retention", daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopped.set()
        self.thread.join()

    def run(self):
        while not self.stopped.is_set():
            try:
                self.projects.cleanup(dry_run=False)
            except Exception as exc:
                logging.getLogger(__name__).error("retention failed: %s", type(exc).__name__)
            self.stopped.wait(self.interval)
