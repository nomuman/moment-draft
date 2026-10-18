import json
import os
from dataclasses import asdict
from fractions import Fraction
from pathlib import Path
import subprocess
import tempfile
import unittest

import av
import numpy as np
from PIL import Image

from moment_draft.media import Cancelled, render, verify_output
from moment_draft.timeline import EditPlan, Scene, Source


FFMPEG = os.environ.get("MOMENT_FFMPEG", "ffmpeg")


class RenderMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.video = self.root / "video.mp4"
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "color=c=blue:s=48x96:r=60:d=2",
                        "-f", "lavfi", "-i", "color=c=green:s=48x96:r=60:d=2",
                        "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=4",
                        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-map", "2:a",
                        "-c:v", "libx264", "-threads", "2", "-pix_fmt", "yuv420p", "-c:a", "aac", str(self.video)], check=True)
        self.image = self.root / "image.png"
        Image.new("RGB", (48, 96), "red").save(self.image)
        self.sources = [Source("video", "video", 1, 48, 96, "video", 120, True),
                        Source("image", "image", 1, 48, 96, "image")]
        self.paths = {"video": self.video, "image": self.image}

    def snapshot(self, scenes):
        return {"plan": asdict(EditPlan(1, 1, tuple(scenes))),
                "assets": [{"id": source.id, "metadata": json.dumps(asdict(source))} for source in self.sources],
                "music": {"id": "off", "level": .2}}

    def test_uneven_scene_boundaries_preserve_frames_pts_padding_and_sound(self):
        scenes = [Scene("a", "video", "video", True, start_frame=30, end_frame=61),
                  Scene("b", "image", "image", True, duration_frames=7),
                  Scene("c", "video", "video", True, start_frame=65, end_frame=94)]
        progress = []
        work = self.root / "work"
        result = render(self.snapshot(scenes), self.paths, work, FFMPEG, lambda: True, progress.append, 60)
        with av.open(str(result)) as container:
            frames = list(container.decode(video=0))
        self.assertEqual(len(frames), 67)
        self.assertEqual([frame.pts * frame.time_base for frame in frames], [Fraction(i, 30) for i in range(67)])
        for index in (0, 29):
            self.assertGreater(frames[index].to_ndarray(format="rgb24")[360, 640, 2], 180)
        for index in (31, 37):
            self.assertGreater(frames[index].to_ndarray(format="rgb24")[360, 640, 0], 180)
        for index in (30, 38, 66):
            pixels = frames[index].to_ndarray(format="rgb24")
            self.assertGreater(pixels[360, 640, 1], 90)
            self.assertLess(pixels[360, 100].max(), 25)
        with av.open(str(result)) as container:
            audio = container.streams.audio[0]
            self.assertAlmostEqual(float(audio.duration * audio.time_base), 67 / 30, delta=1 / 48000)
            sound = np.concatenate([frame.to_ndarray() for frame in container.decode(audio=0)], axis=1)
        self.assertGreater(np.abs(sound[:, 10000:40000]).mean(), .01)
        self.assertLess(np.abs(sound[:, 32 * 1600:36 * 1600]).mean(), .002)
        self.assertGreater(np.abs(sound[:, 40 * 1600:65 * 1600]).mean(), .01)
        self.assertEqual(progress, sorted(progress))
        self.assertLessEqual(max(progress), .94)
        self.assertFalse(list(work.glob("scenes-*")))

    def test_twenty_short_scenes_keep_continuous_original_sound_with_music(self):
        scenes = [Scene(str(i), "video", "video", True, start_frame=i, end_frame=i + 7) for i in range(20)]
        music = self.root / "music.wav"
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "sine=frequency=220:sample_rate=48000:duration=1", str(music)], check=True)
        result = render(self.snapshot(scenes), self.paths, self.root / "work", FFMPEG,
                        lambda: True, lambda value: None, 90, music_path=music)
        with av.open(str(result)) as container:
            frames = list(container.decode(video=0))
        self.assertEqual(len(frames), 140)
        self.assertEqual([frame.pts * frame.time_base for frame in frames], [Fraction(i, 30) for i in range(140)])
        with av.open(str(result)) as container:
            audio = container.streams.audio[0]
            self.assertAlmostEqual(float(audio.duration * audio.time_base), 140 / 30, delta=1 / 48000)
            sound = np.concatenate([frame.to_ndarray() for frame in container.decode(audio=0)], axis=1).mean(axis=0)
        for boundary in range(1, 20):
            offset = boundary * 7 * 1600
            self.assertGreater(np.abs(sound[offset - 480:offset + 480]).mean(), .01)
        self.assertGreater(np.abs(sound[139 * 1600:140 * 1600]).mean(), .01)
        section = sound[48000:96000]
        spectrum = np.abs(np.fft.rfft(section))
        self.assertGreater(spectrum[880], 500)
        self.assertGreater(spectrum[220], 50)

    def test_cancel_between_scenes_removes_intermediates(self):
        scenes = [Scene(str(i), "video", "video", True, start_frame=0, end_frame=7) for i in range(3)]
        values = []
        work = self.root / "work"
        with self.assertRaises(Cancelled):
            render(self.snapshot(scenes), self.paths, work, FFMPEG,
                   lambda: not values or values[-1] < .28, values.append, 60)
        self.assertFalse((work / "output.mp4").exists())
        self.assertFalse(list(work.glob("scenes-*")))

    def test_output_verification_rejects_audio_ending_before_video(self):
        output = self.root / "short-audio.mp4"
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(self.video),
                        "-vf", "scale=1280:720,fps=30", "-af", "atrim=end_sample=189600",
                        "-c:v", "libx264", "-preset", "ultrafast", "-threads", "2", "-c:a", "aac", str(output)], check=True)
        with self.assertRaisesRegex(ValueError, "音声長"):
            verify_output(output, 120)


if __name__ == "__main__":
    unittest.main()
