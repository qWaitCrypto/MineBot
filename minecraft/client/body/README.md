# MineBot real-client Phase A probe

This module is a disposable substrate experiment for Issue #1. It starts a
real Fabric client with upstream Baritone and emits a small log stream marked
`MINEBOT_PHASE_A`. It is not a production Body adapter, does not implement the
Python Body contract, and does not replace the current server Body.

The pinned candidate is Minecraft `26.1.2`, Fabric Loader `0.19.3`, Fabric API
`0.152.1+26.1.2`, Java `25`, and Baritone `v1.18.0`. The Baritone jar is fetched
from its official release and checked by SHA-1 during the Gradle build.

## Run against the disposable local server

Start MineBot's local test server first, then run from `minecraft/`:

```bash
export MINEBOT_CLIENT_SERVER=127.0.0.1:25566
export MINEBOT_CLIENT_USERNAME=MineBotPhaseA
export MINEBOT_CLIENT_IPC_PORT=8768
xvfb-run -a ./gradlew :client:body:runClient --no-daemon --console=plain
```

After the client reports readiness, send a bounded `goto:x:y:z` or
`mine:block_name` request through `minebot.game.real_client_ipc.RealClientIpc`.
The adapter returns the accepted/progress/terminal frames and can request a
cancel. Set `MINEBOT_CLIENT_EARLY_START=true` together with
`MINEBOT_CLIENT_OBJECTIVE=mine:oak_log` only to reproduce the pre-readiness
failure; production-facing experiments must submit through IPC after ready.

The first local run may require setting `onboardAccessibility:false` in the
ignored `client/body/run/options.txt`; this is test-client state and is not a
repository setting. The server-name resolver mixin only bypasses Mojang's
external address blocklist for this local test process.

## Current evidence

The loopback Phase A probe now waits for a loaded feet chunk and a grounded
player before accepting work. Python can then send one `goto` or `mine` target
and receive a structured terminal with inventory delta, process state, path
events, and cancellation latency. The controlled fixture proves global
`mine:oak_log` after readiness; starting the same request from the JOIN callback
still reproduces the old search failure.

The IPC and Java code remain disposable experiment code. It is not the
production Body contract, and it does not claim that Builder, Farm, Follow,
Explore, Elytra, combat, reconnect, or multi-client Baritone behavior is
integrated. See the [Phase A evidence record](../../../docs/references/client-body-baritone-phase-a-evidence-20261006.md)
for the exact measurements and promotion limits.
