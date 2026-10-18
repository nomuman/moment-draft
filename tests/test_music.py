import hashlib
import os
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from moment_draft.music import catalog, ensure_track


class MusicTests(unittest.TestCase):
    def test_catalog_and_generated_audio(self):
        tracks = catalog()
        self.assertEqual({track["id"] for track in tracks}, {"calm", "light", "reflective"})
        self.assertTrue(all(set(track) == {"id", "name", "description", "license"} for track in tracks))
        with tempfile.TemporaryDirectory() as directory:
            for track in tracks:
                with self.subTest(track=track["id"]):
                    path = ensure_track(track["id"], directory)
                    with wave.open(str(path), "rb") as wav:
                        self.assertEqual((wav.getframerate(), wav.getnchannels(), wav.getsampwidth()), (48_000, 2, 2))
                        self.assertGreaterEqual(wav.getnframes() / wav.getframerate(), 60)
                        self.assertLessEqual(wav.getnframes() / wav.getframerate(), 90)
                        samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").reshape(-1, 2)
                    audio = samples.astype(np.float32) / 32768
                    peak = np.max(np.abs(audio))
                    rms = np.sqrt(np.mean(audio**2))
                    self.assertGreater(peak, 0.1)
                    self.assertLess(peak, 0.85)
                    self.assertGreater(rms, 0.01)
                    self.assertLess(rms, 0.3)
                    self.assertGreater(np.sqrt(np.mean((audio[:, 0] - audio[:, 1]) ** 2)), 0.001)
                    middle = audio[len(audio) // 3:2 * len(audio) // 3]
                    self.assertLess(np.sqrt(np.mean(audio[:24_000] ** 2)), np.sqrt(np.mean(middle**2)) * 0.6)
                    self.assertLess(np.sqrt(np.mean(audio[-24_000:] ** 2)), np.sqrt(np.mean(middle**2)) * 0.4)

    def test_repeatable_pcm_and_cache_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            first = ensure_track("calm", Path(directory) / "first")
            expected_hash = hashlib.sha256(first.read_bytes()).hexdigest()
            os.utime(first, (1_000_000_000, 1_000_000_000))
            self.assertEqual(ensure_track("calm", first.parent), first)
            self.assertEqual(first.stat().st_mtime, 1_000_000_000)
            second = ensure_track("calm", Path(directory) / "second")
            self.assertEqual(hashlib.sha256(second.read_bytes()).hexdigest(), expected_hash)
            first.write_bytes(first.read_bytes()[:1000])
            self.assertEqual(ensure_track("calm", first.parent), first)
            self.assertEqual(hashlib.sha256(first.read_bytes()).hexdigest(), expected_hash)
            with first.open("r+b") as file:
                file.write(b"not a wave")
            self.assertEqual(ensure_track("calm", first.parent), first)
            self.assertEqual(hashlib.sha256(first.read_bytes()).hexdigest(), expected_hash)

    def test_rejects_unrecognized_ids_before_creating_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "cache"
            for track_id in ("off", "../calm", "/tmp/track", "CALM", "", None):
                with self.subTest(track_id=track_id):
                    with self.assertRaises(ValueError):
                        ensure_track(track_id, cache)
            self.assertFalse(cache.exists())


if __name__ == "__main__":
    unittest.main()
