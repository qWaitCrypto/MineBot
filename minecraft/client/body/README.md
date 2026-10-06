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
export MINEBOT_CLIENT_OBJECTIVE=goto:20:70:0
xvfb-run -a ./gradlew :client:body:runClient --no-daemon --console=plain
```

Supported launch objectives are `goto:x:y:z` (or `navigate:x:y:z`),
`mine:block_name`, and the directed fixture form
`mineat:block_name:x:y:z`. The latter first navigates below the specified
block, then invokes the same Baritone mining process; it isolates collection
from global target search. Set `MINEBOT_CLIENT_CANCEL_AFTER_TICKS` to a
positive value to cancel the active path after that many client ticks.

The first local run may require setting `onboardAccessibility:false` in the
ignored `client/body/run/options.txt`; this is test-client state and is not a
repository setting. The server-name resolver mixin only bypasses Mojang's
external address blocklist for this local test process.

## Current evidence

Coordinate navigation, cancellation, and directed collection have been
observed against the local server. The directed probe reached the target and
reported `collection_reached` with one `oak_log` in the client inventory.
Global `mine:block_name` search still reports repeated Baritone path failures,
so search remains an open Phase A blocker. Do not treat this module as
selected for production until that result and the remaining cost measurements
have a concrete decision.
