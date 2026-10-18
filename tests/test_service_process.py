import http.client
import http.cookiejar
import io
import itertools
import json
import os
import signal
import threading
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.error import URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener

from PIL import Image

from moment_draft.config import Config
from moment_draft.media import Cancelled, run_native
from moment_draft.projects import Projects


class ProcessTests(unittest.TestCase):
    def test_shutdown_releases_stalled_playback_and_download_before_restart(self):
        for host, shutdown_signal in itertools.product(("localhost", "127.0.0.1"), (signal.SIGINT, signal.SIGTERM)):
            with self.subTest(host=host, signal=shutdown_signal), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    port = listener.getsockname()[1]
                origin = f"http://{host}:{port}"
                config = Config(root / "data", origin=origin, free_floor=0)
                projects = Projects(config)
                token, session = projects.new_session()
                project = projects.create(session["owner"])["id"]
                job = "stalled-response"
                with projects.transaction() as db:
                    db.execute("INSERT INTO jobs(id,project,revision,request_key,payload_hash,snapshot,state,created) VALUES(?,?,0,?,?,?,'succeeded',?)",
                               (job, project, job, "fixture", "{}", time.time()))
                artifact = projects.artifact_path(project, job)
                with artifact.open("wb") as output:
                    output.truncate(32 * 1024**2)
                projects.close()
                env = {**os.environ, "MOMENT_DATA_DIR": str(config.data_dir), "MOMENT_HOST": host,
                       "MOMENT_PORT": str(port), "MOMENT_ORIGIN": origin}
                processes, connections = [], []
                log_path = root / "server.log"
                with log_path.open("ab") as log:
                    def start():
                        child = subprocess.Popen([sys.executable, "-m", "moment_draft"], env=env, stdout=log, stderr=log)
                        processes.append(child)
                        deadline = time.monotonic() + 10
                        while time.monotonic() < deadline:
                            self.assertIsNone(child.poll(), log_path.read_text())
                            try:
                                with build_opener().open(origin + "/healthz", timeout=1) as response:
                                    self.assertEqual(response.status, 200)
                                    return child
                            except (URLError, TimeoutError):
                                time.sleep(.05)
                        self.fail("service did not start")

                    try:
                        process = start()
                        endpoint = f"/api/projects/{project}/jobs/{job}/artifact"
                        for suffix in ("", "?download=1"):
                            connection = http.client.HTTPConnection(host, port, timeout=3)
                            connections.append(connection)
                            connection.connect()
                            connection.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
                            connection.request("GET", endpoint + suffix, headers={"Cookie": f"moment_owner={token}"})
                            response = connection.getresponse()
                            self.assertEqual(response.status, 200)
                            self.assertEqual(int(response.getheader("Content-Length")), artifact.stat().st_size)
                            if suffix:
                                self.assertIn("attachment", response.getheader("Content-Disposition"))
                        time.sleep(.2)
                        process.send_signal(shutdown_signal)
                        try:
                            process.wait(timeout=8)
                        except subprocess.TimeoutExpired:
                            self.fail("service did not release stalled media responses during shutdown")
                        self.assertEqual(process.returncode, 0, log_path.read_text())
                        self.assertNotIn("thread(s) still running", log_path.read_text())
                        restarted = start()
                        connection = http.client.HTTPConnection(host, port, timeout=3)
                        connections.append(connection)
                        connection.request("GET", endpoint, headers={"Cookie": f"moment_owner={token}", "Range": "bytes=0-15"})
                        response = connection.getresponse()
                        self.assertEqual(response.status, 206)
                        self.assertEqual(response.read(), bytes(16))
                        restarted.terminate()
                        restarted.wait(timeout=8)
                        self.assertEqual(restarted.returncode, 0, log_path.read_text())
                    finally:
                        for connection in connections:
                            connection.close()
                        for child in processes:
                            if child.poll() is None:
                                child.kill()
                                child.wait(timeout=5)

    def test_restart_preserves_cookie_chunks_job_and_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            origin = f"http://127.0.0.1:{port}"
            env = {**os.environ, "MOMENT_DATA_DIR": str(root / "data"), "MOMENT_PORT": str(port), "MOMENT_ORIGIN": origin, "MOMENT_HOST": "127.0.0.1"}
            opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
            process = None
            csrf = None
            log = (root / "server.log").open("ab")

            def call(path, data=None, method=None, extra=None):
                headers = {"Origin": origin, **(extra or {})}
                if data is not None:
                    headers["X-CSRF-Token"] = csrf
                    headers["Content-Type"] = "application/octet-stream" if isinstance(data, bytes) else "application/json"
                    if not isinstance(data, bytes):
                        data = json.dumps(data).encode()
                request = Request(origin + path, data=data, method=method, headers=headers)
                with opener.open(request, timeout=40) as response:
                    content = response.read()
                    return json.loads(content) if response.headers.get_content_type() == "application/json" else content

            def start():
                child = subprocess.Popen([sys.executable, "-m", "moment_draft"], env=env, stdout=log, stderr=log)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if child.poll() is not None:
                        raise AssertionError((root / "server.log").read_text())
                    try:
                        call("/healthz")
                        return child
                    except URLError:
                        time.sleep(.05)
                child.terminate()
                child.wait(timeout=10)
                raise AssertionError("service did not start")

            try:
                process = start()
                csrf = call("/api/session")["csrf"]
                project = call("/api/projects", {})["id"]
                base = f"/api/projects/{project}"
                chunk = b"x" * (8 * 1024**2)
                boundary_asset = call(base + "/assets", {"name": "boundary.mp4", "size": len(chunk)})["id"]
                boundary_endpoint = base + f"/assets/{boundary_asset}"
                self.assertEqual(call(boundary_endpoint + "/chunks", chunk, "PUT", {"Upload-Offset": "0"})["offset"], len(chunk))
                self.assertEqual(call(boundary_endpoint)["offset"], len(chunk))
                call(boundary_endpoint, method="DELETE", extra={"X-CSRF-Token": csrf})
                image = io.BytesIO()
                Image.new("RGB", (48, 96), "red").save(image, format="PNG")
                data = image.getvalue()
                asset = call(base + "/assets", {"name": "portrait.png", "size": len(data)})["id"]
                endpoint = base + f"/assets/{asset}"
                self.assertEqual(call(endpoint + "/chunks", data[:53], "PUT", {"Upload-Offset": "0"})["offset"], 53)
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                connection.putrequest("PUT", endpoint + "/chunks")
                connection.putheader("Content-Length", str(8 * 1024**2 + 1))
                connection.endheaders()
                self.assertEqual(connection.getresponse().status, 413)
                connection.close()
                process.kill()
                process.wait(timeout=10)
                process = start()
                self.assertEqual(call("/api/session")["csrf"], csrf)
                self.assertEqual(call(endpoint)["offset"], 53)
                call(endpoint + "/chunks", data[53:], "PUT", {"Upload-Offset": "53"})
                self.assertEqual(call(endpoint + "/finish", {})["state"], "ready")
                call(base + "/draft", {"revision": 0})
                job = call(base + "/jobs", {"revision": 1, "request_key": "process-render"})
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    state = call(base)
                    if state["jobs"][0]["state"] not in {"queued", "running"}:
                        break
                    time.sleep(.1)
                self.assertEqual(state["jobs"][0]["state"], "succeeded", state)
                artifact = state["artifact"]
                content = call(artifact)
                process.terminate()
                process.wait(timeout=10)
                process = start()
                self.assertEqual(call(base)["jobs"][0]["id"], job["id"])
                self.assertEqual(call(artifact), content)
                self.assertEqual(call(artifact + "?download=1"), content)
            finally:
                if process and process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
                log.close()

    def test_cancellation_reaps_native_that_ignores_sigterm(self):
        self.cancel_native()

    def test_cancellation_escalates_after_supervisor_death(self):
        self.cancel_native(signal.SIGKILL)

    def test_cancellation_escalates_while_supervisor_is_stopped(self):
        self.cancel_native(signal.SIGSTOP)

    def cancel_native(self, supervisor_signal=None):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            marker = work / "native.json"
            code = "import json,os,signal,sys,time; from pathlib import Path; signal.signal(signal.SIGTERM, signal.SIG_IGN); Path(sys.argv[1]).write_text(json.dumps([os.getpid(),os.getppid()])); time.sleep(60)"
            cancel = threading.Event()
            outcomes = []

            def run():
                try:
                    run_native([sys.executable, "-c", code, str(marker)], work,
                               lambda: not cancel.is_set(), time.monotonic() + 20)
                except Cancelled:
                    outcomes.append("cancelled")

            thread = threading.Thread(target=run)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(marker.exists())
                pid, supervisor = json.loads(marker.read_text())
                if supervisor_signal:
                    os.kill(supervisor, supervisor_signal)
                cancel.set()
                thread.join(timeout=6)
                self.assertFalse(thread.is_alive())
                self.assertEqual(outcomes, ["cancelled"])
                state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
                self.assertTrue(not state or state.startswith("Z"), state)
            finally:
                cancel.set()
                thread.join(timeout=6)
                if marker.exists():
                    try:
                        os.kill(json.loads(marker.read_text())[0], signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_restart_waits_for_paused_supervisor_and_native_exit(self):
        self.restart_native()

    def test_restart_remains_blocked_after_supervisor_death(self):
        self.restart_native(kill_supervisor=True)

    def restart_native(self, kill_supervisor=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "native.json"
            ffmpeg = root / "native"
            ffmpeg.write_text(f"#!{sys.executable}\nimport json,os,signal,time\nfrom pathlib import Path\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nPath({str(marker)!r}).write_text(json.dumps([os.getpid(),os.getppid()]))\ntime.sleep(60)\n")
            ffmpeg.chmod(0o700)
            script = """
import io,sys
from pathlib import Path
from PIL import Image
from moment_draft.config import Config
from moment_draft.projects import Projects
from moment_draft.worker import Worker
root = Path(sys.argv[1])
store = Projects(Config(root / 'data', ffmpeg=str(root / 'native'), free_floor=0))
_, session = store.new_session()
owner = session['owner']
project = store.create(owner)['id']
image = io.BytesIO()
Image.new('RGB', (16, 16), 'red').save(image, format='PNG')
data = image.getvalue()
asset = store.register(owner, project, 'image.png', len(data))['id']
store.append(owner, project, asset, 0, data)
store.finish(owner, project, asset)
store.revise(owner, project, 0)
store.enqueue(owner, project, 1, 'restart-test')
Worker(store).run_once()
"""
            parent = subprocess.Popen([sys.executable, "-c", script, str(root)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            native = supervisor = None
            try:
                deadline = time.monotonic() + 10
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(marker.exists())
                native, supervisor = json.loads(marker.read_text())
                attempts = list((root / "data" / "projects").glob("*/attempts/*"))
                self.assertEqual(len(attempts), 1)
                os.kill(supervisor, signal.SIGSTOP)
                parent.kill()
                parent.wait(timeout=5)
                config = Config(root / "data", free_floor=0)
                try:
                    restarted = Projects(config)
                except RuntimeError:
                    pass
                else:
                    restarted.close()
                    self.fail("restart acquired the volume while its prior native process was alive")
                self.assertTrue(attempts[0].is_dir())
                os.kill(native, 0)
                if kill_supervisor:
                    os.kill(supervisor, signal.SIGKILL)
                    time.sleep(.1)
                    try:
                        restarted = Projects(config)
                    except RuntimeError:
                        pass
                    else:
                        restarted.close()
                        self.fail("restart acquired the volume while its orphan native process was alive")
                    self.assertTrue(attempts[0].is_dir())
                    os.kill(native, signal.SIGKILL)
                else:
                    os.kill(supervisor, signal.SIGCONT)
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline:
                    try:
                        restarted = Projects(config)
                    except RuntimeError:
                        time.sleep(.05)
                    else:
                        break
                else:
                    self.fail("volume lease was not released after native shutdown")
                try:
                    self.assertFalse(attempts[0].exists())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(native, 0)
                finally:
                    restarted.close()
            finally:
                if supervisor:
                    try:
                        os.kill(supervisor, signal.SIGCONT)
                    except ProcessLookupError:
                        pass
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=5)
                if native:
                    try:
                        os.kill(native, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_parent_crash_closes_native_supervisor(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            progress = root / "progress"
            ffmpeg = os.environ.get("MOMENT_FFMPEG", "ffmpeg")
            command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi", "-i", "color=s=96x48:r=30:d=60", "-progress", str(progress), "-f", "null", "-"]
            script = "from moment_draft.media import run_native; from pathlib import Path; import json,sys,time; run_native(json.loads(sys.argv[1]),Path(sys.argv[2]),lambda:True,time.monotonic()+60)"
            parent = subprocess.Popen([sys.executable, "-c", script, json.dumps(command), str(root)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            def native_processes():
                lines = subprocess.check_output(["ps", "-axo", "pid=,command="], text=True).splitlines()
                return [line for line in lines if str(progress) in line and " -progress " in line and " -c " not in line]

            try:
                deadline = time.monotonic() + 10
                while not progress.exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue(progress.exists())
                self.assertTrue(native_processes())
                parent.kill()
                parent.wait(timeout=10)
                deadline = time.monotonic() + 5
                while native_processes() and time.monotonic() < deadline:
                    time.sleep(.1)
                self.assertEqual(native_processes(), [])
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
