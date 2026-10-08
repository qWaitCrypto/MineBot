# Testing MineBot

Use a small behavioral regression for fast feedback, then observe the changed
behavior in a real Minecraft task. Issue #1 P30–P40 sets this approach; test
counts and mock success do not prove that the bot can finish a task.

## Default and CI

```bash
python -m pip install -e '.[test]'
python -m pytest -q
```

The default and CI run the same focused tests: real-client Body mapping,
IPC responses and errors, cancellation, chat preemption, truthful terminal
results, and native Responses protocol/session behavior. These tests
use doubles and require no game or model key. There is no second smoke-test copy.

For a changed subsystem, name its existing test file. For example:

```bash
python -m pytest tests/unit/test_real_client_protocol.py -q
```

Do not add tests that freeze filenames, source strings, class organization,
arbitrary counts, or old tuning constants. Keep behavioral regressions for
known failures and necessary boundaries. `tests/unit` and older root scripts
remain explicitly runnable for changes to those modules; they are not a
mandatory full-suite step for each edit. Pytest normally discovers `test_*.py`;
historical `*_test.py` scripts must be named explicitly.

## Directed real Body tasks

Start the disposable server at `127.0.0.1:25566`, with RCON at port `25576`,
and a real client named `MineBotCombat` with IPC at `8768`. RCON is used by the
experiment for fixture setup and independent server checks. The tested tuple
is Minecraft 26.1.2, Fabric Loader 0.19.3, Fabric API 0.152.1, Java 25 and
upstream Baritone 1.18.0. Client launch instructions are in
[the client README](minecraft/client/body/README.md).

Run the task relevant to the change:

```bash
python -m tests.e2e_real_client_body_transactions \
  --disposable-local-server --case menus
```

| Case | Actual result checked |
|---|---|
| `collection` | Finish two small trees before switching; collect requested stone/iron and part of a giant tree; cancel |
| `navigation` | Cross a stone obstacle, arrive, collect a log into inventory |
| `controls` | Cancel navigation/exploration and stop their processes/pathing |
| `building` | Place four blocks, then clear them; check each server block |
| `farming` | Harvest ripe wheat into inventory |
| `interactions` | Eat, place a block, open a gate, operate a lever |
| `consumption` | Eat bread with server hunger/inventory checks; full-player and cancellation behavior |
| `flight` | Drop-start with Elytra, fly 128 blocks and land within eight blocks of the goal; server flight/position checks |
| `menus` | Craft sticks/pickaxe/cake, move chest items, smelt and reclaim fuel, preserve inventory slots |
| `combat_tools` | Melee and bow/crossbow against the named target; leave a decoy intact |
| `vertical_tools` | Dig down/up, return to surface, enforce bounds, cancel |
| `handoff_tools` | Give items to a second real client and verify its inventory |

The lower-level `vertical`, `handoff`, and `ranged` cases remain useful when
changing those actions. `vertical_denied` needs a client launched without
disposable-world permission. Handoff needs a second client named `Receiver`
with IPC at `8769`. The flight fixture supplies Elytra and rockets and starts
the player in a fall; Body/Baritone owns all subsequent flight and landing.

Each run returns a nonzero exit on failure and writes a timestamped JSON under
`logs/` (or the explicit `--output` path): goal, fixture conditions, tuple,
seed, initial/final state, action/tool terminals, server checks and a concrete
failure reason. Keep failed reports when retrying. A case that stops at its
first failed step has not verified the later steps.

These tasks supply fixtures/resources and call Body actions or tool callables.
They do not prove model planning, conversation, or adaptation.

## Native Responses API experiment

When selecting or diagnosing an endpoint, verify its actual native protocol with:

```bash
python tools/probe_responses_protocol.py \
  --env-file /path/to/provider.env --model gpt-6.1-sol
```

This uses real API calls, without Minecraft. Its JSON distinguishes native
async dispatch, conversation with a pending call and late result delivery,
and reports the concrete failed stage. HTTP is the default; `--transport websocket`
also verifies native mid-turn steering. `--proxy` selects a network
route when needed. The default tests include eight protocol/session fixtures;
their model and Body substitutes do not prove this live API experiment.

An `openai_responses` provider defaults to the new production kernel;
`MINEBOT_AGENT_KERNEL=sdk` explicitly selects the old loop. Use
`MINEBOT_RESPONSES_TRANSPORT=http` (the new kernel default),
or explicitly choose `websocket`. Configuration and current
results are in [RUNTIME.md](RUNTIME.md).

## Real Agent task and observation

The user-authorized visible 30/45/60-minute resource, building and transport
tasks are described in [the task record](docs/exec-plans/completed/visible-agent-tasks-20261008.md).
With the existing disposable server, client and live viewer running:

```bash
python -m tests.e2e_real_client_agent_long_tasks \
  --disposable-local-server --case resources --env-file /path/to/provider.env
```

Choose `camp`, `logistics`, or `all` for the other tasks; repeat `--case` to
select a sequence. Use a fresh output directory for a retry; existing reports
are never overwritten. Limits are maxima;
completed or concretely failed tasks stop early. This runs the production model
and tools, with parent-only fixture setup/server checks and recorded chat.
It is opt-in, never part of default pytest, and does not replace the Body baseline.

The 2026-10-08 runs did not complete any of the three full goals. Their concrete
results, scene corrections and one directly reproduced Body fix are recorded
in [RUNTIME.md](RUNTIME.md). Cold distant chunk generation can disconnect the
client during setup; preserve that setup failure separately from an Agent run.

This collection experiment gives the production Agent a player goal and lets
the real model choose its actions. It starts empty-handed with three placed
logs behind a stone obstacle and checks inventory independently:

```bash
python -m tests.e2e_real_client_agent_collect \
  --disposable-local-server --env-file /path/to/provider.env
```

Model configuration follows the normal production environment. Keep the env
file outside Git. The result, Agent output and model/tool trace are written
together under `logs/agent-collect-*/`. Missing configuration, a model error,
timeout or insufficient inventory is a failed experiment, with its reason.

For the Responses kernel's continuous-task acceptance, use the same experiment:

```bash
python -m tests.e2e_real_client_agent_collect \
  --disposable-local-server --continuity \
  --env-file /path/to/provider.env --model gpt-6.1-sol
```

This mode requires the second real client `Receiver` on IPC `8769`. It verifies
the actual collection call has the native async flag and that its original
result is accepted by the model. It does not repeat the synthetic API probe.
`--transport http` is the default; `websocket` remains an explicit option. The production
interactive Agent chooses its own tools. The parent sends ordinary chat and
an appended return request during server-observed movement in a running collection
call, checks the original
call's successful result and API acknowledgement, then verifies Receiver's
inventory and the return position through RCON. A separate navigation goal is
cancelled after server-observed movement; cancellation must settle and position
must remain stable while the Agent is still alive. Messages, receipts and server
checks are recorded with the result. This mode passed live acceptance with
gpt-6.1-sol over HTTP in 83.779 seconds; see [RUNTIME.md](RUNTIME.md) for the
actual report and remaining WebSocket status.

For other player goals, use the existing interactive production entrypoint
with `MINEBOT_BODY_PROVIDER=real_client`, a configured model, client endpoint,
bot name and natural region:

```bash
python -m minebot.app.real_server_session --interactive
```

Observe the goal → short plan → actions → world result. During the same task,
ordinary chat should leave work running; `/cancel` should stop it. Introduce
one actual obstacle when evaluating strategy changes. Record the initial
conditions, messages, interventions, actual outcome and reason for failure.
These are Agent experiments, not additional Body completion requirements.
One collection success does not establish those other behaviors.

## Historical experiments

Old Java/FakePlayer and Scarpet scripts remain evidence for their respective
routes. Run a relevant script explicitly, or select one legacy runner case:

```bash
python tests/run_e2e.py --filter furnace --required --fail-on-skip
```

`--all` is required to intentionally run that whole legacy matrix. An old
unchecked plan or failing legacy scenario does not select current work.

After a coherent change, run its focused regression and relevant real task.
Once the result is established, stop; broaden testing only for a new failure
or a specific unresolved risk.
