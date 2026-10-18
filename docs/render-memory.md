# Render memory

`render` normalizes one selected scene at a time to 1280 × 720 at 30 fps. Each MOV contains H.264 video and stereo 48 kHz PCM audio. The final concat reads these normalized files in order, copies video, and encodes audio to AAC once. Background music is mixed only in that final pass.

The former filter graph opened every original decoder together. A trial that limited decoder threads to two still reached about 10 GiB of macOS physical footprint on 12 windows of 4K 60 fps, 10-bit SDR HEVC. The operator stopped that trial. Its sampled RSS peak was only 2,186,560 KiB because macOS compressed most pages. RSS alone is insufficient to establish memory headroom on that host.

Source validation also caps the PyAV decoder at two threads before decoding its first frame.

The sequential design keeps one original decoder alive, with two decoder threads, two filter threads, and two encoder threads. Increasing scene count adds normalized files and elapsed work instead of simultaneous original decoders. The files occupy only the selected 720p windows. Temporary scene files are removed after success, cancellation, and failure.

The public `render` signature and inherited lease remain unchanged. Every native command uses the same cancellation callback and deadline. Progress assigns 85 percent to scene normalization in proportion to scene length, then 9 percent to concat. Final output validation still decodes every video frame and checks audio presence and duration.

PCM intermediates avoid an AAC priming delay at every scene boundary. Explicit scene durations preserve concat placement. MOV preserves video timestamps at 30 fps, and a 48 kHz movie timescale preserves audio duration to a sample. Tests cover uneven scene lengths, exact decoded frame counts and presentation timestamps, portrait padding, silent images, original audio with background music, twenty scene boundaries, and cancellation cleanup.

## Measurement procedure

Use `scripts/measure_render.py` with an operator-provided local input and native FFmpeg. It does not upload or copy the input. Each run uses a fresh Python process, renders into a temporary directory, decodes the output again to count actual frames, and removes that directory. Store reports under an ignored local directory.

```sh
python3 scripts/measure_render.py \
  --input "$SOURCE" --ffmpeg "$FFMPEG" \
  --count 20 --frames 90 --starts 0,100,200,300 \
  --repeat 1 --work .local/render-measure \
  --report .local/render-stress.json
```

This example selects twenty three-second windows, producing 1,800 frames and 60 seconds of output. Use `--frames 180` for 120 seconds. The supplied start positions must fit the source. A single run is a capacity profile, not a benchmark.

To reproduce the safe comparison fixture, generate it locally with the same FFmpeg binary.

```sh
"$FFMPEG" -hide_banner -loglevel error -y \
  -f lavfi -i testsrc2=s=640x360:r=60:d=8 \
  -f lavfi -i sine=frequency=880:sample_rate=48000:duration=8 \
  -c:v libx265 -pix_fmt yuv420p10le -threads 2 \
  -x265-params pools=1:frame-threads=2:log-level=error \
  -c:a aac .local/render-fixture.mp4
python3 scripts/measure_render.py \
  --input .local/render-fixture.mp4 --ffmpeg "$FFMPEG" \
  --baseline-repo "$BASELINE_CHECKOUT" \
  --count 4 --frames 30 --starts 0,1,2,3 --repeat 5 \
  --work .local/render-measure --report .local/render-comparison.json
```

For an alternating comparison, supply a checkout of the baseline with `--baseline-repo` and use `--repeat 5`. The order is baseline, candidate, baseline, candidate. The optional `--decoder-threads 2` applies only to the candidate and supports testing the smaller thread-only change without modifying production code.

The script reports successes, errors, actual and expected frames, render time, RSS peaks, and the machine load. On macOS it also samples `proc_pid_rusage` physical footprint, including compressed memory, for the native process and its Python process tree. Sampling has a nominal 50 ms interval plus process-list overhead, so peaks are sampled lower bounds. Linux RSS and macOS physical footprint are different measurements. A Linux container test must check the cgroup peak and OOM events separately.

## Evidence and limits

The actual-source stress rendered twenty six-second windows of 4K 60 fps, 10-bit SDR HEVC. It decoded all 3,600 output frames with zero errors. Sampled native physical footprint peaked at 762,225 KiB, about 744 MiB. The render process tree peaked at 805,553 KiB, about 787 MiB. Native and process-tree RSS peaks were 777,680 KiB and 858,704 KiB. This is one capacity profile, with 3,477 samples, not a benchmark. It took 511.64 seconds under host load and produced 24,099,905 bytes before cleanup. The 120-second limit here describes output duration, not a render-latency target.

The first sequential four-window profile decoded all 720 frames. Native RSS peaked at 774,784 KiB and the sampled process tree at 871,328 KiB. An independent live check found about 725 MiB native physical footprint with no compressed pages. This was one profile. Its 118.9-second wall time is not comparable with the earlier 29.3-second baseline profile because host load and memory pressure changed.

A safe generated fixture used four one-second windows of 640 × 360, 60 fps, 10-bit HEVC. Five alternating runs per side all succeeded, with 120 actual frames per run and zero errors. Median sampled native physical footprint was 300,656 KiB before and 124,544 KiB after. The respective ranges were 300,432 to 300,848 KiB and 122,992 to 125,296 KiB. Median native RSS was 315,440 KiB before and 137,504 KiB after. This small fixture validates the repeated measurement and memory reduction, not 4K capacity.

The synthetic render time increased from a median 2.028 seconds to 4.079 seconds. Ranges were 1.557 to 2.393 seconds before and 3.665 to 4.221 seconds after. Sequential normalization adds native process launches and removes parallel scene decoding. The actual-media stress ran concurrently with this comparison, and host load was high, so these timings cannot predict production latency.

No speedup is claimed. The 10-core development host was busy with unrelated applications and experienced memory compression during the rejected thread-only trial. Do not repeat that unsafe trial merely to produce a median. The original private input and detailed run reports remain local.

A separate single probe of that source succeeded with a process maximum RSS of 102,383,616 bytes after the two-thread cap.

These measurements do not establish a whole-service 2 GiB guarantee. That gate needs the integrated service, serialized media operations, the intended Linux image, and cgroup measurement with twenty 4K 10-bit scenes. Browser upload, deployment, and real-user evidence are separate checks.
