import signal
from contextlib import nullcontext
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
from moment_draft.worker import Worker


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
    def assert_startup_cleanup(self, component, interrupt, error=None):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(Path(directory), free_floor=0)
            projects = Projects(config)
            worker, retention = Worker(projects), Retention(projects)
            server = create_server(lambda environ, start_response: [], host="127.0.0.1", port=0)
            listener = server.socket
            handlers = {}
            target = worker if component == "worker" else retention
            start = target.start
            try:
                with (patch("sys.argv", ["moment_draft"]),
                      patch("moment_draft.__main__.os.environ", {}),
                      patch("moment_draft.__main__.Config.environment", return_value=config),
                      patch("moment_draft.__main__.Projects", return_value=projects),
                      patch("moment_draft.__main__.create_server", return_value=server),
                      patch("moment_draft.__main__.Worker", return_value=worker),
                      patch("moment_draft.__main__.Retention", return_value=retention),
                      patch("moment_draft.__main__.signal.signal", side_effect=lambda signum, handler: handlers.update({signum: handler})),
                      patch.object(target, "start", side_effect=lambda: interrupt(start, handlers)),
                      patch.object(server, "run") as run):
                    with self.assertRaisesRegex(RuntimeError, error) if error else nullcontext():
                        main()
                run.assert_not_called()
                self.assertFalse(worker.thread and worker.thread.is_alive())
                self.assertFalse(retention.thread.is_alive())
                self.assertFalse(server.task_dispatcher.threads)
                self.assertEqual(listener.fileno(), -1)
                self.assertTrue(projects.volume_lock.closed)
                reopened = Projects(config)
                reopened.close()
            finally:
                worker.stop_event.set()
                retention.stopped.set()
                for thread in (worker.thread, retention.thread):
                    if thread is not None and thread.ident is not None:
                        thread.join(3)
                server.close()
                server.task_dispatcher.shutdown(timeout=3)
                if not projects.volume_lock.closed:
                    projects.close()

    def test_startup_signal_releases_threads_socket_and_storage(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            for component in ("worker", "retention"):
                for started in (False, True):
                    with self.subTest(signal=signum, component=component, started=started):
                        def interrupt(start, handlers):
                            if started:
                                start()
                            handlers[signum](signum, None)
                        self.assert_startup_cleanup(component, interrupt)

    def test_signal_during_native_thread_creation_waits_for_startup(self):
        start_new_thread = threading._start_new_thread
        for signum in (signal.SIGINT, signal.SIGTERM):
            for component in ("worker", "retention"):
                with self.subTest(signal=signum, component=component):
                    def interrupt(start, handlers):
                        release = threading.Event()
                        def launch(bootstrap, arguments):
                            thread = bootstrap.__self__
                            def delayed_bootstrap():
                                release.wait(3)
                                bootstrap(*arguments)
                            result = start_new_thread(delayed_bootstrap, ())
                            try:
                                self.assertIsNone(thread.ident)
                                handlers[signum](signum, None)
                                self.assertIsNone(thread.ident)
                            finally:
                                release.set()
                                self.assertTrue(thread._started.wait(3))
                            return result
                        with patch("threading._start_new_thread", side_effect=launch):
                            start()
                    self.assert_startup_cleanup(component, interrupt)

    def test_thread_start_failure_releases_started_components(self):
        for component in ("worker", "retention"):
            for started in (False, True):
                with self.subTest(component=component, started=started):
                    def interrupt(start, handlers):
                        if started:
                            start()
                            raise RuntimeError("startup failed")
                        with patch("threading._start_new_thread", side_effect=RuntimeError("startup failed")):
                            start()
                    self.assert_startup_cleanup(component, interrupt, error="startup failed")

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
