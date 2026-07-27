"""Startup selection for the physical Body implementation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from minebot.body import VoxelStructureRiskAssessor
from minebot.contract import Body, Region
from minebot.game.composite_body import CompositeBody
from minebot.game.governance import GovernancePolicy
from minebot.game.java_body import JavaBody
from minebot.game.java_body_adapter import (
    DuplexTransport,
    GovernanceAnswerer,
    JavaBodyClient,
    websocket_transport,
)


class BodyProviderConfigError(ValueError):
    pass


class BodyProviderName(str, Enum):
    SCARPET = "scarpet"
    JAVA = "java"
    COMPOSITE = "composite"

    @classmethod
    def parse(cls, value: str | "BodyProviderName") -> "BodyProviderName":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError as error:
            choices = ", ".join(item.value for item in cls)
            raise BodyProviderConfigError(
                f"MINEBOT_BODY_PROVIDER must be one of: {choices}"
            ) from error


@dataclass(frozen=True)
class BodyProviderRuntime:
    name: BodyProviderName
    body: Body
    governance: GovernancePolicy
    scarpet_body: Body | None
    java_body: JavaBody | None


def build_body_provider(
    provider: str | BodyProviderName,
    *,
    bot_name: str,
    natural_region: Region,
    scarpet_body: Body | None = None,
    java_body_url: str | None = None,
    java_connect: Callable[[], DuplexTransport] | None = None,
) -> BodyProviderRuntime:
    """Build one selected Body and the single policy shared by its providers."""

    name = BodyProviderName.parse(provider)
    needs_scarpet = name in {BodyProviderName.SCARPET, BodyProviderName.COMPOSITE}
    needs_java = name in {BodyProviderName.JAVA, BodyProviderName.COMPOSITE}
    if needs_scarpet and scarpet_body is None:
        raise BodyProviderConfigError(f"{name.value} provider requires a Scarpet body")
    if scarpet_body is not None and scarpet_body.bot_name != bot_name:
        raise BodyProviderConfigError("Scarpet body bot_name does not match provider bot")

    java_client: JavaBodyClient | None = None
    java_body: JavaBody | None = None
    if needs_java:
        connect = java_connect
        if connect is None:
            if not java_body_url:
                raise BodyProviderConfigError(
                    f"{name.value} provider requires MINEBOT_JAVA_BODY_URL"
                )
            connect = websocket_transport(java_body_url)
        java_client = JavaBodyClient(bot_name, connect)
        java_body = JavaBody(java_client, bot_name)

    if name is BodyProviderName.SCARPET:
        assert scarpet_body is not None
        selected: Body = scarpet_body
    elif name is BodyProviderName.JAVA:
        assert java_body is not None
        selected = java_body
    else:
        assert scarpet_body is not None and java_body is not None
        selected = CompositeBody(scarpet_body, java_body)

    governance = GovernancePolicy(
        natural_regions=[natural_region],
        structure_risk_assessor=VoxelStructureRiskAssessor(selected),
        require_structure_assessment=True,
    )
    if java_client is not None:
        java_client.configure_governance(GovernanceAnswerer(governance))
    return BodyProviderRuntime(name, selected, governance, scarpet_body, java_body)


def java_objectives_enabled(body: Body) -> bool:
    return isinstance(body, (JavaBody, CompositeBody))


__all__ = [
    "BodyProviderConfigError",
    "BodyProviderName",
    "BodyProviderRuntime",
    "build_body_provider",
    "java_objectives_enabled",
]
