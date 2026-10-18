import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from waitress import create_server
from waitress.task import ThreadedTaskDispatcher

from moment_draft.__main__ import main, stop_service

from moment_draft.config import Config
from moment_draft.projects import Problem, Projects
from moment_draft.retention import Retention


class RetentionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.now = [100]
        self.projects = Projects(Config(Path(directory.name), retention_seconds=1, free_floor=0), clock=lambda: self.now[0])
        self.addCleanup(self.projects.close)
        _, self.session = self.projects.new_session()
        self.project = self.projects.create(self.session["owner"])["id"]
        self.asset = self.projects.register(self.session["owner"], self.project, "test.jpg", 4)
        self.path = self.projects.asset_path(self.project, self.asset["id"])
        self.path.write_bytes(b"test")

    def test_background_cleanup_reclaims_only_expired_owned_copies(self):
        outside = self.projects.root / "original.jpg"
        outside.write_bytes(b"original")
        self.now[0] = 102
        with self.assertRaises(Problem):
            self.projects.view(self.session["owner"], self.project)
        self.assertEqual(self.projects.cleanup(dry_run=True), [self.project])
        self.assertTrue(self.path.exists())
        done = threading.Event()
        cleanup = self.projects.cleanup
        def observed_cleanup(**kwargs):
            result = cleanup(**kwargs)
            done.set()
            return result
        self.projects.cleanup = observed_cleanup
        retention = Retention(self.projects)
        retention.start()
        self.assertTrue(done.wait(2))
        retention.stop()
        self.assertFalse(self.path.exists())
        self.assertEqual(outside.read_bytes(), b"original")
        self.assertFalse(retention.thread.is_alive())

    def test_verification_release_precedes_removal(self):
        with self.projects.transaction() as db:
            db.execute("UPDATE assets SET state=? WHERE id=?", ("verifying", self.asset["id"]))
        self.now[0] = 102
        self.projects.cleanup(dry_run=False)
        self.assertTrue(self.path.exists())
        with self.projects.transaction() as db:
            db.execute("UPDATE assets SET state=? WHERE id=?", ("failed", self.asset["id"]))
        self.projects.cleanup(dry_run=False)
        self.assertFalse(self.path.exists())

    def test_stop_waits_for_current_cleanup_before_database_close(self):
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        def cleanup(**kwargs):
            entered.set()
            release.wait(2)
        retention = Retention(Mock(cleanup=cleanup))
        retention.start()
        self.assertTrue(entered.wait(2))
        stopper = threading.Thread(target=lambda: (retention.stop(), stopped.set()))
        stopper.start()
        self.assertFalse(stopped.wait(.05))
        release.set()
        stopper.join(2)
        self.assertTrue(stopped.is_set())

    def test_cleanup_failure_does_not_log_paths_or_stop_future_passes(self):
        recovered = threading.Event()
        calls = [0]
        def cleanup(**kwargs):
            calls[0] += 1
            if calls[0] == 1:
                raise OSError("private-name-and-secret")
            recovered.set()
        retention = Retention(Mock(cleanup=cleanup), interval=.01)
        with self.assertLogs("moment_draft.retention", level="ERROR") as captured:
            retention.start()
            self.assertTrue(recovered.wait(2))
            retention.stop()
        self.assertNotIn("private-name-and-secret", str(captured.output))
        self.assertIn("OSError", str(captured.output))


class ShutdownTests(unittest.TestCase):
    def test_repeated_signal_finishes_cleanup_before_storage_release(self):
        for first_signal in (signal.SIGINT, signal.SIGTERM):
            for next_signal in (signal.SIGINT, signal.SIGTERM):
                with self.subTest(first=first_signal, following=next_signal), tempfile.TemporaryDirectory() as directory:
                    config = Config(Path(directory), free_floor=0)
                    projects = Projects(config)
                    entered, release = threading.Event(), threading.Event()
                    native = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read()"], stdin=subprocess.PIPE, pass_fds=(projects.volume_lock.fileno(),))
                    dispatcher = ThreadedTaskDispatcher()
                    dispatcher.set_thread_count(1)
                    def service():
                        entered.set()
                        release.wait(3)
                        native.stdin.close()
                        native.wait(timeout=2)
                    dispatcher.add_task(Mock(service=service))
                    self.assertTrue(entered.wait(2))
                    server = create_server(lambda environ, start_response: [], host="127.0.0.1", port=0, _dispatcher=dispatcher)
                    handlers = {}
                    def cleanup():
                        self.assertIsNone(native.poll())
                        self.assertFalse(projects.volume_lock.closed)
                        projects.db.execute("SELECT 1")
                        with self.assertRaises(RuntimeError):
                            Projects(config)
                        handlers[next_signal](next_signal, None)
                        release.set()
                    retention = Mock(stop=Mock(side_effect=cleanup))
                    try:
                        with (patch("sys.argv", ["moment_draft"]),
                              patch("moment_draft.__main__.os.environ", {}),
                              patch("moment_draft.__main__.Config.environment", return_value=config),
                              patch("moment_draft.__main__.Projects", return_value=projects),
                              patch("moment_draft.__main__.create_server", return_value=server),
                              patch("moment_draft.__main__.Worker"),
                              patch("moment_draft.__main__.Retention", return_value=retention),
                              patch("moment_draft.__main__.signal.signal", side_effect=lambda signum, handler: handlers.update({signum: handler})),
                              patch.object(server, "run", side_effect=lambda: handlers[first_signal](first_signal, None))):
                            main()
                        self.assertFalse(dispatcher.threads)
                        self.assertEqual(native.poll(), 0)
                        self.assertTrue(projects.volume_lock.closed)
                        reopened = Projects(config)
                        reopened.close()
                    finally:
                        server.close()
                        release.set()
                        dispatcher.shutdown(timeout=3)
                        if native.poll() is None:
                            native.kill()
                            native.wait(timeout=2)
                        if not projects.volume_lock.closed:
                            projects.close()

    def test_request_and_native_lease_hold_storage_until_actual_release(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(Path(directory), free_floor=0)
            projects = Projects(config)
            entered, release = threading.Event(), threading.Event()
            native = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read()"], stdin=subprocess.PIPE, pass_fds=(projects.volume_lock.fileno(),))
            dispatcher = ThreadedTaskDispatcher()
            dispatcher.set_thread_count(1)
            def service():
                entered.set()
                release.wait(3)
                native.stdin.close()
                native.wait(timeout=2)
            dispatcher.add_task(Mock(service=service))
            self.assertTrue(entered.wait(2))
            server = create_server(lambda environ, start_response: [], host="127.0.0.1", port=0, _dispatcher=dispatcher)
            try:
                with self.assertRaises(RuntimeError):
                    stop_service(server, Mock(), Mock(), projects, timeout=.01)
                self.assertIsNone(native.poll())
                self.assertFalse(projects.volume_lock.closed)
                projects.db.execute("SELECT 1")
                with self.assertRaises(RuntimeError):
                    Projects(config)
                release.set()
                dispatcher.shutdown(timeout=3)
                self.assertFalse(dispatcher.threads)
                self.assertEqual(native.poll(), 0)
                stop_service(server, Mock(), Mock(), projects, timeout=.01)
                self.assertTrue(projects.volume_lock.closed)
                reopened = Projects(config)
                reopened.close()
            finally:
                server.close()
                release.set()
                dispatcher.shutdown(timeout=3)
                if native.poll() is None:
                    native.kill()
                    native.wait(timeout=2)
                if not projects.volume_lock.closed:
                    projects.close()
