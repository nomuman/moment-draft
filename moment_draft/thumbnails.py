import time

from .media import run_native


def create_thumbnail(source, frame, work, ffmpeg, active, timeout, lease_fd):
    work.mkdir(parents=True, exist_ok=True)
    target = work / "preview.jpg"
    args = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-threads", "2", "-protocol_whitelist", "file,pipe"]
    if frame:
        args += ["-ss", f"{frame / 30:.6f}"]
    args += ["-i", str(source), "-vf",
             "scale=480:270:force_original_aspect_ratio=decrease:force_divisible_by=2,pad=480:270:(ow-iw)/2:(oh-ih)/2,setsar=1",
             "-frames:v", "1", "-threads", "2", "-q:v", "4", str(target)]
    run_native(args, work, active, time.monotonic() + timeout, lease_fd=lease_fd)
    return target
