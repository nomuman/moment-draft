from dataclasses import dataclass, replace

FPS = 30
TARGET = 60 * FPS


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    size: int
    width: int
    height: int
    kind: str
    duration_frames: int = 0
    has_audio: bool = False

    @property
    def capacity(self):
        return 3 * FPS if self.kind == "image" else self.duration_frames


@dataclass(frozen=True)
class Candidate:
    id: str
    source_id: str
    kind: str
    start_frame: int
    end_frame: int
    reason: str

    @property
    def length(self):
        return self.end_frame - self.start_frame


@dataclass(frozen=True)
class Scene:
    id: str
    source_id: str
    kind: str
    included: bool
    duration_frames: int = 0
    start_frame: int = 0
    end_frame: int = 0
    candidate_id: str | None = None

    @property
    def length(self):
        return self.duration_frames if self.kind == "image" else self.end_frame - self.start_frame


@dataclass(frozen=True)
class EditPlan:
    schema_version: int
    revision: int
    scenes: tuple[Scene, ...]

    @property
    def frame_count(self):
        return sum(scene.length for scene in self.scenes if scene.included)


def candidates(sources):
    result = []
    for source in sources:
        if source.capacity < 1:
            continue
        count = 1 if source.kind == "image" else min(12, (source.capacity + 6 * FPS - 1) // (6 * FPS))
        for index in range(count):
            length = min(6 * FPS, source.capacity - index * 6 * FPS)
            start = index * (source.capacity - 6 * FPS) // (count - 1) if count > 1 and source.capacity >= count * 6 * FPS else index * 6 * FPS
            end = start + length
            result.append(Candidate(f"{source.id}-{start}-{end}", source.id, source.kind, start, end,
                                    "写真を3秒表示" if source.kind == "image" else "素材全体から時間順に抽出"))
    return tuple(result)


def candidate_scene(candidate, scene_id, included=True, length=None):
    length = candidate.length if length is None else length
    interval = dict(duration_frames=length) if candidate.kind == "image" else dict(start_frame=candidate.start_frame, end_frame=candidate.start_frame + length)
    return Scene(scene_id, candidate.source_id, candidate.kind, included, **interval, candidate_id=candidate.id)


def baseline(sources):
    if not sources or len(sources) > 100:
        raise ValueError("1〜100点の素材を選んでください。")
    first_pass = sum(min(source.capacity, 6 * FPS) for source in sources)
    count = min(len(sources), max(1, (TARGET * len(sources) + first_pass - 1) // first_pass))
    chosen = [sources[i * (len(sources) - 1) // (count - 1) if count > 1 else 0] for i in range(count)]
    by_source = {source.id: list(candidates([source])) for source in chosen}
    ordered = [group[index] for index in range(12) for group in by_source.values() if index < len(group)]
    scenes, remaining = [], TARGET
    for candidate in ordered:
        if remaining == 0:
            break
        length = min(candidate.length, remaining)
        scenes.append(candidate_scene(candidate, f"scene-{len(scenes) + 1}", length=length))
        remaining -= length
    return EditPlan(1, 1, tuple(scenes))


def _integer(value):
    return type(value) is int and 0 <= value <= 108000


def edit(plan, change, sources):
    if not isinstance(change, dict) or change.get("kind") not in ("include", "exclude", "replace", "trim"):
        raise ValueError("編集内容を確認してください。")
    scene_id = change.get("scene_id")
    if not isinstance(scene_id, str):
        raise ValueError("場面を選んでください。")
    matches = [i for i, scene in enumerate(plan.scenes) if scene.id == scene_id]
    if len(matches) != 1:
        raise ValueError("場面が見つかりません。")
    index = matches[0]
    scene = plan.scenes[index]
    source_by_id = {source.id: source for source in sources}
    kind = change["kind"]
    if kind in ("include", "exclude"):
        updated = replace(scene, included=kind == "include")
    elif kind == "replace":
        candidate_id = change.get("candidate_id")
        if candidate_id is not None:
            candidate = next((item for item in candidates(sources) if item.id == candidate_id), None)
            if candidate is None:
                raise ValueError("差し替える候補が見つかりません。")
            updated = candidate_scene(candidate, scene.id, scene.included)
        else:
            source_id = change.get("source_id")
            if not isinstance(source_id, str):
                raise ValueError("差し替える素材を選んでください。")
            source = source_by_id.get(source_id)
            if source is None or source.capacity < 1:
                raise ValueError("差し替える素材が見つかりません。")
            candidate = candidates([source])[0]
            updated = candidate_scene(candidate, scene.id, scene.included, min(scene.length, candidate.length))
    else:
        source = source_by_id[scene.source_id]
        if scene.kind != "video":
            raise ValueError("前後調整は動画だけで使えます。")
        start, end = change.get("start_frame"), change.get("end_frame")
        if not _integer(start) or not _integer(end) or start >= end or end > source.capacity:
            raise ValueError("動画の開始・終了位置を確認してください。")
        updated = replace(scene, start_frame=start, end_frame=end, candidate_id=None)
    scenes = list(plan.scenes)
    scenes[index] = updated
    return EditPlan(plan.schema_version, plan.revision + 1, tuple(scenes))


def from_dict(value):
    return EditPlan(value["schema_version"], value["revision"], tuple(Scene(**scene) for scene in value["scenes"]))
