import http.client
import http.cookiejar
import io
import json
import os
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


class ProcessTests(unittest.TestCase):
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
