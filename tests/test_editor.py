import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).parents[1] / "experiments" / "editor"))

import av
from PIL import Image

from media import Registry, fingerprint, render
from server import State, handler_for
from timeline import EditPlan, Scene, Source, baseline, edit
from http.server import ThreadingHTTPServer


FFMPEG = os.environ.get("EDITOR_FFMPEG", "ffmpeg")


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.sources = [Source("a", "a.jpg", 100, 100, 100, "image"), Source("b", "b.mp4", 100, 100, 100, "video", 300, True)]

    def test_baseline_edit_and_invalid_preserves_plan(self):
        plan = baseline(self.sources)
        self.assertEqual([scene.source_id for scene in plan.scenes], ["a", "b"])
        self.assertEqual(plan.frame_count, 480)
        changed = edit(plan, {"kind": "exclude", "scene_id": "scene-1"}, self.sources)
        self.assertEqual(changed.frame_count, 300)
        self.assertEqual(changed.scenes[0].duration_frames, plan.scenes[0].duration_frames)
        replaced = edit(changed, {"kind": "replace", "scene_id": "scene-1", "source_id": "b"}, self.sources)
        self.assertFalse(replaced.scenes[0].included)
        self.assertEqual(replaced.scenes[0].length, 180)
        with self.assertRaises(ValueError):
            edit(replaced, {"kind": "trim", "scene_id": "scene-1", "start_frame": 2.1, "end_frame": 20}, self.sources)
        self.assertEqual(replaced.revision, 3)

    def test_even_selection_and_capacity(self):
        sources = [Source(str(i), f"{i}.jpg", 1, 2, 2, "image") for i in range(100)]
        plan = baseline(sources)
        self.assertEqual(len(plan.scenes), 12)
        self.assertEqual(plan.frame_count, 1800)
        self.assertEqual([scene.source_id for scene in plan.scenes], [str(i * 100 // 12) for i in range(12)])

    def test_replace_rejects_non_string_source_ids(self):
        plan = baseline(self.sources)
        for value in ([], {}, None, 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                edit(plan, {"kind": "replace", "scene_id": "scene-1", "source_id": value}, self.sources)


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "originals"
        self.root.mkdir()
        self.work = Path(self.temp.name) / "work"
        Image.new("RGB", (48, 96), (255, 0, 0)).save(self.root / "portrait.png")
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=96x48:r=30:d=2", "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(self.root / "tone.mp4")], check=True)

    def test_match_rejects_ambiguous_and_changed(self):
        registry = Registry(self.root)
        path = self.root / "portrait.png"
        size, head, tail = fingerprint(path)
        descriptor = {"name": path.name, "size": size, "head": head, "tail": tail}
        source = registry.match([descriptor])[0]
        self.assertEqual(registry.path(source.id), path.resolve())
        with self.assertRaises(ValueError):
            registry.match([descriptor, descriptor])
        path.write_bytes(b"changed")
        with self.assertRaises(ValueError):
            registry.path(source.id)
        with self.assertRaises(ValueError):
            registry.match([{**descriptor, "name": "../portrait.png"}])

    def test_match_rejects_original_replaced_by_outside_symlink(self):
        registry = Registry(self.root)
        path = self.root / "portrait.png"
        size, head, tail = fingerprint(path)
        outside = Path(self.temp.name) / "outside.png"
        Image.new("RGB", (192, 192), (0, 0, 255)).save(outside)
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaises(ValueError):
            registry.match([{"name": path.name, "size": size, "head": head, "tail": tail}])

    def test_decoded_timeline_padding_and_audio(self):
        registry = Registry(self.root)
        descriptors = []
        for name in ("portrait.png", "tone.mp4"):
            path = self.root / name
            size, head, tail = fingerprint(path)
            descriptors.append({"name": name, "size": size, "head": head, "tail": tail})
        sources = registry.match(descriptors)
        plan = EditPlan(1, 1, (Scene("one", sources[0].id, "image", True, duration_frames=30), Scene("two", sources[1].id, "video", True, start_frame=0, end_frame=60)))
        artifact, path = render(plan, registry, self.work, FFMPEG)
        self.assertTrue((self.work / f"{artifact}.json").exists())
        with av.open(str(path)) as container:
            video = list(container.decode(video=0))
        self.assertEqual(len(video), 90)
        first = video[10].to_ndarray(format="rgb24")
        later = video[50].to_ndarray(format="rgb24")
        self.assertLess(first[360, 100].max(), 25)
        self.assertGreater(first[360, 640, 0], 180)
        self.assertGreater(later[360, 640, 2], 150)
        with av.open(str(path)) as container:
            audio = list(container.decode(audio=0))
        samples = [frame.to_ndarray().flatten() for frame in audio]
        import numpy as np
        merged = np.concatenate(samples)
        self.assertLess(np.abs(merged[:48000]).mean(), .002)
        self.assertGreater(np.abs(merged[60000:100000]).mean(), .005)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name) / "root"
        root.mkdir()
        Image.new("RGB", (32, 32), (0, 255, 0)).save(root / "one.png")
        self.path = root / "one.png"
        self.state = State(root, Path(self.temp.name) / "work", FFMPEG)
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self.state))
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        self.base = f"http://127.0.0.1:{self.http.server_port}"

    def call(self, path, body=None, token=True, headers=None):
        request_headers = headers or {}
        if token:
            request_headers["X-Editor-Capability"] = self.state.token
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        request = Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, headers=request_headers)
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except HTTPError as exc:
            return exc.code, exc.read()

    def test_routes_capability_revision_and_recovery(self):
        self.assertEqual(self.call("/api/state", token=False)[0], 403)
        self.assertEqual(self.call("/../PRODUCT.md")[0], 404)
        self.assertEqual(self.call("/api/state", headers={"Origin": "https://other.example"})[0], 403)
        size, head, tail = fingerprint(self.path)
        descriptor = {"name": self.path.name, "size": size, "head": head, "tail": tail}
        self.assertEqual(self.call("/api/select", {"files": [{**descriptor, "head": "0" * 64}]})[0], 400)
        status, content = self.call("/api/select", {"files": [descriptor]})
        self.assertEqual(status, 200)
        state = json.loads(content)
        scene = state["plan"]["scenes"][0]
        self.assertEqual(self.call("/api/edit", {"revision": 0, "change": {"kind": "exclude", "scene_id": scene["id"]}})[0], 409)
        self.assertEqual(self.call("/api/edit", {"revision": 1, "change": {"kind": "trim", "scene_id": scene["id"], "start_frame": 0, "end_frame": 1}})[0], 400)
        self.assertEqual(self.call("/api/state")[0], 200)
        self.assertEqual(json.loads(self.call("/api/state")[1])["plan"]["revision"], 1)
        self.assertEqual(self.call("/api/render", {"revision": 1})[0], 200)
        ready = json.loads(self.call("/api/state")[1])
        artifact = ready["render"]["artifact_id"]
        movie = f"/artifact/{artifact}.mp4?cap={self.state.token}"
        self.assertEqual(self.call(movie, token=False, headers={"Range": "bytes=0-99"})[0], 206)
        self.assertEqual(self.call(movie, token=False, headers={"Range": "bytes=100-0"})[0], 416)
        self.assertEqual(self.call(movie, token=False, headers={"Range": "bytes=0-"})[0], 206)
        self.assertEqual(self.call("/artifact/" + artifact + ".mp4", token=False)[0], 403)
        self.assertEqual(self.call("/api/edit", {"revision": 1, "change": {"kind": "exclude", "scene_id": scene["id"]}})[0], 200)
        self.assertEqual(self.call(movie, token=False)[0], 404)
        self.assertEqual(self.call("/api/render", {"revision": 2})[0], 422)

    def test_reselection_rejects_previous_plan_edits(self):
        size, head, tail = fingerprint(self.path)
        selection = {"files": [{"name": self.path.name, "size": size, "head": head, "tail": tail}]}
        old = json.loads(self.call("/api/select", selection)[1])["plan"]
        new = json.loads(self.call("/api/select", selection)[1])["plan"]
        self.assertGreater(new["revision"], old["revision"])
        stale = {"revision": old["revision"], "change": {"kind": "exclude", "scene_id": old["scenes"][0]["id"]}}
        self.assertEqual(self.call("/api/edit", stale)[0], 409)


if __name__ == "__main__":
    unittest.main()
