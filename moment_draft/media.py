import json
import math
import os
import select
import signal
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

import av

from .timeline import FPS, Source, from_dict


class Cancelled(Exception):
    pass


def probe(path, source_id, name):
    with path.open("rb") as handle:
        header = handle.read(32)
    image = header.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")) or (header[:4] == b"RIFF" and header[8:12] == b"WEBP")
    movie = header[4:8] in {b"ftyp", b"wide", b"mdat", b"moov"}
    if not image and not movie:
        raise ValueError("対応していない素材形式です。")
    with av.open(str(path), options={"protocol_whitelist": "file,pipe"}) as container:
        video = next((s for s in container.streams if s.type == "video"), None)
        if video is None:
            raise ValueError("映像を読み取れません。")
        codec = video.codec_context
        if getattr(codec, "color_trc", None) in (16, 18):
            raise ValueError("HDR素材はまだ扱えません。")
        if not 0 < codec.width <= 16384 or not 0 < codec.height <= 16384 or codec.width * codec.height > 100_000_000:
            raise ValueError("素材の寸法が上限を超えています。")
        duration_frames = 0
        if not image:
            seconds = float(video.duration * video.time_base) if video.duration is not None else (container.duration or 0) / 1_000_000
            if not math.isfinite(seconds) or not 1 / FPS <= seconds <= 3600:
                raise ValueError("動画は1時間以内で選んでください。")
            duration_frames = math.floor(seconds * FPS)
        next(container.decode(video=0))
        return asdict(Source(source_id, name, path.stat().st_size, codec.width, codec.height, "image" if image else "video", duration_frames, any(s.type == "audio" for s in container.streams)))


def inspect(path, source_id, name, timeout):
    result = subprocess.run([sys.executable, "-m", "moment_draft.media", "probe", str(path), source_id, name], capture_output=True, timeout=timeout, check=True)
    return json.loads(result.stdout)


def verify_output(path, expected_frames, active=lambda: True):
    with av.open(str(path)) as container:
        video = next((s for s in container.streams if s.type == "video"), None)
        audio = next((s for s in container.streams if s.type == "audio"), None)
        if video is None or audio is None or video.width != 1280 or video.height != 720:
            raise ValueError("書き出し結果を検証できません。")
        frames = 0
        for _ in container.decode(video=0):
            frames += 1
            if frames % 30 == 0 and not active():
                raise Cancelled()
        if frames != expected_frames:
            raise ValueError("書き出しの長さが一致しません。")
        duration = float(audio.duration * audio.time_base) if audio.duration is not None else 0
        if abs(duration - expected_frames / FPS) > .1:
            raise ValueError("書き出しの音声長が一致しません。")


def supervised(args):
    process = subprocess.Popen(args, stdin=subprocess.DEVNULL)
    try:
        while process.poll() is None:
            readable, _, _ = select.select([sys.stdin.buffer], [], [], .2)
            if readable and not os.read(sys.stdin.fileno(), 1):
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                return 1
        return process.returncode
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


def run_native(args, work, active, deadline, progress=None):
    spec = work / "command.json"
    spec.write_text(json.dumps(args))
    with (work / "native.log").open("ab") as log:
        process = subprocess.Popen([sys.executable, "-m", "moment_draft.media", "supervise", str(spec)], stdin=subprocess.PIPE, stdout=log, stderr=log, start_new_session=True)
        try:
            while process.poll() is None:
                if not active():
                    raise Cancelled()
                if time.monotonic() >= deadline:
                    raise TimeoutError("書き出しが時間上限を超えました。")
                if progress:
                    progress()
                time.sleep(.15)
            if process.returncode:
                raise ValueError("映像の処理に失敗しました。")
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            process.stdin.close()


def render(snapshot, paths, work, ffmpeg, active, progress, timeout, music_path=None):
    plan = from_dict(snapshot["plan"])
    sources = {asset["id"]: Source(**json.loads(asset["metadata"])) for asset in snapshot["assets"]}
    work.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    base = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    scaled = {}
    for asset, source in sources.items():
        if source.kind == "image":
            scaled[asset] = work / (asset + ".png")
            run_native(base + ["-protocol_whitelist", "file,pipe", "-i", str(paths[asset]), "-vf", "scale=1280:720:force_original_aspect_ratio=decrease:force_divisible_by=2,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1", "-frames:v", "1", "-threads", "2", str(scaled[asset])], work, active, deadline)
    args, filters, inputs = list(base), [], []
    input_index = 0
    for scene in (s for s in plan.scenes if s.included):
        duration = f"{scene.length / FPS:.6f}"
        if scene.kind == "image":
            args += ["-loop", "1", "-framerate", "30", "-t", duration, "-protocol_whitelist", "file,pipe", "-i", str(scaled[scene.source_id])]
        else:
            args += ["-ss", f"{scene.start_frame / FPS:.6f}", "-t", duration, "-protocol_whitelist", "file,pipe", "-i", str(paths[scene.source_id])]
        video_index = input_index
        input_index += 1
        if sources[scene.source_id].has_audio and scene.kind == "video":
            audio_index = video_index
        else:
            args += ["-f", "lavfi", "-t", duration, "-i", "anullsrc=r=48000:cl=stereo"]
            audio_index = input_index
            input_index += 1
        index = len(inputs)
        filters.append(f"[{video_index}:v]scale=1280:720:force_original_aspect_ratio=decrease:force_divisible_by=2,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,tpad=stop_mode=clone:stop_duration=1,trim=end_frame={scene.length},setpts=PTS-STARTPTS,format=yuv420p[v{index}]")
        filters.append(f"[{audio_index}:a]aresample=48000,apad,atrim=duration={duration},asetpts=PTS-STARTPTS[a{index}]")
        inputs.append(f"[v{index}][a{index}]")
    filters.append("".join(inputs) + f"concat=n={len(inputs)}:v=1:a=1[v][original]")
    if music_path:
        args += ["-stream_loop", "-1", "-protocol_whitelist", "file,pipe", "-i", str(music_path)]
        duration = plan.frame_count / FPS
        level = snapshot["music"]["level"]
        filters += [f"[{input_index}:a]aresample=48000,atrim=duration={duration:.6f},asetpts=PTS-STARTPTS,volume={level:.4f},afade=t=in:d=1,afade=t=out:st={max(0, duration - 2):.6f}:d=2[bgm]", "[original][bgm]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95:level=false[a]"]
    else:
        filters.append("[original]anull[a]")
    target, progress_file = work / "output.mp4", work / "progress.txt"
    args += ["-filter_complex", ";".join(filters), "-filter_complex_threads", "2", "-map", "[v]", "-map", "[a]", "-frames:v", str(plan.frame_count), "-c:v", "libx264", "-threads", "2", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", "-progress", str(progress_file), str(target)]

    def update():
        if progress_file.exists():
            values = [line.partition("=")[2] for line in progress_file.read_text().splitlines() if line.startswith("out_time_us=")]
            if values and values[-1].isdigit():
                progress(int(values[-1]) / 1_000_000 / (plan.frame_count / FPS) * .94)

    run_native(args, work, active, deadline, update)
    verify_output(target, plan.frame_count, active)
    return target


if __name__ == "__main__":
    if sys.argv[1] == "probe":
        print(json.dumps(probe(Path(sys.argv[2]), sys.argv[3], sys.argv[4])))
    elif sys.argv[1] == "supervise":
        raise SystemExit(supervised(json.loads(Path(sys.argv[2]).read_text())))
