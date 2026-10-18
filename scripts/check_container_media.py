"""Exercise the local container with synthetic 4K HEVC media and verify its export."""

import argparse
import hashlib
import http.cookiejar
import json
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ORIGIN = "http://127.0.0.1:8770"
CHUNK_BYTES = 4 * 1024 * 1024
VIDEO_FRAMES = 240
OUTPUT_FRAMES = 1800


class CapacityError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise CapacityError(message)


class Client:
    def __init__(self):
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        self.csrf = None

    def request(self, path, method="GET", value=None, headers=None, accept_busy=False):
        if isinstance(value, bytes):
            body = value
        elif value is not None:
            body = json.dumps(value).encode()
        else:
            body = None
        request_headers = {"Origin": ORIGIN, **(headers or {})}
        if body is not None and not isinstance(value, bytes):
            request_headers["Content-Type"] = "application/json"
        if method not in {"GET", "HEAD"}:
            request_headers["X-CSRF-Token"] = self.csrf
        request = urllib.request.Request(
            ORIGIN + path, data=body, headers=request_headers, method=method
        )
        try:
            with self.opener.open(request, timeout=120) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            if accept_busy and error.code == 429:
                return error.code, None
            raise CapacityError(f"HTTP {error.code}") from None

    def session(self):
        _, value = self.request("/api/session")
        self.csrf = value["csrf"]
        require(0 < value["limits"]["chunk_bytes"] <= 8 * 1024 * 1024, "chunk limit")
        return min(CHUNK_BYTES, value["limits"]["chunk_bytes"])

    def upload(self, project, path, chunk_bytes):
        endpoint = f"/api/projects/{project}/assets"
        status, asset = self.request(
            endpoint, "POST", {"name": path.name, "size": path.stat().st_size}
        )
        require(status == 201, "register asset")
        endpoint += "/" + asset["id"]
        offset = 0
        with path.open("rb") as source:
            while chunk := source.read(chunk_bytes):
                status, receipt = self.request(
                    endpoint + "/chunks",
                    "PUT",
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Upload-Offset": str(offset),
                        "X-Chunk-SHA256": hashlib.sha256(chunk).hexdigest(),
                    },
                    value=chunk,
                )
                offset += len(chunk)
                require(status == 200 and receipt["offset"] == offset, "upload receipt")
        require(offset == path.stat().st_size, "uploaded bytes")
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            status, result = self.request(endpoint + "/finish", "POST", {}, accept_busy=True)
            if status == 429:
                time.sleep(1)
                continue
            require(status == 200 and result["state"] == "ready", "asset verification")
            return result
        raise CapacityError("asset verification timeout")


def make_video(path, width=3840, height=2160):
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-filter_threads", "2", "-f", "lavfi", "-i",
        f"testsrc2=size={width}x{height}:rate=60:duration=4",
        "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=4",
        "-map", "0:v", "-map", "1:a", "-frames:v", str(VIDEO_FRAMES),
        "-c:v", "libx265", "-preset", "ultrafast", "-threads", "2",
        "-x265-params", "pools=2:frame-threads=2:log-level=error",
        "-pix_fmt", "yuv420p10le", "-color_primaries", "bt709",
        "-color_trc", "bt709", "-colorspace", "bt709",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart", str(path),
    ]
    subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                   check=True, timeout=600)
    require(path.stat().st_size > 0, "empty source video")
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    streams = json.loads(probe.stdout)["streams"]
    video = next((stream for stream in streams if stream["codec_type"] == "video"), None)
    require(video is not None and video["codec_name"] == "hevc", "source codec")
    require(video["pix_fmt"] == "yuv420p10le", "source bit depth")
    require((video["width"], video["height"]) == (width, height), "source dimensions")
    require(video["avg_frame_rate"] == "60/1" and int(video["nb_frames"]) == VIDEO_FRAMES,
            "source frame rate and count")
    require(any(stream["codec_type"] == "audio" for stream in streams), "source audio")


def make_photos(directory):
    from PIL import Image

    paths = []
    for index in range(20):
        path = directory / f"synthetic-{index + 1:02d}.jpg"
        image = Image.new("RGB", (96, 54))
        image.putdata([
            ((x * 3 + index * 11) % 256,
             (y * 5 + index * 13) % 256,
             ((x + y) * 2 + index * 17) % 256)
            for y in range(54) for x in range(96)
        ])
        image.save(path, quality=80)
        paths.append(path)
    return paths


def make_plan(client, project, video_id):
    base = f"/api/projects/{project}"
    _, current = client.request(base)
    _, current = client.request(base + "/draft", "POST", {"revision": current["revision"]})
    scenes = current["plan"]["scenes"]
    require(len(scenes) == 20, "initial scene count")
    require(sum(scene["duration_frames"] if scene["kind"] == "image" else
                scene["end_frame"] - scene["start_frame"] for scene in scenes) == OUTPUT_FRAMES,
            "initial duration")
    candidate = next(
        (item for item in current["candidates"]
         if item["source_id"] == video_id and item["start_frame"] == 0
         and item["end_frame"] == 120), None
    )
    require(candidate is not None, "video candidate")
    for scene in scenes:
        _, current = client.request(base + "/edits", "POST", {
            "revision": current["revision"],
            "change": {"kind": "replace", "scene_id": scene["id"],
                       "candidate_id": candidate["id"]},
        })
    for scene in scenes[:10]:
        _, current = client.request(base + "/edits", "POST", {
            "revision": current["revision"],
            "change": {"kind": "trim", "scene_id": scene["id"],
                       "start_frame": 30, "end_frame": 90},
        })
    _, current = client.request(base + "/edits", "POST", {
        "revision": current["revision"], "change": {"kind": "music", "id": "calm"},
    })
    require(current["music"]["id"] == "calm", "music selection")
    final = current["plan"]["scenes"]
    require(len(final) == 20 and all(scene["included"] and scene["kind"] == "video"
                                     and scene["source_id"] == video_id for scene in final),
            "final scene sources")
    require([scene["end_frame"] - scene["start_frame"] for scene in final] ==
            [60] * 10 + [120] * 10, "final scene duration")
    return current["revision"]


def wait_for_job(client, project, revision):
    base = f"/api/projects/{project}/jobs"
    status, job = client.request(base, "POST", {
        "revision": revision, "request_key": secrets.token_hex(16),
    })
    require(status == 202 and job["revision"] == revision, "enqueue render")
    deadline = time.monotonic() + 1000
    while time.monotonic() < deadline:
        _, state = client.request(base + "/" + job["id"])
        if state["state"] == "succeeded":
            require(state["error"] is None, "successful job error")
            return job["id"]
        require(state["state"] in {"queued", "running"}, "render failed or cancelled")
        require(state["error"] is None, "render error")
        time.sleep(2)
    raise CapacityError("render timeout")


def download(client, project, job, path):
    endpoint = f"/api/projects/{project}/jobs/{job}/artifact"
    request = urllib.request.Request(ORIGIN + endpoint, headers={"Origin": ORIGIN})
    try:
        with client.opener.open(request, timeout=120) as response, path.open("wb") as output:
            require(response.status == 200 and response.headers.get_content_type() == "video/mp4",
                    "artifact response")
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
    except urllib.error.HTTPError as error:
        raise CapacityError(f"artifact HTTP {error.code}") from None
    require(path.stat().st_size > 0, "empty artifact")
    return path.stat().st_size


def verify_output(path):
    import av
    import numpy as np

    with av.open(str(path)) as container:
        require(len(container.streams.video) == 1, "output video stream")
        video = container.streams.video[0]
        require(video.codec_context.name == "h264", "output codec")
        require((video.width, video.height) == (1280, 720), "output dimensions")
        require(video.average_rate == 30, "output frame rate")
        frames = sum(1 for _ in container.decode(video=0))
        require(frames == OUTPUT_FRAMES, "decoded frame count")
    with av.open(str(path)) as container:
        require(len(container.streams.audio) == 1, "output audio stream")
        audio = container.streams.audio[0]
        require(audio.codec_context.sample_rate == 48000, "output audio rate")
        samples = 0
        window = []
        for frame in container.decode(audio=0):
            data = frame.to_ndarray()
            require(data.shape[0] == 2, "output stereo audio")
            mono = data.mean(axis=0)
            start, end = samples, samples + len(mono)
            if end > 5 * 48000 and start < 6 * 48000:
                window.append(mono[max(0, 5 * 48000 - start):min(len(mono), 6 * 48000 - start)])
            samples = end
        require(abs(samples - 60 * 48000) <= 4096, "decoded audio duration")
        sound = np.concatenate(window)
        require(len(sound) == 48000, "audio sample window")
        spectrum = np.abs(np.fft.rfft(sound * np.hanning(len(sound))))
        floor = max(1e-6, np.median(spectrum[100:1500]))
        require(max(spectrum[878:883]) > floor * 20, "source 880 Hz audio")
        require(max(spectrum[150:750]) > floor * 20, "background music audio")
    return frames


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    stage = "fixture"
    try:
        with tempfile.TemporaryDirectory(prefix="moment-capacity-") as temporary:
            directory = Path(temporary)
            photos = make_photos(directory)
            video = directory / "synthetic-4k60-hevc10-sdr.mp4"
            make_video(video)
            stage = "session"
            client = Client()
            chunk_bytes = client.session()
            _, project = client.request("/api/projects", "POST", {})
            stage = "upload"
            for photo in photos:
                client.upload(project["id"], photo, chunk_bytes)
            source = client.upload(project["id"], video, chunk_bytes)
            require(source["source"]["kind"] == "video" and
                    (source["source"]["width"], source["source"]["height"]) == (3840, 2160)
                    and source["source"]["has_audio"] and
                    source["source"]["duration_frames"] >= 120, "verified source metadata")
            stage = "plan"
            revision = make_plan(client, project["id"], source["id"])
            stage = "render"
            job = wait_for_job(client, project["id"], revision)
            stage = "download"
            output_bytes = download(client, project["id"], job, directory / "output.mp4")
            stage = "decode"
            frames = verify_output(directory / "output.mp4")
            print(json.dumps({"synthetic_assets": 21, "decoded_frames": frames,
                              "output_bytes": output_bytes,
                              "errors": 0}), flush=True)
    except Exception as error:
        if isinstance(error, CapacityError):
            detail = str(error)
        elif isinstance(error, subprocess.CalledProcessError):
            detail = f"native exit {error.returncode}"
        else:
            detail = type(error).__name__
        print(f"capacity_gate_failed stage={stage} error={detail}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
