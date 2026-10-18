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
        self.assertEqual(len(frames), 120)
        self.assertLess(frames[30].to_ndarray(format="rgb24")[360, 100].max(), 25)
        self.assertGreater(frames[30].to_ndarray(format="rgb24")[360, 640, 0], 180)
        self.assertGreater(frames[110].to_ndarray(format="rgb24")[360, 640, 2], 180)
        with av.open(str(path)) as container:
            sound = np.concatenate([frame.to_ndarray() for frame in container.decode(audio=0)], axis=1)
        self.assertLess(np.abs(sound[:, :48000]).mean(), .002)
        self.assertGreater(np.abs(sound[:, 3 * 48000:]).mean(), .005)
        stranger = self.app.test_client()
        self.call("/api/session", client=stranger)
        for url in [self.base, self.base + f"/assets/{video}", self.base + f"/assets/{video}/source", artifact]:
            self.assertEqual(self.call(url, headers={"Range": "bytes=0-9"}, client=stranger).status_code, 404)
        self.call(self.base + "/edits", {"revision": 2, "change": {"kind": "exclude", "scene_id": scene}})
        self.assertEqual(self.call(artifact).status_code, 404)

    def test_cleanup_after_completion_waits_for_attempt_release_and_worker_continues(self):
        self.upload(self.photo())
        self.call(self.base + "/draft", {"revision": 0})
        self.call(self.base + "/jobs", {"revision": 1, "request_key": "cleanup-completion"})
        complete = self.store.complete
        first_project = self.project
        observed = []

        def complete_then_cleanup(job, target):
            result = complete(job, target)
            self.now += self.config.retention_seconds
            cleanup = threading.Thread(target=lambda: observed.append(self.store.cleanup(dry_run=False)))
            cleanup.start()
            cleanup.join(timeout=5)
            self.assertFalse(cleanup.is_alive())
            self.assertTrue(self.store.directory(first_project).exists())
            return result

        worker = Worker(self.store)
        with patch.object(self.store, "complete", side_effect=complete_then_cleanup):
            self.assertTrue(worker.run_once())
        self.assertEqual(observed, [[first_project]])
        self.store.cleanup(dry_run=False)
        self.assertFalse(self.store.directory(first_project).exists())
        self.csrf = self.call("/api/session").json["csrf"]
        self.project = self.call("/api/projects", {}).json["id"]
        self.base = f"/api/projects/{self.project}"
        self.upload(self.photo())
        self.call(self.base + "/draft", {"revision": 0})
        self.call(self.base + "/jobs", {"revision": 1, "request_key": "after-cleanup-job"})
        with patch("moment_draft.worker.shutil.rmtree", side_effect=PermissionError), self.assertLogs("moment_draft.worker", level="ERROR") as logs:
            self.assertTrue(worker.run_once())
        self.assertIn("attempt cleanup failed: PermissionError", logs.output[0])
        self.assertNotIn(str(self.root), logs.output[0])
        self.assertEqual(self.call(self.base).json["jobs"][0]["state"], "succeeded")
        self.assertTrue(worker.run_once())

    def test_session_cap_and_cleanup_preserve_retained_project_owners(self):
        token = self.client.get_cookie("moment_owner", domain="127.0.0.1").value
        with patch("moment_draft.projects.SESSION_LIMIT", 2):
            abandoned_token, _ = self.store.new_session()
            self.assertEqual(self.call("/api/session", client=self.app.test_client()).status_code, 429)
            self.assertEqual(len(self.store.list(self.store.session(token)["owner"])), 1)
            self.now += self.config.retention_seconds
            with self.store.transaction() as db:
                db.execute("UPDATE projects SET expires=? WHERE id=?", (self.now + 100, self.project))
            self.store.cleanup(dry_run=True)
            self.assertIsNotNone(self.store.session(abandoned_token))
            self.store.cleanup(dry_run=False)
            self.assertIsNone(self.store.session(abandoned_token))
            self.assertIsNotNone(self.store.session(token))
            self.assertEqual(self.call(self.base).status_code, 200)
            self.assertEqual(self.call("/api/session", client=self.app.test_client()).status_code, 200)

    def test_candidates_cover_distinct_source_windows_and_short_material(self):
        from moment_draft.timeline import Source, baseline, candidates, edit
        photo = Source("photo", "photo.png", 1, 48, 96, "image")
        video = Source("video", "video.mp4", 1, 96, 48, "video", 30 * 18)
        plan = baseline([photo, video])
        self.assertEqual(plan.frame_count, 21 * 30)
        windows = [(item.source_id, item.start_frame, item.end_frame) for item in plan.scenes]
        self.assertEqual(len(windows), len(set(windows)))
        video_scenes = [item for item in plan.scenes if item.kind == "video"]
        self.assertEqual([(item.start_frame, item.end_frame) for item in video_scenes], [(0, 180), (180, 360), (360, 540)])
        last = candidates([video])[-1]
        replaced = edit(plan, {"kind": "replace", "scene_id": plan.scenes[0].id, "candidate_id": last.id}, [photo, video])
        self.assertEqual((replaced.scenes[0].start_frame, replaced.scenes[0].end_frame), (360, 540))
        self.assertEqual(baseline([photo]).frame_count, 90)
        photos = [Source(str(index), "photo.png", 1, 48, 96, "image") for index in range(100)]
        selected = baseline(photos)
        self.assertEqual(selected.frame_count, 1800)
        self.assertEqual(len({item.source_id for item in selected.scenes}), 20)
        videos = [Source(str(index), "video.mp4", 1, 96, 48, "video", 30 * 6) for index in range(100)]
        distributed = baseline(videos)
        self.assertEqual(len(distributed.scenes), 10)
        self.assertEqual(distributed.scenes[-1].source_id, "99")
        short = [Source(str(index), "video.mp4", 1, 96, 48, "video", 30) for index in range(100)]
        self.assertEqual(baseline(short).frame_count, 1800)
        long = Source("long", "long.mp4", 1, 96, 48, "video", 30 * 400)
        offered = candidates([long])
        self.assertEqual(len(offered), 12)
        self.assertEqual(offered[-1].end_frame, long.duration_frames)
        self.assertTrue(all(a.end_frame <= b.start_frame for a, b in zip(offered, offered[1:])))
        self.assertEqual(baseline([long]).frame_count, 1800)

    def test_busy_finish_preserves_bytes_and_thumbnail_worker_is_owned_and_bounded(self):
        data = self.photo().read_bytes()
        asset = self.call(self.base + "/assets", {"name": "portrait.png", "size": len(data)}).json["id"]
        endpoint = self.base + f"/assets/{asset}"
        self.call(endpoint + "/chunks", data, "PUT", {"Upload-Offset": "0"})
        with self.store.native_slot() as acquired:
            self.assertTrue(acquired)
            self.assertEqual(self.call(endpoint + "/finish", {}).status_code, 429)
            self.assertFalse(Worker(self.store).run_once())
        state = self.call(endpoint).json
        self.assertEqual((state["state"], state["offset"]), ("receiving", len(data)))
        self.assertEqual(self.call(endpoint + "/finish", {}).json["state"], "ready")
        self.call(self.base + "/draft", {"revision": 0})
        worker = Worker(self.store)
        self.assertTrue(worker.run_once())
        self.assertTrue(worker.run_once())
        self.assertFalse(worker.run_once())
        state = self.call(self.base).json
        scene_thumb = state["thumbnails"]["scene-scene-1"]["url"]
        response = self.call(scene_thumb)
        self.assertEqual(response.status_code, 200)
        import io
        with Image.open(io.BytesIO(response.data)) as image:
            self.assertEqual(image.size, (480, 270))
            self.assertLess(max(image.getpixel((10, 135))), 25)
            self.assertGreater(image.getpixel((240, 135))[0], 180)
        stranger = self.app.test_client()
        self.call("/api/session", client=stranger)
        self.assertEqual(self.call(scene_thumb, client=stranger).status_code, 404)
        self.assertEqual(self.call(scene_thumb, headers={"Range": "bytes=0-19"}).status_code, 206)
        self.now += self.config.retention_seconds
        self.assertEqual(self.call(scene_thumb).status_code, 404)

    def test_thumbnail_failure_has_one_explicit_retry(self):
        self.upload(self.photo())
        self.call(self.base + "/draft", {"revision": 0})
        worker = Worker(self.store)
        with patch("moment_draft.worker.create_thumbnail", side_effect=ValueError):
            for _ in range(2):
                self.assertTrue(worker.run_once())
            self.assertFalse(worker.run_once())
            failed = self.call(self.base).json["thumbnails"]
            self.assertTrue(all(item["state"] == "failed" and item["retryable"] for item in failed.values()))
            self.assertEqual(self.call(self.base + "/thumbnails/retry", {}).status_code, 200)
            for _ in range(2):
                self.assertTrue(worker.run_once())
            exhausted = self.call(self.base + "/thumbnails/retry", {}).json["thumbnails"]
            self.assertTrue(all(item["state"] == "failed" and not item["retryable"] for item in exhausted.values()))
            self.assertFalse(worker.run_once())

    def test_music_native_mix_immutable_revisions_and_reload(self):
        self.upload(self.photo())
        self.upload(self.video())
        state = self.call(self.base + "/draft", {"revision": 0}).json
        tracks = self.call("/api/music").json["tracks"]
        self.assertEqual(len(tracks), 3)
        self.assertTrue(all(track["license"] == "CC0-1.0" for track in tracks))
        worker = Worker(self.store)
        sounds = {}
        for choice in ["off", *(track["id"] for track in tracks)]:
            if choice != "off":
                state = self.call(self.base + "/edits", {"revision": state["revision"], "change": {"kind": "music", "id": choice}}).json
            job = self.call(self.base + "/jobs", {"revision": state["revision"], "request_key": "music-test-" + choice}).json
            self.assertTrue(worker.run_once())
            current = self.call(self.base).json
            self.assertEqual(current["jobs"][0]["state"], "succeeded", current)
            with av.open(str(self.store.artifact_path(self.project, job["id"]))) as container:
                sounds[choice] = np.concatenate([frame.to_ndarray() for frame in container.decode(audio=0)], axis=1).mean(axis=0)
            self.assertEqual(current["music"]["id"], choice)
        silent = sounds["off"][48000:96000]
        self.assertLess(np.abs(silent).mean(), .0001)
        for choice, sound in sounds.items():
            if choice != "off":
                self.assertGreater(np.abs(sound[48000:96000]).mean(), .001)
                self.assertLess(np.max(np.abs(sound)), .97)
            segment = sound[round(3.2 * 48000):round(4.2 * 48000)]
            spectrum = np.abs(np.fft.rfft(segment))
            frequencies = np.fft.rfftfreq(len(segment), 1 / 48000)
            band = spectrum[(frequencies > 875) & (frequencies < 885)].max()
            self.assertGreater(band, 500)
        old_revision = state["revision"]
        immutable = self.call(self.base + "/jobs", {"revision": old_revision, "request_key": "immutable-music"}).json
        edited = self.call(self.base + "/edits", {"revision": old_revision, "change": {"kind": "music", "id": "off"}}).json
        self.assertIsNone(edited["artifact"])
        self.assertEqual(self.call(self.base + "/edits", {"revision": old_revision, "change": {"kind": "music", "id": "calm"}}).status_code, 409)
        self.assertTrue(worker.run_once())
        with self.store.lock:
            snapshot = self.store.db.execute("SELECT snapshot FROM jobs WHERE id=?", (immutable["id"],)).fetchone()[0]
        import json
        self.assertEqual(json.loads(snapshot)["music"]["id"], tracks[-1]["id"])
        self.assertIsNone(self.call(self.base).json["artifact"])
        token = self.client.get_cookie("moment_owner", domain="127.0.0.1").value
        self.store.close()
        self.store = Projects(self.config, clock=lambda: self.now)
        self.app = create_app(self.store)
        self.client = self.app.test_client()
        self.client.set_cookie("moment_owner", token, domain="127.0.0.1")
        restored = self.call(self.base).json
        self.assertEqual(restored["music"], {"id": "off", "level": 0})
        self.assertEqual(restored["revision"], old_revision + 1)
        invalid = self.call(self.base + "/edits", {"revision": restored["revision"], "change": {"kind": "music", "id": []}})
        self.assertEqual(invalid.status_code, 400)

    def test_candidate_replacement_trim_and_thumbnail_source_time(self):
        video = self.root / "colors.mp4"
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=96x48:r=30:d=6", "-f", "lavfi", "-i", "color=c=green:s=96x48:r=30:d=6", "-f", "lavfi", "-i", "color=c=blue:s=96x48:r=30:d=6", "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]", "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)], check=True)
        asset = self.upload(video)
        state = self.call(self.base + "/draft", {"revision": 0}).json
        self.assertEqual(len(state["plan"]["scenes"]), 3)
        candidate = state["candidates"][-1]
        changes = [
            {"kind": "replace", "scene_id": "scene-1", "candidate_id": candidate["id"]},
            {"kind": "trim", "scene_id": "scene-1", "start_frame": 375, "end_frame": 405},
            {"kind": "exclude", "scene_id": "scene-2"},
            {"kind": "exclude", "scene_id": "scene-3"},
            {"kind": "include", "scene_id": "scene-1"},
        ]
        for change in changes:
            state = self.call(self.base + "/edits", {"revision": state["revision"], "change": change}).json
        job = self.call(self.base + "/jobs", {"revision": state["revision"], "request_key": "candidate-replaced"}).json
        worker = Worker(self.store)
        self.assertTrue(worker.run_once())
        with av.open(str(self.store.artifact_path(self.project, job["id"]))) as container:
            frames = list(container.decode(video=0))
        self.assertEqual(len(frames), 30)
        self.assertGreater(frames[15].to_ndarray(format="rgb24")[360, 640, 2], 180)
        for _ in range(4):
            self.assertTrue(worker.run_once())
        self.assertFalse(worker.run_once())
        state = self.call(self.base).json
        import io
        with Image.open(io.BytesIO(self.call(state["thumbnails"]["scene-scene-1"]["url"]).data)) as image:
            self.assertGreater(image.getpixel((240, 135))[2], 180)
        with self.store.lock:
            self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM thumbnails WHERE project=?", (self.project,)).fetchone()[0], 4)
        self.assertEqual(len(list((self.store.directory(self.project) / "thumbnails").glob("*.jpg"))), 4)
        previous_url = state["thumbnails"]["scene-scene-1"]["url"]
        changed = self.call(self.base + "/edits", {"revision": state["revision"], "change": {"kind": "trim", "scene_id": "scene-1", "start_frame": 0, "end_frame": 30}})
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(self.call(previous_url).status_code, 404)
        self.assertTrue(worker.run_once())
        self.assertEqual(len(list((self.store.directory(self.project) / "thumbnails").glob("*.jpg"))), 4)
        new_asset = self.upload(self.photo())
        after_upload = self.call(self.base).json
        self.assertTrue(any(item["source_id"] == new_asset for item in after_upload["candidates"]))
        self.assertFalse(any(item["source_id"] == new_asset for item in after_upload["plan"]["scenes"]))
        rebuilt = self.call(self.base + "/draft", {"revision": after_upload["revision"]}).json
        self.assertTrue(any(item["source_id"] == new_asset for item in rebuilt["plan"]["scenes"]))

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
