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
| `attention` | Visible path turns and wraparound, high-block aim, selected moving follow target, menu, explicit look, cancel and setting restoration |
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


## Proposed open-ended task: a 10×10 two-storey house from an empty inventory

Status: task design only; not run. The next model run uses this card after the
maintainer chooses to start it. Reuse the current Responses HTTP kernel, real
client and visible viewer; do not add a new test runtime.

初始条件：全新 disposable 世界，固定并记录种子；生存、和平难度、正常
昼夜与随机刻，满生命/饱食；背包、护甲、副手、经验均为空。不开物品赠送、
奖励箱或预建营地，不继承旧实验产物。开跑前只检查出生附近确有树林、
露石和可用的自然建造地，记录出生点 B，不提供矿脉坐标。
若默认出生条件不符，就在开跑前明确选定自然出生区域并披露；不在运行中
传送补救。这个限定先检验连续办事，不同时加入怪物生存考题。

给 Agent 的目标：

> 从空库存开始，在出生点 B 附近自行选址，建成外轮廓 10×10 的双层小屋。
> 两层都要能实际站立、行走和使用，地板、楼板、外墙和屋顶完整，入口可通过，
> 楼梯连接两层。合理安排工作台、熔炉、储物箱、床和照明等屋内设施，让工作、
> 储物和休息空间好用，门口、楼梯及通道保持畅通，设施能实际接近和使用。
> 材料、外观、门窗、房间布局和设施数量由你决定。所有资源自己采集、加工；
> 工具按施工需要制作和升级，不限定采集、储备或设施的物品数量。
> 没有总时限，持续推进直到建成。完成后实际走进屋内、上下楼检查，回到门口，
> 汇报房屋坐标、布局和实际完成情况。

这是一个连续目标，由 Agent 自己安排采集、加工、施工和补料次序。
不设分阶段截止时间，不要求最终保留指定工具或库存，也不因超过一小时
取消或判失败。耗时只记录，不作为验收门槛。

观察重点是它能否围绕房屋持续推进：按实际缺料采集、利用已有物资、
调整施工顺序、处理地形及失败动作，最终把两层房屋和内部设施做完。
运行中在一次实际长动作进行时通过真实用户消息问“做到哪一步了？”，
核对回复时原动作仍在；不插入固定物品数量的追加要求，不为凑恢复事件
临时造障碍。用户明确取消时停止；本任务不安排到时自动取消。

验收只看实物：服务器核对房屋外轮廓、两层地板与净空、屋顶、外墙、
入口和楼梯；观察角色从门口进入、上下楼并接近设施，确认设施位置可用，
不会堵住通道。背包和箱子用于核对物资及施工事实，不设储备数量分数线。
成功就记录房屋、布局、实物检查和实际耗时；没建成就说明缺少什么，
记录最后的具体失败调用、当时状态及主要耗时去向，不把局部成果算作完成。

从零运行途中不补物资、不改代码、不重启拼接成功；需要修复就保留这次
未完成记录，直接复现对应调用后另开一轮。RCON 在开跑后只读，Agent
不得到 RCON。本轮用现有 JSONL、任务日志和开头/末尾状态即可；无需扩建
评分、Gate 或测试平台。模型调用/等待、实际采集/施工及工具内部停等从
已有时间线区分。
