import hashlib
import json
import math
import os
import secrets
import subprocess
import tempfile
from dataclasses import asdict
from pathlib import Path

import av

from timeline import FPS, Source

SAMPLE = 65536
ALLOWED = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".mov", ".mp4", ".m4v"}


def fingerprint(path):
    size = path.stat().st_size
    with path.open("rb") as handle:
        head = hashlib.sha256(handle.read(SAMPLE)).hexdigest()
        handle.seek(max(0, size - SAMPLE))
        tail = hashlib.sha256(handle.read(SAMPLE)).hexdigest()
    return size, head, tail


def probe(path, source_id):
    with av.open(str(path), options={"protocol_whitelist": "file,pipe"}) as container:
        video = next((stream for stream in container.streams if stream.type == "video"), None)
        if video is None:
            raise ValueError("映像を読み取れません。")
        codec = video.codec_context
        if str(getattr(codec, "color_trc", "")).lower() in {"smpte2084", "arib-std-b67"} or getattr(codec, "color_trc", None) in (16, 18):
            raise ValueError("HDR素材はこの試作では扱えません。")
        width, height = codec.width, codec.height
        if width < 1 or height < 1 or width > 16384 or height > 16384:
            raise ValueError("素材の寸法を確認してください。")
        kind = "image" if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"} else "video"
        duration_frames = 0
        if kind == "video":
            seconds = float(video.duration * video.time_base) if video.duration is not None else (container.duration or 0) / 1000000
            if not math.isfinite(seconds) or seconds <= 0:
                raise ValueError("動画の長さを読み取れません。")
            duration_frames = math.floor(seconds * FPS)
            if duration_frames < 1:
                raise ValueError("動画が短すぎます。")
        return Source(source_id, path.name, path.stat().st_size, width, height, kind, duration_frames, any(s.type == "audio" for s in container.streams))


class Registry:
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("素材ルートはフォルダーを指定してください。")
        self.entries = {}
        self.selected = {}
        self.sources = {}
        for path in self.root.rglob("*"):
            if path.suffix.lower() not in ALLOWED or not path.is_file() or path.is_symlink():
                continue
            if not path.resolve().is_relative_to(self.root):
                continue
            self.entries.setdefault((path.name, *fingerprint(path)), []).append(path)

    def match(self, descriptors):
        if not isinstance(descriptors, list) or not 1 <= len(descriptors) <= 100:
            raise ValueError("1〜100点の素材を選んでください。")
        matched = []
        paths = {}
        seen = set()
        for descriptor in descriptors:
            if not isinstance(descriptor, dict) or set(descriptor) != {"name", "size", "head", "tail"}:
                raise ValueError("素材の照合情報を確認してください。")
            name, size, head, tail = (descriptor[k] for k in ("name", "size", "head", "tail"))
            if not isinstance(name, str) or not name or len(name) > 255 or "/" in name or "\\" in name or type(size) is not int or size < 1 or size > 100_000_000_000 or not all(isinstance(x, str) and len(x) == 64 and all(c in "0123456789abcdef" for c in x) for x in (head, tail)):
                raise ValueError("素材の照合情報を確認してください。")
            candidates = self.entries.get((name, size, head, tail), [])
            if len(candidates) != 1:
                raise ValueError(f"{name} を設定済みフォルダー内で一意に照合できません。")
            path = candidates[0]
            if path.is_symlink() or not path.resolve().is_relative_to(self.root) or fingerprint(path) != (size, head, tail):
                raise ValueError("原本が変更されました。素材を選び直してください。")
            if path in seen:
                raise ValueError(f"{name} が重複しています。1回だけ選んでください。")
            seen.add(path)
            source_id = secrets.token_urlsafe(12)
            try:
                source = probe(path, source_id)
            except (av.FFmpegError, OSError) as exc:
                raise ValueError(f"{name} を読み取れません。") from exc
            matched.append(source)
            stat = path.stat()
            paths[source_id] = (path, (size, head, tail), (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns))
        self.selected = paths
        self.sources = {source.id: source for source in matched}
        return matched

    def path(self, source_id):
        path, original, identity = self.selected[source_id]
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):
            raise ValueError("原本が変更されました。素材を選び直してください。")
        stat = path.stat()
        current_identity = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns)
        if current_identity != identity or fingerprint(path) != original:
            raise ValueError("原本が変更されました。素材を選び直してください。")
        return path


def verify_output(path, expected_frames):
    with av.open(str(path)) as container:
        video = next((s for s in container.streams if s.type == "video"), None)
        audio = next((s for s in container.streams if s.type == "audio"), None)
        if video is None or audio is None or video.width != 1280 or video.height != 720:
            raise ValueError("書き出し結果を検証できません。")
        frames = sum(1 for _ in container.decode(video=0))
        if frames != expected_frames:
            raise ValueError("書き出しの長さが一致しません。")
        duration = float(audio.duration * audio.time_base) if audio.duration is not None else 0
        if abs(duration - expected_frames / FPS) > 0.1:
            raise ValueError("書き出しの音声長が一致しません。")


def render(plan, registry, work_dir, ffmpeg):
    scenes = [scene for scene in plan.scenes if scene.included]
    if not scenes:
        raise ValueError("採用した場面がありません。少なくとも1場面を採用してください。")
    if plan.frame_count > 3600:
        raise ValueError("この試作では120秒まで書き出せます。")
    work = Path(work_dir).resolve()
    work.mkdir(parents=True, exist_ok=True)
    args = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    filters = []
    inputs = []
    input_index = 0
    for scene in scenes:
        path = registry.path(scene.source_id)
        if scene.kind == "image":
            args += ["-loop", "1", "-framerate", str(FPS), "-t", f"{scene.length / FPS:.6f}", "-protocol_whitelist", "file,pipe", "-i", str(path)]
        else:
            args += ["-ss", f"{scene.start_frame / FPS:.6f}", "-t", f"{scene.length / FPS:.6f}", "-protocol_whitelist", "file,pipe", "-i", str(path)]
        video_index = input_index
        input_index += 1
        source = registry.sources[scene.source_id]
        if source.has_audio and scene.kind == "video":
            audio_index = video_index
        else:
            args += ["-f", "lavfi", "-t", f"{scene.length / FPS:.6f}", "-i", "anullsrc=r=48000:cl=stereo"]
            audio_index = input_index
            input_index += 1
        index = len(inputs)
        filters.append(f"[{video_index}:v]scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,tpad=stop_mode=clone:stop_duration=1,trim=end_frame={scene.length},setpts=PTS-STARTPTS,format=yuv420p[v{index}]")
        filters.append(f"[{audio_index}:a]aresample=48000,apad,atrim=duration={scene.length / FPS:.6f},asetpts=PTS-STARTPTS[a{index}]")
        inputs.append(f"[v{index}][a{index}]")
    filters.append("".join(inputs) + f"concat=n={len(inputs)}:v=1:a=1[v][a]")
    artifact = secrets.token_urlsafe(18)
    target = work / f"{artifact}.mp4"
    with tempfile.NamedTemporaryFile(prefix="render-", suffix=".mp4", dir=work, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        args += ["-filter_complex", ";".join(filters), "-filter_complex_threads", "2", "-map", "[v]", "-map", "[a]", "-frames:v", str(plan.frame_count), "-c:v", "libx264", "-threads", "2", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", str(temporary)]
        result = subprocess.run(args, capture_output=True, timeout=240, check=False)
        if result.returncode:
            raise ValueError("動画を書き出せませんでした。素材の形式と空き容量を確認してください。")
        for scene in scenes:
            registry.path(scene.source_id)
        verify_output(temporary, plan.frame_count)
        os.replace(temporary, target)
        manifest = {"artifact_path": str(target), "plan": asdict(plan), "sources": [asdict(source) for source in registry.sources.values()]}
        with tempfile.NamedTemporaryFile(prefix="manifest-", suffix=".json", dir=work, mode="w", encoding="utf-8", delete=False) as handle:
            json.dump(manifest, handle, ensure_ascii=False)
            manifest_temp = Path(handle.name)
        os.replace(manifest_temp, work / f"{artifact}.json")
        return artifact, target
    except subprocess.TimeoutExpired as exc:
        raise ValueError("書き出しが4分を超えました。素材を減らして再試行してください。") from exc
    except (av.FFmpegError, OSError) as exc:
        raise ValueError("書き出し結果を確認できません。素材の形式と空き容量を確認してください。") from exc
    finally:
        temporary.unlink(missing_ok=True)
