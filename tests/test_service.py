import hashlib
import os
import subprocess
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import av
import numpy as np
from PIL import Image

from moment_draft.config import Config
from moment_draft.media import Cancelled, run_native
from moment_draft.projects import Problem, Projects
from moment_draft.web import create_app
from moment_draft.worker import Worker


FFMPEG = os.environ.get("MOMENT_FFMPEG", os.environ.get("EDITOR_FFMPEG", "ffmpeg"))
ORIGIN = "http://127.0.0.1:8770"


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1000
        self.config = Config(self.root / "service", ffmpeg=FFMPEG, free_floor=0)
        self.store = Projects(self.config, clock=lambda: self.now)
        self.addCleanup(lambda: self.store.close())
        self.app = create_app(self.store)
        self.client = self.app.test_client()
        self.csrf = self.call("/api/session").json["csrf"]
        self.project = self.call("/api/projects", {}).json["id"]
        self.base = f"/api/projects/{self.project}"

    def call(self, path, data=None, method=None, headers=None, client=None, csrf=True):
        kwargs = {"base_url": ORIGIN, "headers": {"Origin": ORIGIN, **(headers or {})}}
        if data is not None:
            if csrf:
                kwargs["headers"]["X-CSRF-Token"] = self.csrf
            kwargs["data" if isinstance(data, bytes) else "json"] = data
            if isinstance(data, bytes):
                kwargs["headers"]["Content-Type"] = "application/octet-stream"
        return (client or self.client).open(path, method=method or ("POST" if data is not None else "GET"), **kwargs)

    def upload(self, path, chunk_size=8192):
        data = path.read_bytes()
        response = self.call(self.base + "/assets", {"name": path.name, "size": len(data)})
        self.assertEqual(response.status_code, 201, response.json)
        asset = response.json["id"]
        endpoint = self.base + f"/assets/{asset}"
        for offset in range(0, len(data), chunk_size):
            response = self.call(endpoint + "/chunks", data[offset:offset + chunk_size], "PUT", {"Upload-Offset": str(offset)})
            self.assertEqual(response.status_code, 200, response.json)
        response = self.call(endpoint + "/finish", {})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["state"], "ready", response.json)
        return asset

    def photo(self):
        path = self.root / "portrait.png"
        Image.new("RGB", (48, 96), (255, 0, 0)).save(path)
        return path

    def video(self):
        path = self.root / "tone.mp4"
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=96x48:r=30:d=2", "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)], check=True)
        return path

    def test_native_upload_edit_job_private_range_and_stale_result(self):
        photo, video = self.upload(self.photo(), 53), self.upload(self.video())
        drafted = self.call(self.base + "/draft", {"revision": 0}).json
        self.assertEqual(drafted["plan"]["scenes"][0]["source_id"], photo)
        scene = drafted["plan"]["scenes"][1]["id"]
        edited = self.call(self.base + "/edits", {"revision": 1, "change": {"kind": "trim", "scene_id": scene, "start_frame": 15, "end_frame": 45}})
        self.assertEqual(edited.status_code, 200, edited.json)
        self.assertEqual(self.call(self.base + "/edits", {"revision": 1, "change": {"kind": "exclude", "scene_id": scene}}).status_code, 409)
        body = {"revision": 2, "request_key": "request-one"}
        job = self.call(self.base + "/jobs", body).json
        self.assertEqual(self.call(self.base + "/jobs", body).json["id"], job["id"])
        self.assertEqual(self.call(self.base + "/jobs", {**body, "revision": 1}).status_code, 409)
        self.assertTrue(Worker(self.store).run_once())
        state = self.call(self.base).json
        self.assertEqual(state["jobs"][0]["state"], "succeeded", state)
        artifact = state["artifact"]
        whole = self.call(artifact).data
        self.assertEqual(self.call(artifact + "?download=1").data, whole)
        partial = self.call(artifact, headers={"Range": "bytes=0-99"})
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(partial.data, whole[:100])
        self.assertEqual(self.call(artifact, headers={"Range": "bytes=-100"}).data, whole[-100:])
        self.assertEqual(self.call(artifact, headers={"Range": "bytes=100-0"}).status_code, 416)
        self.assertEqual(self.call(artifact, headers={"Range": "bytes=0-1,3-4"}).status_code, 416)
        path = self.store.artifact_path(self.project, job["id"])
        with av.open(str(path)) as container:
            frames = list(container.decode(video=0))
        self.assertEqual(len(frames), 210)
        self.assertLess(frames[30].to_ndarray(format="rgb24")[360, 100].max(), 25)
        self.assertGreater(frames[30].to_ndarray(format="rgb24")[360, 640, 0], 180)
        self.assertGreater(frames[200].to_ndarray(format="rgb24")[360, 640, 2], 180)
        with av.open(str(path)) as container:
            sound = np.concatenate([frame.to_ndarray() for frame in container.decode(audio=0)], axis=1)
        self.assertLess(np.abs(sound[:, :48000]).mean(), .002)
        self.assertGreater(np.abs(sound[:, 6 * 48000:]).mean(), .005)
        stranger = self.app.test_client()
        self.call("/api/session", client=stranger)
        for url in [self.base, self.base + f"/assets/{video}", self.base + f"/assets/{video}/source", artifact]:
            self.assertEqual(self.call(url, headers={"Range": "bytes=0-9"}, client=stranger).status_code, 404)
        self.call(self.base + "/edits", {"revision": 2, "change": {"kind": "exclude", "scene_id": scene}})
        self.assertEqual(self.call(artifact).status_code, 404)

    def test_chunk_receipts_restart_truncates_uncommitted_tail(self):
        data = self.photo().read_bytes()
        asset = self.call(self.base + "/assets", {"name": "photo.png", "size": len(data)}).json["id"]
        endpoint = self.base + f"/assets/{asset}"
        chunk = data[:53]
        headers = {"Upload-Offset": "0", "X-Chunk-SHA256": hashlib.sha256(chunk).hexdigest()}
        self.assertEqual(self.call(endpoint + "/chunks", chunk, "PUT", headers).json["offset"], 53)
        self.assertEqual(self.call(endpoint + "/chunks", chunk, "PUT", headers).json["offset"], 53)
        self.assertEqual(self.call(endpoint + "/chunks", b"wrong", "PUT", {"Upload-Offset": "0"}).status_code, 409)
        self.assertEqual(self.call(endpoint + "/chunks", b"wrong", "PUT", {"Upload-Offset": "60"}).status_code, 409)
        self.assertEqual(self.call(endpoint + "/finish", {}).status_code, 409)
        path = self.store.asset_path(self.project, asset)
        with path.open("ab") as handle:
            handle.write(b"uncommitted tail")
        token = self.client.get_cookie("moment_owner", domain="127.0.0.1").value
        self.store.close()
        self.store = Projects(self.config, clock=lambda: self.now)
        self.app = create_app(self.store)
        self.client = self.app.test_client()
        self.client.set_cookie("moment_owner", token, domain="127.0.0.1")
        self.assertEqual(path.stat().st_size, 53)
        self.assertEqual(self.call(endpoint).json["receipts"][0]["digest"], hashlib.sha256(chunk).hexdigest())
        self.assertEqual(self.call(endpoint + "/chunks", data[53:], "PUT", {"Upload-Offset": "53"}).status_code, 200)
        self.assertEqual(self.call(endpoint + "/finish", {}).json["state"], "ready")
        self.assertEqual(self.call(endpoint + "/finish", {}).json["state"], "ready")
        self.assertEqual(self.call(self.base).json["assets"][0]["state"], "ready")

    def test_boundaries_limits_invalid_media_and_expiry(self):
        self.assertEqual(self.call(self.base, headers={"Origin": "https://other.test"}).status_code, 403)
        self.assertEqual(self.call(self.base, headers={"Host": "other.test"}).status_code, 403)
        self.assertEqual(self.call(self.base + "/assets", {}, csrf=False).status_code, 403)
        self.assertEqual(self.call(self.base + "/assets", {"name": "../path", "size": 3}).status_code, 400)
        asset = self.call(self.base + "/assets", {"name": "bad.mp4", "size": 3}).json["id"]
        endpoint = self.base + f"/assets/{asset}"
        self.assertEqual(self.call(endpoint + "/chunks", b"x" * (8 * 1024**2 + 1), "PUT", {"Upload-Offset": "0"}).status_code, 413)
        self.assertEqual(self.call(endpoint + "/chunks", b"bad", "PUT", {"Upload-Offset": "0"}).status_code, 200)
        self.assertEqual(self.call(endpoint + "/finish", {}).json["state"], "failed")
        good = self.upload(self.photo())
        self.assertEqual(self.call(self.base + "/draft", {"revision": 0}).status_code, 200)
        self.now += self.config.retention_seconds
        self.assertEqual(self.call(self.base).status_code, 404)
        self.assertEqual(self.call(self.base + f"/assets/{good}/source").status_code, 404)
        self.assertEqual(self.store.cleanup(), [self.project])
        self.assertTrue(self.store.directory(self.project).exists())
        self.store.cleanup(dry_run=False)
        self.assertFalse(self.store.directory(self.project).exists())
        self.assertTrue(self.photo().exists())

    def test_restart_running_job_retry_cancel_and_source_fence(self):
        asset = self.upload(self.photo())
        self.call(self.base + "/draft", {"revision": 0})
        job = self.call(self.base + "/jobs", {"revision": 1, "request_key": "interrupt-one"}).json
        attempt = self.store.claim()
        self.assertEqual(attempt["id"], job["id"])
        self.store.recover()
        self.assertFalse(self.store.active(attempt))
        retry = self.call(self.base + f"/jobs/{job['id']}/retry", {})
        self.assertEqual(retry.json["state"], "queued")
        self.assertEqual(self.call(self.base + f"/jobs/{job['id']}/cancel", {}).json["state"], "cancelled")
        self.call(self.base + f"/jobs/{job['id']}/retry", {})
        second = self.store.claim()
        self.assertNotEqual(second["token"], attempt["token"])
        output = self.root / "pretend.mp4"
        output.write_bytes(b"pretend")
        self.assertFalse(self.store.complete(attempt, output))
        self.call(self.base + f"/jobs/{job['id']}/cancel", {})
        self.assertFalse(self.store.complete(second, output))
        self.store.fail(second, cancelled=True)
        source = self.store.asset_path(self.project, asset)
        source.write_bytes(b"changed")
        self.assertEqual(self.call(self.base + "/jobs", {"revision": 1, "request_key": "changed-source"}).status_code, 409)

    def test_retry_rejects_old_revision_without_queueing(self):
        self.upload(self.photo())
        self.call(self.base + "/draft", {"revision": 0})
        job = self.call(self.base + "/jobs", {"revision": 1, "request_key": "old-revision"}).json
        endpoint = self.base + f"/jobs/{job['id']}"
        self.call(endpoint + "/cancel", {})
        self.call(self.base + "/draft", {"revision": 1})
        response = self.call(endpoint + "/retry", {})
        self.assertEqual(response.status_code, 409, response.json)
        self.assertEqual(self.call(endpoint).json["state"], "cancelled")

    def test_discard_incomplete_and_failed_assets_releases_reservations(self):
        self.store.config = replace(self.config, max_assets=1, project_bytes=3, host_bytes=3)
        for failed in (False, True):
            asset = self.call(self.base + "/assets", {"name": "bad.mp4", "size": 3}).json["id"]
            endpoint = self.base + f"/assets/{asset}"
            self.call(endpoint + "/chunks", b"bad" if failed else b"b", "PUT", {"Upload-Offset": "0"})
            if failed:
                self.assertEqual(self.call(endpoint + "/finish", {}).json["state"], "failed")
            self.assertEqual(self.call(self.base + "/assets", {"name": "new.mp4", "size": 3}).status_code, 413)
            path = self.store.asset_path(self.project, asset)
            response = self.call(endpoint, {}, "DELETE")
            self.assertEqual(response.status_code, 200, response.json)
            self.assertFalse(path.exists())
            self.assertEqual(self.call(endpoint, {}, "DELETE").status_code, 200)
            self.assertEqual(self.call(endpoint).status_code, 404)
            self.assertEqual(self.call(self.base).json["assets"], [])

    def test_discard_checks_owner_csrf_and_asset_state(self):
        asset = self.call(self.base + "/assets", {"name": "partial.mp4", "size": 3}).json["id"]
        endpoint = self.base + f"/assets/{asset}"
        self.assertEqual(self.call(endpoint, {}, "DELETE", csrf=False).status_code, 403)
        stranger = self.app.test_client()
        token = self.call("/api/session", client=stranger).json["csrf"]
        response = self.call(endpoint, {}, "DELETE", headers={"X-CSRF-Token": token}, client=stranger, csrf=False)
        self.assertEqual(response.status_code, 404)
        with self.store.transaction() as db:
            db.execute("UPDATE assets SET state='verifying' WHERE id=?", (asset,))
        self.assertEqual(self.call(endpoint, {}, "DELETE").status_code, 409)
        ready = self.upload(self.photo())
        self.assertEqual(self.call(self.base + f"/assets/{ready}", {}, "DELETE").status_code, 409)
        self.assertEqual(self.call(self.base + f"/assets/{ready}/source").status_code, 200)
        self.now += self.config.retention_seconds
        self.assertEqual(self.call(endpoint, {}, "DELETE").status_code, 404)

    def test_discard_retries_after_unlink_failure_and_interrupted_commit(self):
        self.config = replace(self.config, max_assets=1)
        self.store.config = self.config
        asset = self.call(self.base + "/assets", {"name": "partial.mp4", "size": 3}).json["id"]
        endpoint = self.base + f"/assets/{asset}"
        self.call(endpoint + "/chunks", b"b", "PUT", {"Upload-Offset": "0"})
        path = self.store.asset_path(self.project, asset)
        with patch("moment_draft.projects.os.unlink", side_effect=PermissionError):
            self.assertEqual(self.call(endpoint, {}, "DELETE").status_code, 500)
        self.assertTrue(path.exists())
        self.assertEqual(self.call(self.base + "/assets", {"name": "new.mp4", "size": 3}).status_code, 413)
        with patch("moment_draft.projects.os.fsync", side_effect=OSError):
            self.assertEqual(self.call(endpoint, {}, "DELETE").status_code, 500)
        self.assertFalse(path.exists())
        self.assertEqual(self.call(endpoint).json["offset"], 1)
        self.store.close()
        self.store = Projects(self.config, clock=lambda: self.now)
        self.app = create_app(self.store)
        token = self.client.get_cookie("moment_owner", domain="127.0.0.1").value
        self.client = self.app.test_client()
        self.client.set_cookie("moment_owner", token, domain="127.0.0.1")
        self.assertEqual(self.call(endpoint).json["state"], "failed")
        self.assertEqual(self.call(endpoint, {}, "DELETE").status_code, 200)
        self.assertEqual(self.call(endpoint, {}, "DELETE").status_code, 200)
        self.assertEqual(self.call(self.base + "/assets", {"name": "new.mp4", "size": 3}).status_code, 201)

    def test_discard_never_follows_asset_directory_symlink(self):
        asset = self.call(self.base + "/assets", {"name": "partial.mp4", "size": 3}).json["id"]
        endpoint = self.base + f"/assets/{asset}"
        outside = self.root / "originals"
        outside.mkdir()
        original = outside / asset
        original.write_bytes(b"original")
        directory = self.store.asset_path(self.project, asset).parent
        saved = directory.with_name("saved-assets")
        directory.rename(saved)
        directory.symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.call(endpoint, {}, "DELETE").status_code, 500)
        self.assertEqual(original.read_bytes(), b"original")
        self.assertEqual(self.call(endpoint).status_code, 200)
        directory.unlink()
        saved.rename(directory)
        self.store.asset_path(self.project, asset).symlink_to(original)
        self.assertEqual(self.call(endpoint, {}, "DELETE").status_code, 200)
        self.assertEqual(original.read_bytes(), b"original")

    def test_volume_lock_and_public_origin_refusal(self):
        with self.assertRaises(RuntimeError):
            Projects(self.config)
        with self.assertRaises(ValueError):
            Config(self.root, origin="https://public.example").validate()
        with self.assertRaises(ValueError):
            self.config.bind("0.0.0.0", 8770)
        self.config.bind("0.0.0.0", 8770, container_local=True)
        with self.assertRaises(ValueError):
            Config(self.root, origin="http://public.example:8770").bind("0.0.0.0", 8770, container_local=True)

    def test_reservations_upload_slots_and_retry_caps(self):
        self.store.config = replace(self.config, project_bytes=100, host_bytes=100, max_attempts=1)
        self.assertEqual(self.call(self.base + "/assets", {"name": "a.mp4", "size": 60}).status_code, 201)
        self.assertEqual(self.call(self.base + "/assets", {"name": "b.mp4", "size": 60}).status_code, 413)
        owner = self.store.session(self.client.get_cookie("moment_owner", domain="127.0.0.1").value)["owner"]
        with self.store.upload_slot(owner), self.store.upload_slot(owner):
            with self.assertRaises(Problem) as error:
                with self.store.upload_slot(owner):
                    pass
            self.assertEqual(error.exception.status, 429)
        self.store.config = replace(self.config, max_attempts=1)
        self.upload(self.photo())
        self.call(self.base + "/draft", {"revision": 0})
        job = self.call(self.base + "/jobs", {"revision": 1, "request_key": "one-attempt-only"}).json
        attempt = self.store.claim()
        self.store.fail(attempt)
        self.assertEqual(self.call(self.base + f"/jobs/{job['id']}/retry", {}).status_code, 409)

    def test_cancel_stops_native_process_group(self):
        work = self.root / "native-cancellation"
        work.mkdir()
        progress = work / "progress"
        cancel = threading.Event()
        outcomes = []

        def run():
            try:
                run_native([FFMPEG, "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi", "-i", "color=c=blue:s=96x48:r=30:d=60", "-progress", str(progress), "-f", "null", "-"], work, lambda: not cancel.is_set(), time.monotonic() + 20)
            except Cancelled:
                outcomes.append("cancelled")

        thread = threading.Thread(target=run)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not progress.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(progress.exists())
            cancel.set()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outcomes, ["cancelled"])
        finally:
            cancel.set()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
