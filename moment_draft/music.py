"""Original, deterministic instrumental music for local video drafts."""

from __future__ import annotations

import math
import os
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np


SAMPLE_RATE = 48_000
_CHANNELS = 2
_SAMPLE_WIDTH = 2


@dataclass(frozen=True)
class _Track:
    id: str
    name: str
    description: str
    bpm: int
    chords: tuple[tuple[int, tuple[int, ...]], ...]
    melody: tuple[tuple[tuple[float, int], ...], ...]
    color: str

    @property
    def bars(self) -> int:
        return len(self.melody)

    @property
    def frames(self) -> int:
        return round(self.bars * 4 * 60 * SAMPLE_RATE / self.bpm)


_CALM = _Track(
    "calm",
    "穏やか",
    "柔らかな鍵盤と広がる和音。落ち着いた場面向け。",
    80,
    (
        (48, (60, 64, 67, 71)), (45, (57, 60, 64, 67)),
        (41, (53, 57, 60, 64)), (43, (55, 59, 62, 69)),
        (40, (52, 55, 59, 62)), (45, (57, 60, 64, 67)),
        (38, (50, 53, 57, 64)), (43, (55, 59, 62, 69)),
    ),
    (
        ((0.5, 76), (2.5, 74)), ((1.0, 72), (3.0, 71)),
        ((0.5, 69), (2.0, 72)), ((1.5, 74),),
        ((0.5, 71), (2.5, 69)), ((1.0, 72), (3.0, 76)),
        ((0.5, 74), (2.0, 72)), ((1.5, 71),),
        ((0.5, 72), (2.5, 76)), ((1.0, 79), (3.0, 76)),
        ((0.5, 72), (2.0, 69)), ((1.5, 74),),
        ((0.5, 74), (2.5, 71)), ((1.0, 72), (3.0, 76)),
        ((0.5, 77), (2.0, 76)), ((1.5, 74),),
        ((0.5, 76), (2.5, 74)), ((1.0, 72), (3.0, 67)),
        ((0.5, 69), (2.0, 72)), ((1.5, 74),),
        ((0.5, 71), (2.5, 74)), ((1.0, 72), (3.0, 69)),
        ((0.5, 72), (2.0, 69)), ((1.5, 67),),
    ),
    "soft",
)

_LIGHT = _Track(
    "light",
    "軽やか",
    "弾む撥弦の音と明るい旋律。動きのある場面向け。",
    92,
    (
        (48, (60, 64, 67, 72)), (43, (55, 59, 62, 67)),
        (45, (57, 60, 64, 69)), (41, (53, 57, 60, 65)),
        (38, (50, 53, 57, 62)), (40, (52, 55, 59, 64)),
        (41, (53, 57, 60, 65)), (43, (55, 59, 62, 67)),
    ),
    (
        ((0.0, 76), (1.5, 79), (3.0, 76)), ((0.5, 74), (2.0, 71)),
        ((0.0, 72), (1.5, 76), (3.0, 81)), ((0.5, 77), (2.0, 76)),
        ((0.0, 74), (1.5, 77)), ((0.5, 76), (2.0, 79)),
        ((0.0, 81), (1.5, 77), (3.0, 76)), ((0.5, 74), (2.0, 71)),
        ((0.0, 79), (1.5, 76), (3.0, 72)), ((0.5, 74), (2.0, 79)),
        ((0.0, 81), (1.5, 76), (3.0, 72)), ((0.5, 77), (2.0, 81)),
        ((0.0, 77), (1.5, 74)), ((0.5, 79), (2.0, 76)),
        ((0.0, 81), (1.5, 84), (3.0, 81)), ((0.5, 79), (2.0, 74)),
        ((0.0, 76), (1.5, 72), (3.0, 79)), ((0.5, 74), (2.0, 71)),
        ((0.0, 72), (1.5, 76), (3.0, 79)), ((0.5, 77), (2.0, 72)),
        ((0.0, 74), (1.5, 77)), ((0.5, 79), (2.0, 76)),
        ((0.0, 77), (1.5, 74), (3.0, 72)), ((0.5, 76), (2.0, 79)),
        ((0.0, 81), (1.5, 76), (3.0, 72)), ((0.5, 77), (2.0, 76)),
        ((0.0, 74), (1.5, 72)), ((0.5, 79), (2.0, 76)),
        ((0.0, 76), (1.5, 72), (3.0, 67)), ((0.5, 71), (2.0, 67)),
    ),
    "bright",
)

_REFLECTIVE = _Track(
    "reflective",
    "余韻",
    "静かな低音とゆっくりした旋律。振り返る場面向け。",
    72,
    (
        (38, (50, 53, 57, 64)), (46, (58, 62, 65, 69)),
        (41, (53, 57, 60, 64)), (36, (48, 52, 55, 62)),
        (43, (55, 58, 62, 65)), (38, (50, 53, 57, 64)),
        (46, (58, 62, 65, 69)), (45, (57, 60, 64, 69)),
    ),
    (
        ((1.0, 69), (3.0, 72)), ((0.5, 70), (2.5, 69)),
        ((1.0, 72), (3.0, 76)), ((0.5, 74),),
        ((1.0, 70), (3.0, 69)), ((0.5, 65), (2.5, 69)),
        ((1.0, 70), (3.0, 74)), ((0.5, 72),),
        ((1.0, 77), (3.0, 74)), ((0.5, 72), (2.5, 70)),
        ((1.0, 69), (3.0, 72)), ((0.5, 67),),
        ((1.0, 70), (3.0, 74)), ((0.5, 77), (2.5, 74)),
        ((1.0, 72), (3.0, 70)), ((0.5, 69),),
        ((1.0, 69), (3.0, 72)), ((0.5, 74), (2.5, 70)),
        ((1.0, 72), (3.0, 69)), ((0.5, 67),),
        ((1.0, 70), (3.0, 69)), ((0.5, 65), (2.5, 69)),
        ((1.0, 70), (3.0, 74)), ((0.5, 72),),
    ),
    "dark",
)

_TRACKS = {track.id: track for track in (_CALM, _LIGHT, _REFLECTIVE)}


def catalog() -> list[dict[str, str]]:
    return [
        {"id": track.id, "name": track.name, "description": track.description,
         "license": "CC0-1.0"}
        for track in _TRACKS.values()
    ]


def _frequency(note: int) -> float:
    return 440.0 * 2.0 ** ((note - 69) / 12.0)


def _add_note(
    audio: np.ndarray,
    start: int,
    length: int,
    note: int,
    gain: float,
    pan: float,
    voice: str,
) -> None:
    stop = min(start + length, len(audio))
    if stop <= start:
        return
    time = np.arange(stop - start, dtype=np.float64) / SAMPLE_RATE
    phase = 2 * math.pi * _frequency(note) * time
    if voice == "pad":
        phase += 0.08 * np.sin(2 * math.pi * 0.37 * time)
        sound = np.sin(phase) + 0.22 * np.sin(2 * phase) + 0.06 * np.sin(3 * phase)
        envelope = np.minimum(1.0, time / 0.35)
        release = 0.38
    elif voice == "bass":
        sound = np.sin(phase) + 0.18 * np.sin(2 * phase)
        envelope = np.minimum(1.0, time / 0.025) * np.exp(-time / 1.7)
        release = 0.15
    elif voice == "bell":
        sound = np.sin(phase) + 0.26 * np.sin(2.005 * phase)
        envelope = np.minimum(1.0, time / 0.012) * np.exp(-time / 0.7)
        release = 0.12
    else:
        sound = np.sin(phase) + 0.30 * np.sin(2 * phase) + 0.09 * np.sin(3 * phase)
        envelope = np.minimum(1.0, time / 0.015) * np.exp(-time / 0.38)
        release = 0.10
    remaining = (stop - start) / SAMPLE_RATE - time
    envelope *= np.minimum(1.0, remaining / release)
    mono = (sound * envelope * gain).astype(np.float32)
    audio[start:stop, 0] += mono * (1 - 0.32 * pan)
    audio[start:stop, 1] += mono * (1 + 0.32 * pan)


def _render_bar(track: _Track, bar_index: int, frames: int) -> np.ndarray:
    audio = np.zeros((frames, _CHANNELS), dtype=np.float32)
    beat = frames / 4
    root, chord = track.chords[bar_index % len(track.chords)]
    section = bar_index // 8
    for index, pitch in enumerate(chord):
        _add_note(audio, 0, frames, pitch, 0.020, (index - 1.5) / 1.5, "pad")
    for position, pitch in ((0.0, root), (2.0, root + 7)):
        _add_note(audio, round(position * beat), round(1.65 * beat), pitch, 0.080, 0, "bass")
    if track.color == "bright":
        positions = (0.0, 0.5, 1.0, 1.75, 2.0, 2.5, 3.0, 3.5)
        pluck_gain = 0.055
    elif track.color == "dark":
        positions = (0.0, 1.0, 2.0, 3.0)
        pluck_gain = 0.039
    else:
        positions = (0.0, 0.75, 1.5, 2.5, 3.25)
        pluck_gain = 0.046
    for index, position in enumerate(positions):
        pitch = chord[(index + section) % len(chord)] + 12
        _add_note(
            audio, round(position * beat), round(0.48 * beat), pitch,
            pluck_gain, -0.65 if index % 2 else 0.65, "pluck",
        )
    for position, pitch in track.melody[bar_index]:
        start = round(position * beat)
        _add_note(audio, start, round(0.85 * beat), pitch, 0.052, -0.2, "bell")
        echo = start + round(0.28 * beat)
        _add_note(audio, echo, round(0.6 * beat), pitch, 0.016, 0.65, "bell")
    return audio


def _valid_cached(path: Path, frames: int) -> bool:
    try:
        if not path.is_file() or path.stat().st_size != 44 + frames * _CHANNELS * _SAMPLE_WIDTH:
            return False
        with wave.open(str(path), "rb") as wav:
            return (
                wav.getnchannels() == _CHANNELS
                and wav.getsampwidth() == _SAMPLE_WIDTH
                and wav.getframerate() == SAMPLE_RATE
                and wav.getnframes() == frames
                and wav.getcomptype() == "NONE"
            )
    except (OSError, EOFError, wave.Error):
        return False


def ensure_track(track_id: str, cache_dir: str | Path) -> Path:
    try:
        track = _TRACKS[track_id]
    except (KeyError, TypeError) as error:
        raise ValueError("unknown music track") from error
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    output = cache / f"{track.id}.wav"
    if _valid_cached(output, track.frames):
        return output
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(dir=cache, prefix=f".{track.id}-", suffix=".wav", delete=False) as temp:
            temp_name = temp.name
            with wave.open(temp, "wb") as wav:
                wav.setnchannels(_CHANNELS)
                wav.setsampwidth(_SAMPLE_WIDTH)
                wav.setframerate(SAMPLE_RATE)
                frames_written = 0
                for bar_index in range(track.bars):
                    bar_end = round((bar_index + 1) * track.frames / track.bars)
                    frames = bar_end - frames_written
                    audio = _render_bar(track, bar_index, frames)
                    positions = np.arange(frames_written, bar_end, dtype=np.float64)
                    intro = np.minimum(1.0, positions / (0.7 * SAMPLE_RATE))
                    outro = np.minimum(1.0, (track.frames - 1 - positions) / (2.3 * SAMPLE_RATE))
                    audio *= np.minimum(intro, outro)[:, None].astype(np.float32)
                    pcm = np.rint(np.clip(audio, -0.85, 0.85) * 32767).astype("<i2")
                    wav.writeframesraw(pcm.tobytes())
                    frames_written = bar_end
            temp.flush()
            os.fsync(temp.fileno())
        if not _valid_cached(Path(temp_name), track.frames):
            raise OSError("generated music file is invalid")
        os.replace(temp_name, output)
        temp_name = None
        return output
    finally:
        if temp_name is not None:
            Path(temp_name).unlink(missing_ok=True)
