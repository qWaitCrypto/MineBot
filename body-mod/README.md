# MineBot Body Mod Status

This directory currently contains an early Java/Fabric WebSocket proof of
concept. It is **not** the canonical MineBot Body runtime.

Canonical direction:

- Carpet FakePlayer remains the server-authoritative body.
- Scarpet remains the primary server-side Body app for game logic: controllers,
  ownership, events, state, inventory, and verified server mutations.
- Python keeps the stable Body schema and Skill/Brain interface.
- Java/Fabric code is allowed only as a thin bridge or no-downgrade補强 layer
  when Scarpet/RCON cannot reliably cover a required capability.

The existing `MineBotBodyMod.java` creates its own Fabric API fake player and a
WebSocket protocol. That was useful as a probe, but it does not match the
current canonical stack. Do not extend it into a second body implementation.

Acceptable future use:

- expose the same logical envelopes as `minebot/game/protocol.py`;
- provide high-throughput transport for the existing Scarpet Body app;
- expose fields/events Scarpet cannot read;
- support GUI/trade/container mechanisms only when no Scarpet/Carpet path is
  reliable.

Non-goals:

- no independent body state model;
- no second movement/combat controller stack;
- no Brain/Skill schema changes;
- no JS or mineflayer runtime.
