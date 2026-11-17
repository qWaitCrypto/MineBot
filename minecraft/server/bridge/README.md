# MineBot Bridge

This Fabric module is MineBot's optional Minecraft-side bridge. It exposes a
small, typed, loopback-only transport surface for capabilities that do not fit
Scarpet/RCON cleanly, including observer control.

It is not a second Body implementation. Carpet FakePlayer remains the physical
body, and the Scarpet app remains the authoritative server-side data plane and
controller layer.

The bridge must remain narrow:

- no Brain, Skill, or Python Body semantics;
- no independent movement, combat, inventory, or world-state model;
- no arbitrary command or gameplay-action surface;
- no dependency on Carpet internals;
- protocol and version-specific access stay behind explicit adapters.

Build it from the shared Minecraft workspace:

```bash
cd minecraft
./gradlew :server:bridge:build
```
