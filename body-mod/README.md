# MineBot Bridge Mod Status

This directory is becoming the thin Java/Fabric bridge for MineBot advanced
features. It is **not** the canonical MineBot Body runtime: the main action path
remains Python -> RCON -> Scarpet -> Carpet until an explicit Body IPC transport
upgrade is made.

Current entrypoint:

- `dev.minebot.bridge.MineBotBridgeMod`

First formal bridge channel:

- `dev.minebot.bridge.worldstream` — Stage-0 read-only `world-stream` data plane for
  camera/vision: `HELLO`, `SUBSCRIBE{center:entity}`, followed-entity
  `TRANSFORM`, and one center `SECTION_KEYFRAME`.

Canonical direction:

- Carpet FakePlayer remains the server-authoritative body.
- Scarpet remains the primary server-side Body app for game logic: controllers,
  ownership, events, state, inventory, and verified server mutations.
- Python keeps the stable Body schema and Skill/Brain interface.
- Java/Fabric code is allowed only as a thin bridge or no-downgrade補强 layer
  when Scarpet/RCON cannot reliably cover a required capability.

The old `dev.minebot.body.MineBotBodyMod` creates its own Fabric API fake player
and exposes control-style WebSocket messages. It is quarantined under
`attic/` so it is not compiled into the production jar. Do not extend it into a
second body implementation.

Acceptable future use:

- `world-stream`: expose high-throughput read-only world/entity data for camera
  and vision;
- `body-ipc`: optionally expose the same logical envelopes as
  `minebot/game/protocol.py` if RCON is deliberately replaced later;
- `server-facts`: expose fields/events Scarpet cannot read cleanly;
- tightly scoped no-downgrade补强 only when no Scarpet/Carpet path is reliable.

Non-goals:

- no independent body state model;
- no second movement/combat controller stack;
- no world-stream writes (`setBlock`, teleport, fake-player creation, entity
  spawn/discard, inventory mutation, command dispatch);
- no Brain/Skill schema changes;
- no JS or mineflayer runtime.
