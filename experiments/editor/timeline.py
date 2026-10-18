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
        return 6 * FPS if self.kind == "image" else self.duration_frames


@dataclass(frozen=True)
class Scene:
    id: str
    source_id: str
    kind: str
    included: bool
    duration_frames: int = 0
    start_frame: int = 0
    end_frame: int = 0

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


def _interval(source, length):
    if source.kind == "image":
        return dict(duration_frames=length)
    start = (source.capacity - length) // 2
    return dict(start_frame=start, end_frame=start + length)


def baseline(sources):
    if not sources or len(sources) > 100:
        raise ValueError("1〜100点の素材を選んでください。")
    count = min(12, len(sources))
    chosen = [sources[i * len(sources) // count] for i in range(count)]
    capacities = [source.capacity for source in chosen]
    lengths = [0] * count
    remaining = min(TARGET, sum(capacities))
    while remaining:
        active = [i for i, cap in enumerate(capacities) if lengths[i] < cap]
        if not active:
            break
        share, extra = divmod(remaining, len(active))
        moved = 0
        for index, i in enumerate(active):
            take = min(capacities[i] - lengths[i], share + (index < extra))
            lengths[i] += take
            moved += take
        remaining -= moved
    scenes = tuple(Scene(f"scene-{i + 1}", source.id, source.kind, True, **_interval(source, length)) for i, (source, length) in enumerate(zip(chosen, lengths)))
    return EditPlan(1, 1, scenes)


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
        source_id = change.get("source_id")
        if not isinstance(source_id, str):
            raise ValueError("差し替える素材を選んでください。")
        source = source_by_id.get(source_id)
        if source is None or source.capacity < 1:
            raise ValueError("差し替える素材が見つかりません。")
        length = min(scene.length, source.capacity)
        updated = Scene(scene.id, source.id, source.kind, scene.included, **_interval(source, length))
    else:
        source = source_by_id[scene.source_id]
        if scene.kind != "video":
            raise ValueError("前後調整は動画だけで使えます。")
        start, end = change.get("start_frame"), change.get("end_frame")
        if not _integer(start) or not _integer(end) or start >= end or end > source.capacity:
            raise ValueError("動画の開始・終了位置を確認してください。")
        updated = replace(scene, start_frame=start, end_frame=end)
    scenes = list(plan.scenes)
    scenes[index] = updated
    return EditPlan(plan.schema_version, plan.revision + 1, tuple(scenes))
