"""Support-matrix types for the Baritone-backed real-client Body.

The real-client path is deliberately opt-in. A tuple is usable only after
live conformance has promoted it to ``supported``; unverified candidates must
never be selected by composition.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable


class SupportStatus(StrEnum):
    CANDIDATE = "candidate"
    SUPPORTED = "supported"
    EXCLUDED = "excluded"


@dataclass(frozen=True)
class RealClientTuple:
    """The complete version tuple that defines one client Body build."""

    minecraft: str
    baritone: str
    loader_package: str
    java: str
    integration: str

    def as_dict(self) -> dict[str, str]:
        return {
            "minecraft_version": self.minecraft,
            "baritone_version": self.baritone,
            "loader_package": self.loader_package,
            "java_runtime": self.java,
            "integration_version": self.integration,
        }


@dataclass(frozen=True)
class SupportMatrixEntry:
    matrix_id: str
    tuple: RealClientTuple
    status: SupportStatus
    evidence: str

    @property
    def selectable(self) -> bool:
        return self.status is SupportStatus.SUPPORTED


# The exact tuple below has passed the disposable-world conformance record:
# start/join, real terrain arrival, resource collection, cancellation, every
# main Baritone process family, and structured Body state/terminal evidence.
SUPPORTED_26_1_2 = SupportMatrixEntry(
    matrix_id="supported-mc-26.1.2-baritone-v1.18.0-fabric-java25",
    tuple=RealClientTuple(
        minecraft="26.1.2",
        baritone="v1.18.0",
        loader_package="fabric-0.19.3",
        java="25",
        integration="minebot-client-spike",
    ),
    status=SupportStatus.SUPPORTED,
    evidence=(
        "Phase A and Body conformance evidence: the pinned tuple starts, joins, "
        "reaches terrain, mines with an inventory delta, cancels within a "
        "measured bound, and exposes all eight main Baritone process families"
    ),
)

REAL_CLIENT_SUPPORT_MATRIX: tuple[SupportMatrixEntry, ...] = (
    SUPPORTED_26_1_2,
)


class UnsupportedRealClientTuple(ValueError):
    """Raised when production code tries to select an unpromoted tuple."""


def find_real_client_tuple(
    observed: RealClientTuple,
    entries: Iterable[SupportMatrixEntry] = REAL_CLIENT_SUPPORT_MATRIX,
) -> SupportMatrixEntry | None:
    """Return the exact matrix entry for ``observed`` if one is recorded."""

    for entry in entries:
        if entry.tuple == observed:
            return entry
    return None


def require_supported_real_client_tuple(
    observed: RealClientTuple,
    entries: Iterable[SupportMatrixEntry] = REAL_CLIENT_SUPPORT_MATRIX,
) -> SupportMatrixEntry:
    """Fail closed unless the exact tuple has passed live conformance."""

    entry = find_real_client_tuple(observed, entries)
    if entry is None:
        raise UnsupportedRealClientTuple(
            "real-client tuple is absent from the support matrix: "
            f"{observed.as_dict()}"
        )
    if not entry.selectable:
        raise UnsupportedRealClientTuple(
            f"real-client tuple {entry.matrix_id} is {entry.status.value}: {entry.evidence}"
        )
    return entry
