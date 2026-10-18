"""Measure render RSS on operator-provided media; keep reports outside version control."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import threading
import time


class ProcessUsage(ctypes.Structure):
    _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [
        (name, ctypes.c_uint64) for name in (
            "user_time", "system_time", "package_idle_wakeups", "interrupt_wakeups",
            "pageins", "wired_size", "resident_size", "physical_footprint",
            "process_start", "process_exit", "child_user_time", "child_system_time",
            "child_package_idle_wakeups", "child_interrupt_wakeups", "child_pageins",
            "child_elapsed", "disk_read_bytes", "disk_write_bytes")]


if sys.platform == "darwin":
    process_usage = ctypes.CDLL("/usr/lib/libproc.dylib").proc_pid_rusage
    process_usage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    process_usage.restype = ctypes.c_int
else:
    process_usage = None


def footprint(pid):
    usage = ProcessUsage()
    if process_usage is None or process_usage(pid, 2, ctypes.byref(usage)):
        return 0
    return usage.physical_footprint // 1024


def sample_tree(pid):
    rows = subprocess.check_output(["ps", "-axo", "pid=,ppid=,rss=,comm="], text=True)
    processes = [line.split(None, 3) for line in rows.splitlines()]
    family = {pid}
    while True:
        children = {int(row[0]) for row in processes if int(row[1]) in family}
        if children <= family:
            break
        family |= children
    members = [row for row in processes if int(row[0]) in family]
    native = [row for row in members if "ffmpeg" in row[3]]
    footprints = {int(row[0]): footprint(int(row[0])) for row in members}
    return (sum(int(row[2]) for row in native), sum(int(row[2]) for row in members),
            sum(footprints[int(row[0])] for row in native), sum(footprints.values()))


def run(args):
    sys.path.insert(0, str(args.repo.resolve()))
    import av
    from dataclasses import asdict
    from moment_draft import media
    from moment_draft.timeline import EditPlan, Scene, Source

    with av.open(str(args.input)) as container:
        video = container.streams.video[0]
        duration = float(video.duration * video.time_base)
        source = Source("source", "input", args.input.stat().st_size,
                        video.width, video.height, "video", int(duration * 30),
                        bool(container.streams.audio))
    starts = [float(value) for value in args.starts.split(",")]
    scenes = tuple(Scene(f"scene-{i}", "source", "video", True,
                         start_frame=round(starts[i % len(starts)] * 30),
                         end_frame=round(starts[i % len(starts)] * 30) + args.frames)
                   for i in range(args.count))
    snapshot = {"plan": asdict(EditPlan(1, 1, scenes)),
                "assets": [{"id": "source", "metadata": json.dumps(asdict(source))}],
                "music": {"id": "off", "level": 0}}
    native = media.run_native
    if args.decoder_threads:
        def limited(command, *positional, **keywords):
            changed = []
            for token in command:
                if token == "-i":
                    changed += ["-threads", str(args.decoder_threads)]
                changed.append(token)
            return native(changed, *positional, **keywords)
        media.run_native = limited
    stop = threading.Event()
    peaks = [0, 0, 0, 0]
    samples = 0

    def watch():
        nonlocal samples
        while not stop.is_set():
            values = sample_tree(os.getpid())
            for index, value in enumerate(values):
                peaks[index] = max(peaks[index], value)
            samples += 1
            stop.wait(.05)

    thread = threading.Thread(target=watch)
    report = {"success": False, "expectedFrames": args.count * args.frames,
              "actualFrames": 0, "loadBefore": os.getloadavg(),
              "cpuCount": os.cpu_count(), "platform": sys.platform,
              "ffmpegVersion": subprocess.check_output([args.ffmpeg, "-version"], text=True).splitlines()[0]}
    thread.start()
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="render-measure-", dir=args.work) as directory:
            result = media.render(snapshot, {"source": args.input}, Path(directory),
                                  args.ffmpeg, lambda: True, lambda value: None, args.timeout)
            report["renderSeconds"] = time.monotonic() - started
            with av.open(str(result)) as container:
                report["actualFrames"] = sum(1 for _ in container.decode(video=0))
            report["success"] = report["actualFrames"] == report["expectedFrames"]
            report["outputBytes"] = result.stat().st_size
    except Exception as error:
        report["error"] = type(error).__name__ + ": " + str(error)
    finally:
        stop.set()
        thread.join()
    report.update(sampledNativeRSSPeakKiB=peaks[0], sampledTreeRSSPeakKiB=peaks[1],
                  sampledNativeFootprintPeakKiB=peaks[2] if process_usage else None,
                  sampledTreeFootprintPeakKiB=peaks[3] if process_usage else None,
                  samplingSeconds=.05, samples=samples, elapsedSeconds=time.monotonic() - started)
    print(json.dumps(report), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--baseline-repo", type=Path)
    parser.add_argument("--starts", default="0,100,200,300")
    parser.add_argument("--count", type=int, default=4)
    parser.add_argument("--frames", type=int, default=180)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--decoder-threads", type=int)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if min(args.count, args.frames, args.repeat, args.timeout) <= 0:
        parser.error("count, frames, repeat and timeout must be positive")
    args.work.mkdir(parents=True, exist_ok=True)
    if args.child:
        run(args)
        return
    reports = []
    for iteration in range(args.repeat):
        sides = [("baseline", args.baseline_repo), ("candidate", args.repo)] if args.baseline_repo else [("candidate", args.repo)]
        for label, repo in sides:
            command = [sys.executable, str(Path(__file__).resolve()), "--child", "--input", str(args.input.resolve()),
                       "--ffmpeg", args.ffmpeg, "--repo", str(repo.resolve()), "--starts", args.starts,
                       "--count", str(args.count), "--frames", str(args.frames), "--timeout", str(args.timeout),
                       "--work", str(args.work.resolve())]
            if args.decoder_threads and label == "candidate":
                command += ["--decoder-threads", str(args.decoder_threads)]
            env = dict(os.environ, PYTHONPATH=str(repo.resolve()) + os.pathsep + os.environ.get("PYTHONPATH", ""))
            child = subprocess.run(command, capture_output=True, text=True, env=env)
            report = json.loads(child.stdout) if child.returncode == 0 else {"success": False, "error": child.stderr}
            report.update(side=label, iteration=iteration + 1)
            reports.append(report)
            print(json.dumps(report), flush=True)
            if args.report:
                args.report.write_text(json.dumps(reports, indent=2))
    for label in dict.fromkeys(report["side"] for report in reports):
        selected = [report for report in reports if report["side"] == label]
        summary = {"side": label, "successes": sum(report["success"] for report in selected),
                   "errors": sum(not report["success"] for report in selected)}
        for key in ("renderSeconds", "sampledNativeRSSPeakKiB", "sampledTreeRSSPeakKiB", "sampledNativeFootprintPeakKiB", "sampledTreeFootprintPeakKiB"):
            values = [report[key] for report in selected if report["success"] and report.get(key) is not None]
            if values:
                summary[key] = {"median": statistics.median(values), "min": min(values), "max": max(values)}
        print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
