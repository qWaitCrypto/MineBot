#!/usr/bin/env python3
"""Step 5 fixture runner: exercise the integrated production Agent path.

What this proves, and what it does not
--------------------------------------
The Agent under test is the unmodified production ingress -- this runner spawns
``python -m minebot.app.real_server_session --interactive`` and speaks to it
through its documented user channel (stdin), against the authoritative Carpet
FakePlayer over the Java Body WebSocket. Nothing under ``minebot/`` is patched,
monkeypatched or bypassed. The runtime builds its own model provider from the
environment, so the single substitution is *which HTTP endpoint the model client
dials*.

That endpoint is the scripted OpenAI-compatible server in this file. A scripted
model proves plumbing only: it is never autonomous-model evidence, and every
artifact written here carries ``model: "scripted"`` so the distinction cannot be
lost downstream. What the scripting buys is the ability to assert on the *model
surface*. The server keeps a verbatim record of the messages and tool schemas
the runtime sends, which turns "the obstacle reached the model" from an
inference about traces into a checkable claim about bytes.

Safety
------
The fixture is built with the same discipline as ``probe_body_geometry.py``:
emptiness is decided by authoritative ``blockCells`` perception and the run
refuses rather than overwriting anything, force-loaded chunks are released only
if this runner added them, and every placed cell is restored to air afterwards.
No world reset and no server restart.
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from minebot.body.world_read import read_block_facts  # noqa: E402
# The private classifier, imported on purpose: predicting the fixture's
# governance verdict is only worth anything if it runs the *same* function the
# live assessor runs. A re-implementation would agree with itself and nothing
# else. If this import breaks, the prediction phase must be fixed rather than
# quietly replaced with a guess.
from minebot.body.structure_risk import _classify_voxels, _sample_positions  # noqa: E402
from minebot.contract.governance import BreakContext, Region  # noqa: E402
from minebot.game.governance import GovernancePolicy  # noqa: E402
from minebot.game.java_body import JavaBody  # noqa: E402
from minebot.game.java_body_adapter import (  # noqa: E402
    GovernanceAnswerer,
    JavaBodyClient,
    websocket_transport,
)
from minebot.game.rcon import RconClient, RconConfig  # noqa: E402

# The geometry probe already carries a reviewed implementation of the pieces
# that must not drift between probes: what counts as an empty cell, which
# chunks a volume spans, and force-load bookkeeping that only releases what it
# claimed. Re-deriving them here would be a second chance to get them wrong.
from probe_body_geometry import (  # noqa: E402
    EMPTY_TYPES,
    await_body,
    bot_alive,
    bot_pos,
    chunk_span,
    forceload_add,
)

# --- Scripted model -------------------------------------------------------


@dataclass
class Move:
    """One scripted model turn.

    ``expect_in_messages`` is the assertion that makes a scripted run worth
    running: substrings that must already be present in the conversation the
    runtime sends *before* this move is emitted. That is how obstacle feedback,
    partial output and restored checkpoints are verified -- at the model
    boundary, in the runtime's own words.
    """

    label: str
    tool: str | None = None
    args: dict[str, object] = field(default_factory=dict)
    text: str | None = None
    expect_in_messages: tuple[str, ...] = ()
    forbid_in_messages: tuple[str, ...] = ()


EXHAUSTED = Move(
    label="<script_exhausted>",
    text="The script has no further moves; stopping without inventing work.",
)

_MESSAGE_CAP = 24000


def _cap(text: str) -> tuple[str, bool]:
    if len(text) <= _MESSAGE_CAP:
        return text, False
    return text[:_MESSAGE_CAP], True


def _record_messages(messages) -> list[dict[str, object]]:
    """Per-message record, so a huge system prompt cannot push the tool result
    -- the thing actually under test -- out of the artifact."""
    recorded: list[dict[str, object]] = []
    for message in messages:
        if not isinstance(message, dict):
            recorded.append({"role": None, "content": str(message)[:_MESSAGE_CAP]})
            continue
        content = message.get("content")
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        capped, truncated = _cap(text or "")
        entry: dict[str, object] = {"role": message.get("role"), "content": capped}
        if truncated:
            entry["truncated"] = True
        if message.get("tool_calls"):
            entry["tool_calls"] = message["tool_calls"]
        recorded.append(entry)
    return recorded


class ScriptedModel:
    """The scripted policy plus a verbatim record of the model surface."""

    def __init__(self, policy, record_path: Path) -> None:
        # ``policy`` is called with (turn, messages, tools) and returns the Move
        # for that turn. A fixed list would be brittle -- the runtime decides
        # how often it consults the model, and a plan review or a recovery turn
        # would silently shift every later move onto the wrong evidence. A
        # policy that reads what it was actually sent stays aligned.
        self._policy = policy
        self._cursor = 0
        self._call_seq = 0
        self._lock = threading.Lock()
        self.turns: list[dict[str, object]] = []
        self.failures: list[str] = []
        record_path.parent.mkdir(parents=True, exist_ok=True)
        # Truncated, not appended: a record that accumulates across runs invites
        # reading one run's model surface as evidence about another.
        self._record = record_path.open("w", encoding="utf-8")

    def close(self) -> None:
        self._record.close()

    @property
    def consumed(self) -> int:
        return self._cursor

    def respond(self, payload: dict[str, object]) -> tuple[Move, str]:
        with self._lock:
            turn = len(self.turns)
            raw_tools = payload.get("tools")
            tools = sorted(
                str(entry.get("function", {}).get("name"))
                for entry in (raw_tools if isinstance(raw_tools, list) else [])
                if isinstance(entry, dict)
            )
            messages = payload.get("messages")
            messages = messages if isinstance(messages, list) else []
            try:
                move = self._policy(turn, messages, tools)
            except Exception as exc:  # noqa: BLE001 - recorded as probe evidence
                self.failures.append(f"turn {turn}: policy raised {type(exc).__name__}: {exc}")
                move = Move(label="<policy_error>", text="Scripted policy failed.")
            self._cursor += 1
            self._call_seq += 1
            call_id = f"call_scripted_{self._call_seq}"
            haystack = json.dumps(messages, ensure_ascii=False)

            missing = [needle for needle in move.expect_in_messages if needle not in haystack]
            present = [needle for needle in move.forbid_in_messages if needle in haystack]
            emitted = move
            if move.tool is not None and move.tool not in tools:
                # Emitting a call for a tool the runtime never offered would
                # fabricate a capability. Record the gap and say nothing.
                self.failures.append(
                    f"turn {turn} [{move.label}]: tool {move.tool!r} was not offered"
                )
                emitted = Move(label=move.label + "<tool_absent>", text="Required tool unavailable.")
            for needle in missing:
                self.failures.append(
                    f"turn {turn} [{move.label}]: expected evidence {needle!r} never reached the model"
                )
            for needle in present:
                self.failures.append(
                    f"turn {turn} [{move.label}]: forbidden text {needle!r} reached the model"
                )

            capped, truncated = _cap(haystack)
            record = {
                "turn": turn,
                "at": datetime.now(timezone.utc).isoformat(),
                "move": move.label,
                "emitted_tool": emitted.tool,
                "emitted_args": emitted.args or None,
                "message_count": len(messages),
                "tools_offered": tools,
                "tools_json": json.dumps(raw_tools, ensure_ascii=False, sort_keys=True)
                if isinstance(raw_tools, list)
                else None,
                "expect_in_messages": list(move.expect_in_messages),
                "missing_expectations": missing,
                "forbidden_present": present,
                "messages": _record_messages(messages),
                "messages_json": capped,
                "messages_truncated": truncated,
            }
            self.turns.append(
                {
                    key: value
                    for key, value in record.items()
                    if key not in ("messages_json", "tools_json", "messages")
                }
            )
            self._record.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            self._record.flush()
            return emitted, call_id


def sequence_policy(moves: list[Move]):
    """Adapt a fixed move list to the policy interface, for simple phases."""

    def policy(turn: int, _messages, _tools) -> Move:
        return moves[turn] if turn < len(moves) else EXHAUSTED

    return policy


def last_tool_message(messages) -> str:
    """The most recent tool result the runtime handed the model, as raw text."""
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "tool":
            content = message.get("content")
            if isinstance(content, str):
                return content
            return json.dumps(content, ensure_ascii=False)
    return ""


def _chunks(move: Move, call_id: str, model: str):
    base = {
        "id": "chatcmpl-scripted",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
    }
    if move.tool is not None:
        yield {
            **base,
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": move.tool,
                                    "arguments": json.dumps(move.args, sort_keys=True),
                                },
                            }
                        ],
                    },
                    "finish_reason": None,
                }
            ],
        }
        yield {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}
    else:
        yield {
            **base,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": move.text or ""},
                    "finish_reason": None,
                }
            ],
        }
        yield {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    yield {
        **base,
        "choices": [],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self.send_error(400, "invalid json")
            return
        if not isinstance(payload, dict):
            self.send_error(400, "expected a JSON object")
            return
        scripted: ScriptedModel = self.server.scripted  # type: ignore[attr-defined]
        move, call_id = scripted.respond(payload)
        model = str(payload.get("model") or "scripted")
        chunks = list(_chunks(move, call_id, model))
        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        message: dict[str, object] = {"role": "assistant", "content": move.text}
        if move.tool is not None:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": move.tool,
                            "arguments": json.dumps(move.args, sort_keys=True),
                        },
                    }
                ],
            }
        body = json.dumps(
            {
                "id": "chatcmpl-scripted",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if move.tool else "stop",
                    }
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        # The request record file is the log; stderr chatter would drown the probe.
        return


def start_scripted_server(scripted: ScriptedModel) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.scripted = scripted  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, name="scripted-model", daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}/v1"


# --- Fixture --------------------------------------------------------------

OAK_COUNT = 5
BIRCH_COUNT = 3
GOAL = "collect 8 logs"


# Trunk base is always ``oy``; the value is the log count, so the totals below
# are the fixture's arithmetic rather than a comment's promise about it.
OAK_TRUNKS = (((2, 2), 3), ((6, 2), 2))
BIRCH_TRUNKS = ((((4, 6)), 3),)
CANOPY_Y_OFFSET = 3

assert sum(height for _, height in OAK_TRUNKS) == OAK_COUNT
assert sum(height for _, height in BIRCH_TRUNKS) == BIRCH_COUNT


def _tree_cells(dx: int, dz: int, height: int, species: str, ox: int, oy: int, oz: int):
    """One rooted trunk with a leaf cap, shaped so governance reads it as a tree.

    A bare horizontal row of logs is not a wood supply to the single governance
    boundary -- it is a candidate player structure, and it scored 0.6 (AMBIGUOUS)
    on ``horizontal_log_pattern`` + ``log_without_tree_evidence``, so every break
    demanded confirmation. Trees are what a collect goal is entitled to take, so
    the fixture has to actually be trees: a vertical trunk standing on dirt earns
    ``rooted_vertical_log``, and the leaf cap earns ``nearby_leaves``.

    Two placement rules keep that verdict stable for the whole run. Leaves sit
    strictly above every log, so no eye-to-log ray from a ground stand can cross
    them and lose a reach candidate to line-of-sight. And the cap sits within
    three blocks of the *bottom* log, so it stays inside the assessor's sample
    window for every log in the trunk -- including the last one left standing,
    when the trunk below it is already gone and rootedness no longer applies.
    """
    x, z = ox + dx, oz + dz
    cells = [((x, oy + step, z), f"{species}_log") for step in range(height)]
    canopy_y = oy + CANOPY_Y_OFFSET
    assert canopy_y > oy + height - 1, "leaves must clear every log"
    assert canopy_y - oy <= 3, "leaves must stay inside the bottom log's sample window"
    leaf = f"{species}_leaves[persistent=true]"
    for lx in range(x - 1, x + 2):
        for lz in range(z - 1, z + 2):
            cells.append(((lx, canopy_y, lz), leaf))
    cells.append(((x, canopy_y + 1, z), leaf))
    return cells


def fixture_cells(ox: int, oy: int, oz: int) -> list[tuple[tuple[int, int, int], str]]:
    """A floating platform holding a finite, countable wood supply.

    The whole point is scarcity the runtime cannot route around by accident:
    exactly ``OAK_COUNT`` oak logs and ``BIRCH_COUNT`` birch logs, nothing else
    within reach. An over-specific first stage (oak only) must therefore run out
    of candidates at 5 of 8, and only widening to the log family can finish the
    goal. Suspending the platform far above the terrain keeps natural trees
    outside the tree search radius, so "exhausted" means the fixture, not luck.
    """
    placed: list[tuple[tuple[int, int, int], str]] = []
    floor_y = oy - 1
    # Dirt, not stone: ``rooted`` is what separates a tree from a wooden build,
    # and it is decided by the block under the trunk.
    for x in range(ox - 1, ox + 9):
        for z in range(oz - 1, oz + 9):
            placed.append(((x, floor_y, z), "dirt"))
    # A two-high rim: one block can be jumped, two cannot, so neither the body
    # nor a dropped item leaves the platform and turns a fall into a "result".
    for x in range(ox - 2, ox + 10):
        for z in range(oz - 2, oz + 10):
            if ox - 1 <= x <= ox + 8 and oz - 1 <= z <= oz + 8:
                continue
            placed.append(((x, floor_y, z), "stone"))
            placed.append(((x, oy, z), "stone"))
            placed.append(((x, oy + 1, z), "stone"))
    for (dx, dz), height in OAK_TRUNKS:
        placed.extend(_tree_cells(dx, dz, height, "oak", ox, oy, oz))
    for (dx, dz), height in BIRCH_TRUNKS:
        placed.extend(_tree_cells(dx, dz, height, "birch", ox, oy, oz))
    return placed


def fixture_bounds(ox: int, oy: int, oz: int) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    return (ox - 2, oy - 1, oz - 2), (ox + 9, oy + CANOPY_Y_OFFSET + 1, oz + 9)


def volume_cells(low, high) -> list[tuple[int, int, int]]:
    return [
        (x, y, z)
        for x in range(low[0], high[0] + 1)
        for y in range(low[1], high[1] + 1)
        for z in range(low[2], high[2] + 1)
    ]


def connect_body(url: str, bot: str, low, high) -> tuple[JavaBody, JavaBodyClient, Region]:
    natural = Region(
        "agent-path-probe",
        (low[0] - 16, 0, low[2] - 16),
        (high[0] + 16, 320, high[2] + 16),
    )
    policy = GovernancePolicy(natural_regions=[natural])
    client = JavaBodyClient(
        bot,
        websocket_transport(url),
        GovernanceAnswerer(policy),
        action_wall_timeout_s=180.0,
        recv_timeout_s=2.0,
    )
    client.connect()
    return JavaBody(client, bot), client, natural


def assert_empty(body: JavaBody, low, high) -> list[tuple[int, int, int]]:
    """Decide emptiness from authoritative perception, never from a fill count.

    ``/fill ... replace air`` reports zero changes when the state is already
    what it would set, so its changed-count cannot tell "empty" from
    "occupied". ``blockCells`` reads the real block at every cell and raises
    rather than inventing one.
    """
    cells = volume_cells(low, high)
    facts = read_block_facts(body, tuple(cells), page_size=256, failure_label="agent-path emptiness")
    return sorted(
        pos
        for pos, fact in facts.items()
        if str(fact.data.get("type") or "") not in EMPTY_TYPES
    )


def build_fixture(rcon: RconClient, body: JavaBody, ox: int, oy: int, oz: int) -> dict[str, object]:
    low, high = fixture_bounds(ox, oy, oz)
    chunks = chunk_span(low, high)
    added = forceload_add(rcon, chunks)
    occupied = assert_empty(body, low, high)
    if occupied:
        for cx, cz in added:
            rcon.command(f"forceload remove {cx * 16} {cz * 16}")
        sample = ", ".join(str(pos) for pos in occupied[:4])
        print(
            f"[fixture] REFUSING to build: {len(occupied)} of "
            f"{len(volume_cells(low, high))} cells are not air, e.g. {sample}. "
            "Pick an empty origin with --ox/--oy/--oz.",
            flush=True,
        )
        return {"built": False, "reason": "non_empty_volume", "occupied": len(occupied)}
    placed = fixture_cells(ox, oy, oz)
    for (x, y, z), block in placed:
        rcon.command(f"setblock {x} {y} {z} minecraft:{block}")
    return {
        "built": True,
        "low": list(low),
        "high": list(high),
        "placed": len(placed),
        "forceloaded": [list(chunk) for chunk in added],
        "oak": OAK_COUNT,
        "birch": BIRCH_COUNT,
    }


def restore_fixture(rcon: RconClient, body: JavaBody, low, high, added) -> dict:
    """Return the volume to the air the build gate proved was there.

    ``low``/``high`` are the bounds recorded by the build that actually ran, not
    whatever this build of the file would compute. Only the recorded volume was
    verified empty before the fixture went in, so only it may be filled with
    air: a later, taller layout would otherwise clear cells nobody ever checked.

    Clearing the recorded placement list instead would undo only the layout this
    file happens to describe -- it would miss blocks a previous layout left
    behind, and anything the body moved during a run. ``assert_empty`` verifies
    the result from perception rather than from the fill's own report.
    """
    low = tuple(low)
    high = tuple(high)
    rcon.command(
        f"fill {low[0]} {low[1]} {low[2]} {high[0]} {high[1]} {high[2]} minecraft:air"
    )
    # Drops the body did not pick up are entities, not blocks, so the fill
    # leaves them. Bound the kill to the fixture volume: this is a shared world
    # to everything outside it.
    span = [high[i] - low[i] for i in range(3)]
    rcon.command(
        "kill @e[type=item,"
        f"x={low[0]},y={low[1]},z={low[2]},dx={span[0]},dy={span[1]},dz={span[2]}]"
    )
    residue = assert_empty(body, low, high)
    for cx, cz in added or []:
        rcon.command(f"forceload remove {cx * 16} {cz * 16}")
    return {
        "low": list(low),
        "high": list(high),
        "residue": len(residue),
        "residue_sample": [list(p) for p in residue[:8]],
        "released": len(added or []),
    }


# --- Production ingress ---------------------------------------------------


def ingress_env(*, base_url: str, key_env: str, log_path: Path, state_db: Path,
                bot: str, body_url: str, low, high, pad) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "MINEBOT_BODY_PROVIDER": "java",
            "MINEBOT_JAVA_BODY_URL": body_url,
            "MINEBOT_REAL_BOT": bot,
            "MINEBOT_REAL_NATURAL_REGION": ",".join(
                str(value)
                for value in (low[0] - 16, 0, low[2] - 16, high[0] + 16, 320, high[2] + 16)
            ),
            "MINEBOT_REAL_RECOVERY_RESPAWN_POS": ",".join(str(value) for value in pad),
            "MINEBOT_AGENT_LOG_PATH": str(log_path),
            "MINEBOT_AGENT_STATE_DB": str(state_db),
            "MINEBOT_REAL_SERVER_ID": "agent-path-probe-local",
            "MINEBOT_LLM_KIND": "openai_chat",
            "MINEBOT_LLM_MODEL": "scripted-plumbing",
            "MINEBOT_LLM_BASE_URL": base_url,
            "MINEBOT_LLM_API_KEY_ENV": key_env,
            # A loopback stub still wants a non-empty bearer; this is a
            # placeholder for a local socket, not a credential.
            key_env: "scripted-local-endpoint",
            "MINEBOT_AGENT_LANGUAGE": "English",
        }
    )
    return env


def run_ingress(env: dict[str, str], *, driver, timeout_s: float,
                interactive: bool = True, goal: str | None = None,
                out_path: Path | None = None) -> dict[str, object]:
    """Drive the production CLI over its documented user channel.

    ``driver`` receives the live process and the growing stdout line list, so a
    phase can pace its user messages against what the session has actually
    done. Writing every line up front does not work: the session drains stdin
    as fast as it can, so a trailing ``/quit`` lands before the turn it was
    meant to follow.
    """
    argv = [sys.executable, "-m", "minebot.app.real_server_session"]
    if interactive:
        argv.append("--interactive")
    if goal:
        argv.append(goal)
    started = time.time()
    proc = subprocess.Popen(
        argv,
        cwd=str(ROOT),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    lines: list[str] = []

    def pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line.rstrip("\n"))

    reader = threading.Thread(target=pump, name="ingress-stdout", daemon=True)
    reader.start()

    driver_error: list[str] = []
    try:
        driver(proc, lines)
    except Exception as exc:  # noqa: BLE001 - reported as probe evidence
        driver_error.append(f"{type(exc).__name__}: {exc}")

    timed_out = False
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        proc.wait(timeout=30)
    reader.join(timeout=15)
    elapsed = time.time() - started
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "exit_code": proc.returncode,
        "timed_out": timed_out,
        "driver_error": driver_error or None,
        "elapsed_s": round(elapsed, 2),
        "stdout_tail": lines[-25:],
    }


def send(proc: subprocess.Popen, line: str) -> None:
    assert proc.stdin is not None
    proc.stdin.write(line if line.endswith("\n") else line + "\n")
    proc.stdin.flush()


def wait_for_line(lines: list[str], needle: str, *, timeout_s: float) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if any(needle in line for line in lines):
            return True
        time.sleep(0.2)
    return False


def wait_for_turns(scripted: ScriptedModel, count: int, *, timeout_s: float) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if scripted.consumed >= count:
            return True
        time.sleep(0.2)
    return False


def read_trace(path: Path) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    if not path.exists():
        return events
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            events.append(record)
    return events


def inventory_logs(rcon: RconClient, bot: str) -> dict[str, int]:
    """Count logs from the server's own inventory view, not the Agent's story."""
    counts: dict[str, int] = {}
    for species in ("oak_log", "birch_log"):
        response = rcon.command(
            f"execute as {bot} run clear {bot} minecraft:{species} 0"
        )
        digits = "".join(ch if ch.isdigit() else " " for ch in response).split()
        counts[species] = int(digits[0]) if digits else 0
    return counts


# --- Phases ---------------------------------------------------------------

ARTIFACT_DIR = ROOT / "logs" / "agentic-runtime"
STATE_FILE = ARTIFACT_DIR / "agent-path-fixture.json"


def rcon_for(args: argparse.Namespace) -> RconClient:
    rcon = RconClient(
        RconConfig(host="127.0.0.1", port=args.rcon_port, password=args.rcon_password)
    )
    rcon.connect()
    return rcon


def ensure_bot(rcon: RconClient, bot: str, *, pad=None) -> None:
    """Make the FakePlayer present and survival, without assuming it already is."""
    if not bot_alive(rcon, bot):
        rcon.command(f"player {bot} spawn")
        time.sleep(2.5)
    rcon.command(f"gamemode survival {bot}")
    if pad is not None:
        rcon.command(f"tp {bot} {pad[0] + 0.5} {pad[1]} {pad[2] + 0.5}")
        time.sleep(1.0)


def ready_body(url: str, bot: str, low, high, rcon: RconClient):
    """Connect, and do not return until the Body actually answers perception.

    An RCON ``player <bot> spawn`` returns before the login completes, and the
    bridge resolves the body by name at request time -- so a connect that races
    the login gets ``body_missing`` from the very first read. Every phase here
    reads before it writes, and a spurious ``body_missing`` on the emptiness
    gate is the difference between a refusal and a build, so waiting for a real
    answer is not optional politeness.
    """
    body, client, region = connect_body(url, bot, low, high)
    try:
        await_body(body, rcon, bot)
    except Exception:
        client.close()
        raise
    return body, client, region


def phase_discover(args: argparse.Namespace) -> dict[str, object]:
    """Validate the scripted transport and capture the real model surface.

    No world writes: the message is deliberately not a strict collect/acquire
    command, so the run reaches the model, records what it was offered, and
    stops without touching a block. Interactive mode is the surface under test
    because only it builds the durable task/checkpoint workspace, so the
    one-shot path would under-report the tools the scenario depends on.
    """
    scripted = ScriptedModel(
        sequence_policy(
            [Move(label="discover", text="Recording the model surface; taking no action.")]
        ),
        ARTIFACT_DIR / "agent-path-discover.requests.jsonl",
    )
    server, base_url = start_scripted_server(scripted)
    pad = (args.ox, args.oy, args.oz)
    low, high = fixture_bounds(*pad)
    env = ingress_env(
        base_url=base_url,
        key_env="MINEBOT_SCRIPTED_ENDPOINT_KEY",
        log_path=ARTIFACT_DIR / "agent-path-discover.trace.jsonl",
        state_db=ARTIFACT_DIR / "agent-path-discover.db",
        bot=args.bot,
        body_url=args.url,
        low=low,
        high=high,
        pad=pad,
    )
    try:
        def driver(proc, lines):
            wait_for_line(lines, "interactive_ready", timeout_s=90.0)
            send(proc, "describe what you can see")
            wait_for_turns(scripted, 1, timeout_s=120.0)
            send(proc, "/quit")

        outcome = run_ingress(
            env,
            driver=driver,
            timeout_s=args.ingress_timeout_s,
            interactive=True,
            goal=None,
            out_path=ARTIFACT_DIR / "agent-path-discover.stdout.txt",
        )
    finally:
        server.shutdown()
        scripted.close()
    tools = scripted.turns[0]["tools_offered"] if scripted.turns else []
    return {
        "phase": "discover",
        "model": "scripted",
        "ingress": outcome,
        "model_turns": len(scripted.turns),
        "tools_offered": tools,
        "failures": scripted.failures,
    }


def _bare_type(block: str) -> str:
    """The block id without its state, as perception reports it."""
    return block.split("[", 1)[0]


def predict_fixture_verdicts(ox: int, oy: int, oz: int) -> dict[str, object]:
    """Predict the governance verdict for every log the fixture offers.

    ``_classify_voxels`` is the function the live assessor calls once perception
    has supplied the voxel window, so feeding it the planned layout answers
    "will this read as a tree" without a build-and-collect round trip. The first
    fixture failed here: a bare horizontal row of logs on stone scored 0.6
    (AMBIGUOUS) on ``horizontal_log_pattern`` + ``log_without_tree_evidence``,
    and governance correctly demanded confirmation for every break.

    The verdict is checked along the *depletion order*, not just at full health.
    A collector takes a trunk one log at a time, and the last log standing has
    lost the trunk that made it ``rooted_vertical_log``. If that final break
    would flip to AMBIGUOUS the fixture cannot deliver its full count, so every
    intermediate state has to clear the bar too.
    """
    cells = {pos: _bare_type(block) for pos, block in fixture_cells(ox, oy, oz)}
    logs = sorted(pos for pos, block in cells.items() if block.endswith("_log"))
    rows: list[dict[str, object]] = []
    worst = 0.0
    # Bottom-up removal is the pessimistic order: it strips rootedness from the
    # remaining logs as early as possible.
    remaining = dict(cells)
    for pos in logs:
        block = remaining[pos]
        window = {
            sample: remaining.get(sample, "air") for sample in _sample_positions(pos)
        }
        assessment = _classify_voxels(pos, block, BreakContext.COLLECT, window)
        rows.append(
            {
                "pos": list(pos),
                "block": block,
                "level": assessment.level.value,
                "score": assessment.score,
                "signals": list(assessment.signals),
                "depleted_before": sorted(str(p) for p in cells if p not in remaining),
            }
        )
        worst = max(worst, assessment.score)
        del remaining[pos]
    ambiguous = [row for row in rows if row["level"] != "low"]
    return {
        "log_count": len(logs),
        "worst_score": worst,
        "all_low": not ambiguous,
        "not_low": ambiguous,
        "verdicts": rows,
    }


def phase_predict(args: argparse.Namespace) -> dict[str, object]:
    """Offline gate: refuse to build a fixture governance would have to challenge.

    No RCON, no body, no world writes -- this only reads the planned layout, so
    it is the cheapest place to catch a fixture that misrepresents itself as a
    player structure.
    """
    report = predict_fixture_verdicts(args.ox, args.oy, args.oz)
    failures: list[str] = []
    if report["log_count"] != OAK_COUNT + BIRCH_COUNT:
        failures.append(
            f"fixture offers {report['log_count']} logs, expected {OAK_COUNT + BIRCH_COUNT}"
        )
    for row in report["not_low"]:
        failures.append(
            f"{row['block']} at {row['pos']} would be {row['level']} "
            f"(score {row['score']}, {', '.join(row['signals'])})"
        )
    return {"phase": "predict", **report, "failures": failures}


def forceload_marked(rcon: RconClient, chunks=None) -> set[tuple[int, int]]:
    """Which of ``chunks`` the server currently reports as force-loaded.

    Uses the same per-chunk query ``forceload_add`` uses, so "marked" means the
    same thing in both directions. What the query cannot tell us is *who*
    marked a chunk -- so this is only ever called with a fixture's own chunk
    span, and the caller records those for release. The bound is the fixture
    volume: this probe never releases a force-load outside the area it owns.
    """
    found: set[tuple[int, int]] = set()
    for cx, cz in chunks or ():
        reply = rcon.command(f"forceload query {cx * 16} {cz * 16}").lower()
        if "not marked" not in reply:
            found.add((cx, cz))
    return found


def verify_fixture(body: JavaBody, ox: int, oy: int, oz: int) -> dict[str, object]:
    """Compare the world, cell by cell, against the planned layout.

    A build report says what the runner *asked* for; this says what is there.
    That distinction is the whole reason the phase exists: the run that placed
    the current fixture left no state file, so believing any report about it
    would be believing a story instead of the world. Every cell in the volume is
    checked in both directions -- each planned cell holds its planned block, and
    no unplanned cell holds anything but air -- so a partial build, a stale
    layout, or a leftover from an earlier run all show up as a mismatch rather
    than as a fixture that quietly under-delivers mid-run.
    """
    low, high = fixture_bounds(ox, oy, oz)
    plan = {pos: _bare_type(block) for pos, block in fixture_cells(ox, oy, oz)}
    cells = volume_cells(low, high)
    facts = read_block_facts(body, tuple(cells), page_size=256, failure_label="agent-path verify")
    wrong: list[dict[str, object]] = []
    extra: list[dict[str, object]] = []
    for pos in cells:
        fact = facts.get(pos)
        actual = _bare_type(str((fact.data.get("type") if fact else None) or "missing"))
        actual = actual.removeprefix("minecraft:")
        expected = plan.get(pos)
        if expected is None:
            if actual not in {"air", "cave_air", "void_air"}:
                extra.append({"pos": list(pos), "actual": actual})
        elif actual != expected:
            wrong.append({"pos": list(pos), "expected": expected, "actual": actual})
    logs = {
        "oak_log": sum(1 for block in plan.values() if block == "oak_log"),
        "birch_log": sum(1 for block in plan.values() if block == "birch_log"),
    }
    return {
        "low": list(low),
        "high": list(high),
        "planned_cells": len(plan),
        "checked_cells": len(cells),
        "wrong": wrong[:24],
        "wrong_count": len(wrong),
        "extra": extra[:24],
        "extra_count": len(extra),
        "matches_plan": not wrong and not extra,
        "planned_logs": logs,
    }


def phase_verify(args: argparse.Namespace) -> dict[str, object]:
    """Confirm the standing fixture is exactly the planned one, and record it.

    The state file is rewritten from verified facts, not from a build's promise,
    so ``--phase restore`` afterwards releases exactly the chunks this probe
    marked and clears exactly the volume that was checked.
    """
    prediction = predict_fixture_verdicts(args.ox, args.oy, args.oz)
    rcon = rcon_for(args)
    try:
        ensure_bot(rcon, args.bot)
        low, high = fixture_bounds(args.ox, args.oy, args.oz)
        body, client, _region = ready_body(args.url, args.bot, low, high, rcon)
        try:
            report = verify_fixture(body, args.ox, args.oy, args.oz)
        finally:
            client.close()
        chunks = chunk_span(low, high)
        marked = forceload_marked(rcon, chunks)
        # Only chunks that are both in this fixture's span and currently marked
        # are recorded for release. The world holds force-loads other probes
        # own; releasing one of those would be reaching outside this fixture.
        owned = [chunk for chunk in chunks if chunk in marked]
        report["forceloaded"] = [list(chunk) for chunk in owned]
        report["unmarked_span_chunks"] = [
            list(chunk) for chunk in chunks if chunk not in marked
        ]
        failures: list[str] = []
        if not report["matches_plan"]:
            failures.append(
                f"world differs from plan: {report['wrong_count']} wrong, "
                f"{report['extra_count']} unplanned"
            )
        if not prediction["all_low"]:
            failures.append("standing fixture would be challenged by governance")
        if not failures:
            ensure_bot(rcon, args.bot, pad=(args.ox, args.oy, args.oz))
            rcon.command(f"clear {args.bot}")
            state = {
                "built": True,
                "verified": True,
                "low": report["low"],
                "high": report["high"],
                "placed": report["planned_cells"],
                "forceloaded": report["forceloaded"],
                "oak": OAK_COUNT,
                "birch": BIRCH_COUNT,
                "origin": [args.ox, args.oy, args.oz],
                "bot": args.bot,
                "bot_pos": bot_pos(rcon, args.bot),
            }
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
            report["state_file"] = str(STATE_FILE)
            report["bot_pos"] = state["bot_pos"]
        return {"phase": "verify", **report, "prediction_all_low": prediction["all_low"], "failures": failures}
    finally:
        rcon.close()


def phase_build(args: argparse.Namespace) -> dict[str, object]:
    # An offline prediction is cheaper than a wasted world build: if any log in
    # the planned layout would be challenged as a possible player structure,
    # the fixture cannot deliver its count and there is no point placing it.
    prediction = predict_fixture_verdicts(args.ox, args.oy, args.oz)
    if not prediction["all_low"]:
        return {
            "phase": "build",
            "built": False,
            "reason": "fixture_would_be_challenged",
            "prediction": prediction,
            "failures": [
                f"{row['block']} at {row['pos']} would be {row['level']}"
                for row in prediction["not_low"]
            ],
        }
    rcon = rcon_for(args)
    try:
        ensure_bot(rcon, args.bot)
        low, high = fixture_bounds(args.ox, args.oy, args.oz)
        body, client, _region = ready_body(args.url, args.bot, low, high, rcon)
        try:
            report = build_fixture(rcon, body, args.ox, args.oy, args.oz)
        finally:
            client.close()
        if not report.get("built"):
            return {"phase": "build", **report}
        report["prediction"] = prediction
        # Only now is there a floor to land on.
        rcon.command(f"tp {args.bot} {args.ox + 0.5} {args.oy} {args.oz + 0.5}")
        time.sleep(1.5)
        rcon.command(f"clear {args.bot}")
        report["bot_pos"] = bot_pos(rcon, args.bot)
        report["origin"] = [args.ox, args.oy, args.oz]
        report["bot"] = args.bot
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        return {"phase": "build", **report}
    finally:
        rcon.close()


def phase_restore(args: argparse.Namespace) -> dict[str, object]:
    if not STATE_FILE.exists():
        return {"phase": "restore", "skipped": "no fixture state file"}
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    origin = state.get("origin") or [args.ox, args.oy, args.oz]
    added = [tuple(chunk) for chunk in state.get("forceloaded") or []]
    recorded_low = state.get("low")
    recorded_high = state.get("high")
    if not recorded_low or not recorded_high:
        return {"phase": "restore", "skipped": "state file records no verified bounds"}
    rcon = rcon_for(args)
    try:
        ensure_bot(rcon, args.bot)
        low, high = tuple(recorded_low), tuple(recorded_high)
        body, client, _region = ready_body(args.url, args.bot, low, high, rcon)
        try:
            # Emptying the volume removes the floor under the body. Park it above
            # the volume in spectator first: a survival fake player would fall
            # 150 blocks and die mid-restore, and a dead body cannot serve the
            # perception read that verifies the restore.
            rcon.command(f"gamemode spectator {args.bot}")
            rcon.command(f"tp {args.bot} {origin[0] + 0.5} {high[1] + 4} {origin[2] + 0.5}")
            time.sleep(1.0)
            report = restore_fixture(rcon, body, low, high, added)
        finally:
            client.close()
        # Leaving a spectator hovering over nothing is its own residue; the
        # probe re-spawns the body in every phase, so remove it.
        rcon.command(f"player {args.bot} kill")
        STATE_FILE.unlink()
        return {"phase": "restore", "origin": origin, **report}
    finally:
        rcon.close()


PHASES = {
    "predict": phase_predict,
    "verify": phase_verify,
    "discover": phase_discover,
    "build": phase_build,
    "restore": phase_restore,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", required=True, choices=sorted(PHASES))
    parser.add_argument("--url", default="ws://127.0.0.1:8767")
    parser.add_argument("--bot", default="PathProbe")
    parser.add_argument("--ox", type=int, default=10)
    parser.add_argument("--oy", type=int, default=150)
    parser.add_argument("--oz", type=int, default=120)
    parser.add_argument("--rcon-port", type=int, default=25576)
    parser.add_argument(
        "--rcon-password",
        default=os.environ.get("MINEBOT_REAL_RCON_PASSWORD", "test"),
    )
    parser.add_argument("--ingress-timeout-s", type=float, default=300.0)
    # How long after the navigation stage is emitted the body is taken away.
    # Long enough that the action is genuinely in flight, short enough that it
    # lands before any block could have been collected.
    parser.add_argument("--kill-delay-s", type=float, default=6.0)
    # How long the second lifetime may take to admit the persisted continuation
    # on its own. This scenario deliberately has no operator fallback: a
    # /continue nudge would turn a failed autonomous handoff into a false pass.
    parser.add_argument("--autonomy-grace-s", type=float, default=45.0)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    report = PHASES[args.phase](args)
    out = args.out or (ARTIFACT_DIR / f"agent-path-{args.phase}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True, default=str)[:4000], flush=True)
    print(f"[agent-path] report={out}", flush=True)
    return 0 if not report.get("failures") else 1


def phase_evidence(args: argparse.Namespace) -> dict[str, object]:
    """Capture the real shape of a partial/exhausted collect result.

    The detectors the scenario relies on must quote the runtime, not a guess
    about it, so this phase runs one genuine over-specific stage against the
    fixture and records verbatim what came back.
    """
    moves = [
        Move(label="oak_stage", tool="collect_resource", args={"item": "oak_log", "count": 8}),
        Move(label="observe", text="Recording the returned evidence."),
    ]
    scripted = ScriptedModel(
        sequence_policy(moves), ARTIFACT_DIR / "agent-path-evidence.requests.jsonl"
    )
    server, base_url = start_scripted_server(scripted)
    pad = (args.ox, args.oy, args.oz)
    low, high = fixture_bounds(*pad)
    env = ingress_env(
        base_url=base_url,
        key_env="MINEBOT_SCRIPTED_ENDPOINT_KEY",
        log_path=ARTIFACT_DIR / "agent-path-evidence.trace.jsonl",
        state_db=ARTIFACT_DIR / "agent-path-evidence.db",
        bot=args.bot,
        body_url=args.url,
        low=low,
        high=high,
        pad=pad,
    )
    try:
        def driver(proc, lines):
            wait_for_line(lines, "interactive_ready", timeout_s=90.0)
            send(proc, GOAL)
            wait_for_turns(scripted, 2, timeout_s=args.ingress_timeout_s - 60.0)
            send(proc, "/quit")

        outcome = run_ingress(
            env,
            driver=driver,
            timeout_s=args.ingress_timeout_s,
            interactive=True,
            goal=None,
            out_path=ARTIFACT_DIR / "agent-path-evidence.stdout.txt",
        )
    finally:
        server.shutdown()
        scripted.close()
    rcon = rcon_for(args)
    try:
        inventory = inventory_logs(rcon, args.bot)
    finally:
        rcon.close()
    return {
        "phase": "evidence",
        "model": "scripted",
        "ingress": outcome,
        "model_turns": len(scripted.turns),
        "inventory_logs": inventory,
        "failures": scripted.failures,
    }


PHASES["evidence"] = phase_evidence


# --- Scenario -------------------------------------------------------------
#
# One goal, two process lifetimes, one durable state database. The seven
# behaviours step 5 asks for are not seven separate runs here on purpose: an
# intermediate choice that is never contradicted proves nothing, and a
# checkpoint that is never reloaded by a *different* process proves only that a
# row was written. They are one story because that is the only arrangement in
# which each behaviour is falsifiable.


# The scenario goal is deliberately *not* the fixture's full log count. Oak
# alone must be unable to satisfy it, so that lifetime A cannot finish and
# lifetime B has something real to continue; and it must sit below the total, so
# the few logs a death may scatter do not decide the outcome.
SCENARIO_TARGET = 6
SCENARIO_GOAL = f"collect {SCENARIO_TARGET} logs"
assert OAK_COUNT < SCENARIO_TARGET <= OAK_COUNT + BIRCH_COUNT, "goal must need birch, and be reachable"

# Asking for more oak than exists is the obstacle: the stage cannot succeed, and
# it must fail as an honest partial rather than as an error.
OAK_OVERASK = 8
assert OAK_OVERASK > OAK_COUNT, "the overask must actually exceed the fixture's oak"

CONTINUATION_OBJECTIVE = (
    f"reach {SCENARIO_TARGET} logs in inventory using any log species"
)

_REJECTIONS = ("task_plan_update_rejected", "task_checkpoint_rejected")

# A refusal, not a rejection: the incoming continuation's bounded epoch budget
# is spent or no longer current, so this process may not execute further work
# under it. Observed for the first time when a completion request closed the
# budget and every later body action in that lifetime was refused.
_BUDGET_CLOSED = "continuation_work_budget_closed"

PLAN_OAK_FIRST = [
    {"title": "stage:oak-first -- collect oak logs from the standing oaks", "status": "in_progress"},
    {
        "title": f"Fall back to any log species if oak alone cannot reach {SCENARIO_TARGET}",
        "status": "pending",
    },
    {
        "title": f"Verify {SCENARIO_TARGET} logs against authoritative inventory truth",
        "status": "pending",
    },
]

PLAN_ANY_SPECIES_SUMMARY = (
    f"stage:any-species -- oak is exhausted at {OAK_COUNT}; the goal counts logs, "
    "not oaks, so widen the collect to any log species for the remainder."
)

# Needles quoted from a real runtime result (see --phase evidence), not guessed.
# If the runtime's vocabulary changes, this scenario must fail loudly rather
# than quietly stop checking that the obstacle reached the model.
PARTIAL_NEEDLES = (
    "partial_candidate_targets_exhausted",
    "resource_domain_partial_exhausted",
    "'outcome': 'partial'",
)


def _tool_literal(text: str) -> dict[str, object] | None:
    """Parse a tool result back out of the model surface.

    Tool results reach the model as a Python ``dict`` repr, not JSON, so
    ``json.loads`` cannot read them. The scripted policy needs the real revision
    numbers out of that text: hardcoding a revision would make this scenario
    pass or fail on bookkeeping instead of on behaviour.
    """
    try:
        value = ast.literal_eval(text)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _task_artifact(messages) -> dict[str, object]:
    """The newest task/plan/checkpoint projection the runtime showed the model.

    The projection is nested under ``summary``; an earlier revision of this
    probe read the top level, silently found nothing, and sent stale revisions
    into every optimistic-concurrency check. Both levels are searched now so a
    future move of the key degrades into a visible behaviour change rather than
    into an invisible default.
    """
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        content = message.get("content")
        parsed = _tool_literal(content if isinstance(content, str) else "")
        if parsed is None:
            continue
        summary = parsed.get("summary")
        for holder in (summary if isinstance(summary, dict) else {}, parsed):
            artifact = holder.get("task_artifact")
            if isinstance(artifact, dict):
                return artifact
    return {}


# The rejection carries the revision the caller should have used. Reading it is
# the whole point of reporting it, and it is what a real model has to do.
_CONFLICT_RE = re.compile(r"actual=(\d+)")


def _conflict_correction(text: str) -> dict[str, object]:
    """Turn a revision-conflict rejection into the artifact it implies."""
    match = _CONFLICT_RE.search(text)
    if match is None:
        return {}
    revision = int(match.group(1))
    if "task_checkpoint_rejected" in text:
        return {"task": {"revision": revision}}
    if "task_plan_update_rejected" in text:
        return {"plan": {"revision": revision}}
    return {}


def _revision(artifact: dict[str, object], section: str, default: int) -> int:
    block = artifact.get(section)
    if isinstance(block, dict) and isinstance(block.get("revision"), int):
        return int(block["revision"])
    return default


class StagePolicy:
    """Ordered stages, chosen from the conversation rather than a turn counter.

    A fixed move list breaks the moment the runtime takes a turn of its own --
    a plan review, or the recovery turn this scenario deliberately provokes --
    because every later move would then land on the wrong evidence and the
    mismatch would look like a runtime defect. Deciding from what has already
    been emitted makes an extra runtime turn free and a missing one visible.

    Two conversation signals steer that decision:

    * A turn whose conversation does not end in a tool result is the runtime
      handing control back for a reason of its own. The stage that was in
      flight never got its answer, so it is re-emitted rather than skipped --
      and a one-shot recovery move may run first. Consuming the next stage
      there is what made an earlier run report a widen against an exhaustion
      that had never been observed.
    * A rejected optimistic-concurrency write is retried with the revision the
      rejection itself reports. That retry is real model behaviour, not a
      workaround: the tool reports the current revision precisely so the caller
      can use it.
    """

    def __init__(self, stages, *, on_handover=None) -> None:
        self._stages = list(stages)
        self._on_handover = on_handover
        self._handover_used = False
        self._done: set[str] = set()
        self._retries: Counter = Counter()
        self.emitted: list[str] = []
        self.handovers: list[dict[str, object]] = []
        self.refusals: list[dict[str, object]] = []
        self.notes: list[str] = []

    def _reemit(self, turn: int, why: str) -> str | None:
        # Walk back past the synthetic recovery move: what needs re-emitting is
        # the scripted stage that lost its answer, never the move that was
        # inserted to react to that loss.
        name = next((n for n in reversed(self.emitted) if n in self._done), None)
        if name is None:
            return None
        if self._retries[name] >= 2:
            self.notes.append(f"turn {turn}: giving up on {name} after two retries ({why})")
            return None
        self._retries[name] += 1
        self._done.discard(name)
        return name

    def __call__(self, turn: int, messages, _tools) -> Move:
        last = last_tool_message(messages)
        if _BUDGET_CLOSED in last:
            # Work refused because the incoming continuation's budget is closed.
            # Recorded here because the in-memory turn records drop the message
            # bodies, and because a lifetime that cannot execute work must never
            # be read as having finished on merit.
            self.refusals.append(
                {
                    "turn": turn,
                    "after_stage": self.emitted[-1] if self.emitted else None,
                    "reason": _BUDGET_CLOSED,
                }
            )
        correction: dict[str, object] = {}
        if self.emitted and any(reason in last for reason in _REJECTIONS):
            correction = _conflict_correction(last)
            retried = self._reemit(turn, "revision rejection")
            if retried:
                self.notes.append(
                    f"turn {turn}: retrying {retried} with the revision the rejection "
                    f"reported ({correction or 'none reported'})"
                )

        tail = messages[-1] if messages else {}
        handover = bool(self.emitted) and (
            not isinstance(tail, dict) or tail.get("role") != "tool"
        )
        if handover:
            interrupted = self._reemit(turn, "runtime handover")
            content = tail.get("content") if isinstance(tail, dict) else ""
            self.handovers.append(
                {
                    "turn": turn,
                    "interrupted_stage": interrupted,
                    "role": tail.get("role") if isinstance(tail, dict) else None,
                    "text": (content if isinstance(content, str) else str(content))[:400],
                }
            )
            if self._on_handover is not None and not self._handover_used:
                self._handover_used = True
                self.emitted.append("handover.recover")
                return self._on_handover()

        artifact = dict(_task_artifact(messages))
        artifact.update(correction)
        for name, builder in self._stages:
            if name in self._done:
                continue
            self._done.add(name)
            self.emitted.append(name)
            return builder(artifact)
        return Move(
            label="stages_complete",
            text="Every scripted stage has been emitted; stopping without inventing work.",
        )


def wait_for_stage(policy: StagePolicy, name: str, *, timeout_s: float) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if name in policy.emitted:
            return True
        time.sleep(0.2)
    return False


def lifetime_a_policy() -> StagePolicy:
    """Choose oak, lose the body mid-collect, exhaust the oak, widen, hand over."""

    def recover() -> Move:
        # Emitted once, on the turn the runtime hands control back after a
        # recovery. A fresh body starts empty, so whatever the old one was
        # carrying is on the ground; reclaiming it is what a real Agent would
        # do before deciding the collect fell short.
        return Move(
            label="handover.recover",
            tool="pickup_items",
            args={"expected_items": ["oak_log", "birch_log"], "minimum_count": 1, "radius": 12},
        )

    return StagePolicy(
        [
            (
                "A.read_task",
                # The goal arrived on argv, so a durable task must already exist
                # before the model has done anything. If the goal is not in the
                # conversation, the rest of the scenario is measuring nothing.
                lambda artifact: Move(
                    label="A.read_task",
                    tool="read_task",
                    expect_in_messages=(SCENARIO_GOAL,),
                ),
            ),
            (
                "A.plan_oak_first",
                lambda artifact: Move(
                    label="A.plan_oak_first",
                    tool="update_plan",
                    args={
                        "expected_revision": _revision(artifact, "plan", 0),
                        "summary": (
                            "stage:oak-first -- the goal counts logs of any species; "
                            "start with the closer oaks and keep birch visible as the "
                            "fallback rather than a blocker."
                        ),
                        "steps": PLAN_OAK_FIRST,
                    },
                    expect_in_messages=("task_read",),
                ),
            ),
            (
                "A.oak_overask",
                # Long-running on purpose, and the action the interruption is
                # aimed at: navigation across a force-loaded fixture returns in
                # milliseconds, so a collect is the only stage that is reliably
                # still in flight when the body is taken away.
                lambda artifact: Move(
                    label="A.oak_overask",
                    tool="collect_resource",
                    args={"item": "oak_log", "count": OAK_OVERASK},
                    expect_in_messages=("task_plan_updated",),
                ),
            ),
            (
                "A.premature_complete",
                # The falsification, and it lives in lifetime A on purpose.
                # Oak is exhausted well short of the goal, so this completion
                # request must not complete anything: a checkpoint is only ever
                # a request and Body truth is the authority.
                #
                # It was in lifetime B until a run showed why it cannot be:
                # under an *incoming* continuation, a completion request closes
                # the continuation's work budget, and every later body action
                # in that lifetime is refused with
                # ``continuation_work_budget_closed``. Lifetime A holds no
                # incoming continuation, so the same falsification runs here
                # without deciding whether the scenario can finish.
                lambda artifact: Move(
                    label="A.premature_complete",
                    tool="checkpoint_task",
                    args={
                        "expected_task_revision": max(1, _revision(artifact, "task", 1)),
                        "disposition": "complete",
                        "summary": "Calling this done on the oak alone.",
                        "evidence": ["the oak stage collected what it could"],
                    },
                ),
            ),
            (
                "A.plan_any_species",
                # Replanning, and the single point where the obstacle is
                # asserted: these needles must already be in the conversation
                # that asked for this move, or the runtime never told the model
                # why oak stopped.
                lambda artifact: Move(
                    label="A.plan_any_species",
                    tool="update_plan",
                    args={
                        "expected_revision": _revision(artifact, "plan", 1),
                        "summary": PLAN_ANY_SPECIES_SUMMARY,
                        "steps": [
                            {
                                "title": PLAN_OAK_FIRST[0]["title"],
                                "status": "blocked",
                                "blocker": f"oak candidates exhausted at {OAK_COUNT} of {OAK_OVERASK}",
                                "evidence": ["partial_candidate_targets_exhausted"],
                            },
                            {
                                "title": "stage:any-species -- collect the remaining logs from any species",
                                "status": "in_progress",
                            },
                            PLAN_OAK_FIRST[2],
                        ],
                    },
                    expect_in_messages=PARTIAL_NEEDLES,
                ),
            ),
            (
                "A.checkpoint_continue",
                lambda artifact: Move(
                    label="A.checkpoint_continue",
                    tool="checkpoint_task",
                    args={
                        "expected_task_revision": max(1, _revision(artifact, "task", 1)),
                        "disposition": "continue",
                        "summary": (
                            f"Oak is exhausted at {OAK_COUNT} of {OAK_OVERASK}. Handing over "
                            "a continuation for the remaining logs of any species."
                        ),
                        "next_step": "Collect the remaining logs from any available species.",
                        "evidence": [
                            "collect_resource oak_log returned partial_candidate_targets_exhausted",
                            "the oak candidate domain is exhausted",
                        ],
                        "continuation": {
                            "objective": CONTINUATION_OBJECTIVE,
                            "operation_class": "material",
                            "target_descriptor": {
                                "kind": "resource",
                                "identifier": "logs",
                                "traits": ["any_species"],
                            },
                            "expected_evidence": [f"inventory holds {SCENARIO_TARGET} logs"],
                            "bounded_epoch_budget": 4,
                        },
                    },
                ),
            ),
        ],
        on_handover=recover,
    )


def lifetime_b_policy() -> StagePolicy:
    """Reload someone else's checkpoint, claim done too early, then finish."""
    return StagePolicy(
        [
            (
                "B.read_task",
                # Checkpoint continuation, asserted where it matters: a process
                # that never saw the first one is being handed the first one's
                # plan and continuation objective.
                lambda artifact: Move(
                    label="B.read_task",
                    tool="read_task",
                    expect_in_messages=(
                        SCENARIO_GOAL,
                        CONTINUATION_OBJECTIVE,
                        "stage:any-species",
                    ),
                ),
            ),
            (
                "B.any_species",
                lambda artifact: Move(
                    label="B.any_species",
                    tool="collect_resource",
                    args={"item": "logs", "count": SCENARIO_TARGET},
                ),
            ),
            (
                "B.verify_inventory",
                lambda artifact: Move(
                    label="B.verify_inventory",
                    tool="read_inventory",
                ),
            ),
            (
                "B.checkpoint_complete",
                lambda artifact: Move(
                    label="B.checkpoint_complete",
                    tool="checkpoint_task",
                    args={
                        "expected_task_revision": max(1, _revision(artifact, "task", 1)),
                        "disposition": "complete",
                        "summary": f"{SCENARIO_TARGET} logs are in inventory; requesting completion.",
                        "evidence": [
                            f"read_inventory reports {SCENARIO_TARGET} logs",
                            "collect_resource logs reached the requested total",
                        ],
                    },
                ),
            ),
        ]
    )


def trace_summary(path: Path) -> dict[str, object]:
    """Event-name census, plus whatever the runtime called its recovery.

    The recovery event names are discovered rather than assumed: asserting on a
    name this probe invented would prove the probe can spell, not that the
    runtime recovered.
    """
    events = read_trace(path)
    census = Counter(str(event.get("event") or "") for event in events)
    interesting = {
        name: count
        for name, count in census.items()
        if any(token in name for token in ("recover", "death", "missing", "lifecycle", "reconcil"))
    }
    scope = next((event for event in events if event.get("event") == "runtime_scope"), None)
    reconciliation = next(
        (event for event in events if event.get("event") == "startup_reconciliation"), None
    )
    terminal = next(
        (event for event in events if event.get("event") == "session_terminal"), None
    )
    # Authoritative completion is this event, not the terminal line. Once the
    # runtime completes a task there is no foreground goal left, so a lifetime
    # launched without a fallback goal on argv prints ``goal: ""`` and
    # ``satisfied: false`` *because* it succeeded. Asserting on the printed line
    # made an earlier run report a completion failure that had not happened.
    completed = next(
        (event for event in events if event.get("event") == "session_goal_completed"), None
    )
    return {
        "event_count": len(events),
        "recovery_related_events": interesting,
        "scope_key": None if scope is None else scope.get("scope_key"),
        "goal_completed": None
        if completed is None
        else {key: completed.get(key) for key in ("goal", "reason", "ts")},
        "startup_reconciliation": None
        if reconciliation is None
        else {
            key: reconciliation.get(key)
            for key in ("decision", "event_count", "orphaned_intent_count", "state_missing")
        },
        "session_terminal": None
        if terminal is None
        else {
            "status": terminal.get("status"),
            "lifecycle": terminal.get("lifecycle"),
            "terminal_truth": terminal.get("terminal_truth"),
        },
    }


def task_store_summary(db_path: Path) -> dict[str, object]:
    """Durable task truth, read straight out of the runtime's own database.

    Stronger evidence than either the trace or the terminal line: ``status`` and
    ``completion_authority`` are what the runtime committed, and they survive
    the process that wrote them. The checkpoint ledger is included in full
    because the ordering is the story -- notably whether anything superseded the
    continuation a lifetime handed over.
    """
    if not db_path.exists():        return {"present": False}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tasks = [dict(row) for row in conn.execute("SELECT * FROM tasks")]
        checkpoints = [
            {
                "revision": row["revision"],
                "disposition": row["disposition"],
                "summary": row["summary"],
                "has_continuation": row["continuation_json"] is not None,
                "continuation": None
                if row["continuation_json"] is None
                else json.loads(row["continuation_json"]),
                "body_generation": (
                    json.loads(row["body_fingerprint_json"]).get("generation")
                    if row["body_fingerprint_json"]
                    else None
                ),
                "created_at": row["created_at"],
            }
            for row in conn.execute(
                "SELECT * FROM task_checkpoints ORDER BY revision ASC"
            )
        ]
    finally:
        conn.close()
    return {
        "present": True,
        "task_count": len(tasks),
        "tasks": [
            {
                key: task.get(key)
                for key in (
                    "task_id",
                    "goal_text",
                    "status",
                    "revision",
                    "completion_authority",
                    "source",
                )
            }
            for task in tasks
        ],
        "checkpoints": checkpoints,
    }


def stage_evidence(scripted: ScriptedModel, stage: str) -> dict[str, object]:
    """What the model was actually shown when a stage was emitted.

    The *last* emission is reported, not the first: a stage interrupted by the
    body loss is re-emitted, and it is the re-emission that ran to completion
    and observed what the behaviour claims. ``emissions`` keeps the retry
    visible rather than hiding it behind a single record.
    """
    records = [record for record in scripted.turns if record.get("move") == stage]
    if not records:
        return {"emitted": False, "emissions": 0, "expected": [], "missing": []}
    record = records[-1]
    return {
        "emitted": True,
        "emissions": len(records),
        "turn": record.get("turn"),
        "tool": record.get("emitted_tool"),
        "expected": record.get("expect_in_messages") or [],
        "missing": record.get("missing_expectations") or [],
    }


def _behaviour_table(a: ScriptedModel, b: ScriptedModel, interrupt, truth) -> dict[str, object]:
    return {
        "intermediate_choice": stage_evidence(a, "A.plan_oak_first"),
        "partial_output": stage_evidence(a, "A.oak_overask"),
        "obstacle_feedback": stage_evidence(a, "A.plan_any_species"),
        "replanning": stage_evidence(a, "A.plan_any_species"),
        "interruption_recovery": interrupt,
        "checkpoint_continuation": stage_evidence(b, "B.read_task"),
        "authoritative_completion": truth,
    }


def phase_scenario(args: argparse.Namespace) -> dict[str, object]:
    """One goal across two process lifetimes, against one durable state store.

    Requires a standing, verified fixture (``--phase verify``) and an empty bot
    inventory, because every count here is checked against the server's own
    inventory view rather than against anything the Agent said.
    """
    if not STATE_FILE.exists():
        return {"phase": "scenario", "skipped": "no verified fixture state file"}
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if not state.get("verified"):
        return {"phase": "scenario", "skipped": "fixture state file is not verified"}
    low, high = tuple(state["low"]), tuple(state["high"])
    pad = tuple(state.get("origin") or (args.ox, args.oy, args.oz))

    rcon = rcon_for(args)
    failures: list[str] = []
    try:
        ensure_bot(rcon, args.bot, pad=pad)
        rcon.command(f"clear {args.bot}")
        starting = inventory_logs(rcon, args.bot)
        if sum(starting.values()):
            failures.append(f"inventory did not clear before the run: {starting}")

        state_db = ARTIFACT_DIR / "agent-path-scenario.db"
        trace_a = ARTIFACT_DIR / "agent-path-scenario-a.trace.jsonl"
        trace_b = ARTIFACT_DIR / "agent-path-scenario-b.trace.jsonl"
        for stale in (
            state_db,
            state_db.with_suffix(".db-wal"),
            state_db.with_suffix(".db-shm"),
            # The runtime opens its trace for append. Left in place, a re-run
            # reads the previous run's startup and terminal events and reports
            # on a run that is no longer happening -- which is exactly how an
            # earlier iteration of this probe reported "no failures" against
            # stale rows.
            trace_a,
            trace_b,
            # Not read back by the report -- stdout is captured live off the
            # pipe -- but a crashed run would otherwise leave the previous
            # run's terminal line on disk for a human to misread.
            ARTIFACT_DIR / "agent-path-scenario-a.stdout.txt",
            ARTIFACT_DIR / "agent-path-scenario-b.stdout.txt",
        ):
            stale.unlink(missing_ok=True)

        # --- Lifetime A ---------------------------------------------------
        policy_a = lifetime_a_policy()
        scripted_a = ScriptedModel(policy_a, ARTIFACT_DIR / "agent-path-scenario-a.requests.jsonl")
        server_a, base_a = start_scripted_server(scripted_a)
        interrupt: dict[str, object] = {"killed": False}
        try:
            env_a = ingress_env(
                base_url=base_a,
                key_env="MINEBOT_SCRIPTED_ENDPOINT_KEY",
                log_path=trace_a,
                state_db=state_db,
                bot=args.bot,
                body_url=args.url,
                low=low,
                high=high,
                pad=pad,
            )

            def driver_a(proc, lines) -> None:
                wait_for_line(lines, "interactive_ready", timeout_s=120.0)
                # The kill lands inside the oak collect. Navigation across this
                # force-loaded fixture returns in milliseconds, so a collect is
                # the only stage reliably still in flight; the cost is that the
                # body may be carrying logs, which is why the counts below are
                # bounds rather than equalities.
                if wait_for_stage(policy_a, "A.oak_overask", timeout_s=180.0):
                    time.sleep(args.kill_delay_s)
                    killer = rcon_for(args)
                    try:
                        interrupt["inventory_before_kill"] = inventory_logs(killer, args.bot)
                        interrupt["pos_before_kill"] = bot_pos(killer, args.bot)
                        interrupt["stages_at_kill"] = list(policy_a.emitted)
                        interrupt["response"] = killer.command(f"player {args.bot} kill")
                        interrupt["killed"] = True
                        interrupt["at"] = datetime.now(timezone.utc).isoformat()
                    finally:
                        killer.close()
                else:
                    interrupt["skipped"] = "the collect stage never emitted"
                wait_for_stage(
                    policy_a, "A.checkpoint_continue", timeout_s=args.ingress_timeout_s - 90.0
                )
                # One more model turn ends the run cleanly with plain text.
                wait_for_turns(scripted_a, len(policy_a.emitted) + 1, timeout_s=60.0)
                send(proc, "/quit")

            outcome_a = run_ingress(
                env_a,
                driver=driver_a,
                timeout_s=args.ingress_timeout_s,
                interactive=True,
                goal=SCENARIO_GOAL,
                out_path=ARTIFACT_DIR / "agent-path-scenario-a.stdout.txt",
            )
        finally:
            server_a.shutdown()
            scripted_a.close()
        inventory_a = inventory_logs(rcon, args.bot)

        # The body must be present and survival again before lifetime B; if the
        # runtime's own recovery did not bring it back, that is the finding, and
        # the probe records it instead of hiding it behind a respawn.
        recovered_by_runtime = bot_alive(rcon, args.bot)
        interrupt["body_present_after_lifetime_a"] = recovered_by_runtime
        if not recovered_by_runtime:
            failures.append("body was still missing when lifetime A ended")
            ensure_bot(rcon, args.bot, pad=pad)

        # --- Lifetime B ---------------------------------------------------
        policy_b = lifetime_b_policy()
        scripted_b = ScriptedModel(policy_b, ARTIFACT_DIR / "agent-path-scenario-b.requests.jsonl")
        server_b, base_b = start_scripted_server(scripted_b)
        resume: dict[str, object] = {}
        try:
            env_b = ingress_env(
                base_url=base_b,
                key_env="MINEBOT_SCRIPTED_ENDPOINT_KEY",
                log_path=trace_b,
                state_db=state_db,
                bot=args.bot,
                body_url=args.url,
                low=low,
                high=high,
                pad=pad,
            )

            def driver_b(proc, lines) -> None:
                wait_for_line(lines, "interactive_ready", timeout_s=120.0)
                # No goal on argv on purpose: passing it again would call
                # replace_goal and start a *new* task, which is the one thing
                # that would make this leg prove nothing. The persisted task has
                # to be what resumes, so autonomy gets a bounded chance to admit
                # the continuation. There is deliberately no /continue fallback:
                # this leg verifies autonomous process handoff.
                autonomous = wait_for_turns(scripted_b, 1, timeout_s=args.autonomy_grace_s)
                resume["autonomy_resumed"] = autonomous
                if not autonomous:
                    resume["failure"] = "continuation was not admitted autonomously"
                    send(proc, "/quit")
                    return
                wait_for_stage(
                    policy_b, "B.checkpoint_complete", timeout_s=args.ingress_timeout_s - 90.0
                )
                wait_for_turns(scripted_b, len(policy_b.emitted) + 1, timeout_s=60.0)
                send(proc, "/quit")

            outcome_b = run_ingress(
                env_b,
                driver=driver_b,
                timeout_s=args.ingress_timeout_s,
                interactive=True,
                goal=None,
                out_path=ARTIFACT_DIR / "agent-path-scenario-b.stdout.txt",
            )
        finally:
            server_b.shutdown()
            scripted_b.close()
        inventory_b = inventory_logs(rcon, args.bot)
    finally:
        rcon.close()

    summary_a = trace_summary(trace_a)
    summary_b = trace_summary(trace_b)
    truth_a = summary_a.get("session_terminal") or {}
    truth_b = summary_b.get("session_terminal") or {}
    truth_a_facts = truth_a.get("terminal_truth") if isinstance(truth_a, dict) else None
    truth_b_facts = truth_b.get("terminal_truth") if isinstance(truth_b, dict) else None

    total_b = sum(inventory_b.values())
    completed_b = summary_b.get("goal_completed")
    store = task_store_summary(state_db)
    tasks = store.get("tasks") or []
    task = tasks[0] if len(tasks) == 1 else None
    checkpoints = store.get("checkpoints") or []
    handed_over = next(
        (cp for cp in checkpoints if cp.get("disposition") == "continue"), None
    )
    superseding = (
        None
        if handed_over is None
        else next(
            (
                cp
                for cp in checkpoints
                if cp.get("revision", 0) > handed_over.get("revision", 0)
                and cp.get("disposition") == "yield"
            ),
            None,
        )
    )
    completion = {
        "goal": SCENARIO_GOAL,
        "target": SCENARIO_TARGET,
        "inventory_after_lifetime_a": inventory_a,
        "inventory_after_lifetime_b": inventory_b,
        "server_counted_logs": total_b,
        "task_store": store,
        "continuation_handed_over": handed_over,
        "continuation_superseded_by": superseding,
        "lifetime_a_goal_completed": summary_a.get("goal_completed"),
        "lifetime_b_goal_completed": completed_b,
        "lifetime_a_terminal_truth": truth_a_facts,
        "lifetime_b_terminal_truth": truth_b_facts,
    }

    # Assertions. Each one names the behaviour it defends, so a failure says
    # what stopped being true rather than only that something did.
    scripted_failures = list(policy_a.notes) + list(policy_b.notes)
    total_a = sum(inventory_a.values())
    if not (total_a <= OAK_COUNT and total_a < SCENARIO_TARGET):
        # A bound, not an equality: the interruption is inside the collect, so
        # a death may scatter logs the pickup does not fully reclaim. What must
        # hold is that oak alone left lifetime A short of the goal.
        failures.append(
            f"lifetime A should have ended short of {SCENARIO_TARGET} with at most "
            f"{OAK_COUNT} oak, got {inventory_a}"
        )
    if summary_a.get("goal_completed") is not None:
        failures.append(
            "authoritative completion: lifetime A completed the goal while short of it "
            f"({summary_a.get('goal_completed')})"
        )
    if isinstance(truth_a_facts, dict) and truth_a_facts.get("satisfied"):
        failures.append(
            "authoritative completion: lifetime A reported satisfied while short of the goal"
        )
    if total_b < SCENARIO_TARGET:
        failures.append(f"lifetime B did not reach {SCENARIO_TARGET} logs: {inventory_b}")

    # Checkpoint continuation, asserted on the durable record: one task must
    # have crossed the process boundary, not two.
    if task is None:
        failures.append(
            f"checkpoint continuation: expected exactly one durable task, got {tasks}"
        )
    else:
        if task.get("goal_text") != SCENARIO_GOAL:
            failures.append(
                f"the durable task is not this run's goal: {task.get('goal_text')!r}"
            )
        # Authoritative completion, on the strongest evidence available: the
        # runtime committed a terminal status and named what authorised it.
        if task.get("status") != "completed":
            failures.append(
                f"authoritative completion: the task never reached completed ({task.get('status')})"
            )
        elif task.get("completion_authority") != "body_truth":
            failures.append(
                "authoritative completion: the task completed on something other than "
                f"Body truth ({task.get('completion_authority')})"
            )
    if handed_over is None:
        failures.append("checkpoint continuation: lifetime A never persisted a continue checkpoint")
    elif not handed_over.get("has_continuation"):
        failures.append("checkpoint continuation: the handover carried no continuation contract")
    if resume.get("autonomy_resumed") is not True:
        failures.append(
            "checkpoint continuation: lifetime B did not autonomously admit the durable "
            f"continuation ({resume})"
        )

    # The falsification, checked on the ledger: a completion request was
    # recorded while the goal was unmet, and it did not complete the task. The
    # continue checkpoint that follows it is the proof the task was still open.
    premature = next(
        (
            cp
            for cp in checkpoints
            if cp.get("disposition") == "complete"
            and handed_over is not None
            and cp.get("revision", 0) < handed_over.get("revision", 0)
        ),
        None,
    )
    if premature is None:
        failures.append(
            "authoritative completion: no premature completion request was recorded, so "
            "nothing falsified the claim that a checkpoint can complete a task"
        )

    # Work refused because a continuation's budget was closed means the lifetime
    # could not have finished on merit; that must never be read as a pass.
    for leg, policy in (("A", policy_a), ("B", policy_b)):
        if policy.refusals:
            failures.append(
                f"lifetime {leg}: work was refused with {_BUDGET_CLOSED} — {policy.refusals}"
            )
    if not isinstance(completed_b, dict):
        failures.append(
            "authoritative completion: lifetime B never emitted session_goal_completed "
            f"(terminal line reported {truth_b_facts})"
        )
    elif completed_b.get("reason") != "terminal_truth_satisfied":
        failures.append(
            "authoritative completion: lifetime B completed for a reason other than Body "
            f"truth ({completed_b.get('reason')})"
        )
    if summary_a.get("scope_key") != summary_b.get("scope_key"):
        failures.append(
            "checkpoint continuation: the two lifetimes did not share a runtime scope "
            f"({summary_a.get('scope_key')} vs {summary_b.get('scope_key')})"
        )
    if not interrupt.get("killed"):
        failures.append("interruption/recovery: the body was never taken away")
    if not policy_a.handovers:
        failures.append(
            "interruption/recovery: the runtime never handed control back to the model "
            "after the body was lost"
        )

    return {
        "phase": "scenario",
        "model": "scripted",
        "goal": SCENARIO_GOAL,
        "state_db": str(ARTIFACT_DIR / "agent-path-scenario.db"),
        "lifetime_a": {
            "ingress": outcome_a,
            "stages": policy_a.emitted,
            "handovers": policy_a.handovers,
            "refusals": policy_a.refusals,
            "model_turns": len(scripted_a.turns),
            "surface_failures": scripted_a.failures,
            "trace": summary_a,
        },
        "lifetime_b": {
            "ingress": outcome_b,
            "stages": policy_b.emitted,
            "handovers": policy_b.handovers,
            "refusals": policy_b.refusals,
            "model_turns": len(scripted_b.turns),
            "surface_failures": scripted_b.failures,
            "resume": resume,
            "trace": summary_b,
        },
        "behaviours": _behaviour_table(scripted_a, scripted_b, interrupt, completion),
        "failures": failures + scripted_a.failures + scripted_b.failures + scripted_failures,
    }


PHASES["scenario"] = phase_scenario


if __name__ == "__main__":
    raise SystemExit(main())
