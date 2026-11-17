"""Every native action a Body transaction sends must reach the real client.

pillar_up passed its client GameTest and its fake-body unit tests, yet the
live H6 run got capability_unavailable:unsupported_action: the real-client
wire table never listed pillarUp (2026-10-29)."""

import re
from pathlib import Path

from minebot.contract import Action
from minebot.game import real_client_body as rcb

ROOT = Path(__file__).resolve().parents[2]


def _sent_action_names() -> set[str]:
    names: set[str] = set()
    for path in (ROOT / "minebot" / "body").rglob("*.py"):
        names.update(re.findall(r'Action\.create\(\s*"([A-Za-z]+)"', path.read_text()))
    return names


# Objective routes read their target from the params; these stand in for any.
_SAMPLE_PARAMS = (
    {"target": [0, 64, 0], "goal": {"kind": "near", "pos": [0, 64, 0]}, "start": [0, 64, 0], "end": [0, 64, 0],
     "anchor": [0, 64, 0]},
    {"target": "zombie", "block": "stone", "item": "stone"},
)


def _routed(name: str) -> bool:
    return name in rcb.UNSUPPORTED_CAPABILITIES or any(
        route(Action.create(name, dict(params))) is not None
        for params in ({},) + _SAMPLE_PARAMS
        for route in (rcb._objective_for, rcb._wire_action_for, rcb._control_for)
    )


def test_every_body_action_has_a_real_client_route():
    assert sorted(name for name in _sent_action_names() if not _routed(name)) == []


def test_pillar_up_goes_to_the_client_and_ends_on_its_own_terminal():
    assert rcb._wire_action_for(Action.create("pillarUp", {"blocks": 3}))["name"] == "pillarUp"
    assert rcb._terminal_name("pillarUp") == "pillarUpDone"
