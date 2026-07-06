from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from camera.worldstream.protocol import Keyframe, SectionKey, TransformSample


SECTION_SIZE = 16
SECTION_VOLUME = SECTION_SIZE * SECTION_SIZE * SECTION_SIZE


@dataclass
class SectionData:
    key: SectionKey
    palette: tuple[str, ...]
    indices: np.ndarray

    @classmethod
    def from_keyframe(cls, keyframe: Keyframe) -> "SectionData":
        indices = np.asarray(keyframe.indices, dtype=np.uint16).reshape((SECTION_SIZE, SECTION_SIZE, SECTION_SIZE))
        return cls(key=keyframe.section, palette=keyframe.palette, indices=indices)

    def occupied_mask(self) -> np.ndarray:
        air_indices = {index for index, state in enumerate(self.palette) if state == "minecraft:air"}
        if not air_indices:
            return np.ones_like(self.indices, dtype=bool)
        mask = np.ones_like(self.indices, dtype=bool)
        for index in air_indices:
            mask &= self.indices != index
        return mask


class SectionStore:
    def __init__(self) -> None:
        self.sections: dict[SectionKey, SectionData] = {}
        self.dirty_sections: set[SectionKey] = set()

    def apply_keyframe(self, keyframe: Keyframe) -> None:
        section = SectionData.from_keyframe(keyframe)
        self.sections[section.key] = section
        self.dirty_sections.add(section.key)

    def mark_clean(self, key: SectionKey) -> None:
        self.dirty_sections.discard(key)

    def take_dirty_sections(self) -> set[SectionKey]:
        dirty = set(self.dirty_sections)
        self.dirty_sections.clear()
        return dirty

    def clear(self) -> None:
        self.sections.clear()
        self.dirty_sections.clear()


class TransformBuffer:
    def __init__(self) -> None:
        self.samples: list[TransformSample] = []

    def add(self, sample: TransformSample) -> None:
        self.samples.append(sample)
        self.samples = self.samples[-8:]

    def latest(self) -> TransformSample | None:
        return self.samples[-1] if self.samples else None

    def clear(self) -> None:
        self.samples.clear()

    def interpolated(self, monotonic_s: float) -> TransformSample | None:
        if not self.samples:
            return None
        if len(self.samples) == 1 or monotonic_s <= self.samples[0].monotonic_s:
            return self.samples[0]
        previous = self.samples[0]
        for sample in self.samples[1:]:
            if monotonic_s <= sample.monotonic_s:
                span = max(1e-6, sample.monotonic_s - previous.monotonic_s)
                alpha = max(0.0, min(1.0, (monotonic_s - previous.monotonic_s) / span))
                return TransformSample(
                    entity=sample.entity,
                    dimension=sample.dimension,
                    pos=tuple(previous.pos[index] * (1.0 - alpha) + sample.pos[index] * alpha for index in range(3)),
                    yaw=_lerp_angle(previous.yaw, sample.yaw, alpha),
                    pitch=previous.pitch * (1.0 - alpha) + sample.pitch * alpha,
                    on_ground=sample.on_ground,
                    pose=sample.pose,
                    monotonic_s=monotonic_s,
                )
            previous = sample
        return self.samples[-1]


def _lerp_angle(start: float, end: float, alpha: float) -> float:
    delta = ((end - start + 180.0) % 360.0) - 180.0
    return start + delta * alpha
