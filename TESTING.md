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


## Proposed one-hour task: a camp from an empty inventory

Status: task design only; not run. The next model run uses this card after the
maintainer chooses to start it. Reuse the current Responses HTTP kernel, real
client and visible viewer; do not add a new test runtime.

初始条件：全新 disposable 世界，固定并记录种子；生存、和平难度、正常
昼夜与随机刻，满生命/饱食；背包、护甲、副手、经验均为空。不开物品赠送、
奖励箱或预建营地，不继承旧实验产物。开跑前只检查出生附近确有树林、
露石和可用的自然建造地，记录出生点 B 和空地位置，不提供矿脉坐标。
若默认出生条件不符，就在开跑前明确选定自然出生区域并披露；不在运行中
传送补救。这个限定先检验连续办事，不同时加入怪物生存考题。

给 Agent 的目标：

> 从空库存开始，在出生点 B 附近建一个可用的小营地。造出并保留一把铁镐、
> 一把铁斧；建一间外轮廓 5×5、内部至少两格净高的小屋，地板、屋顶和四面
> 墙完整，留两格高入口，入口可实际通过。屋里放工作台、熔炉和箱子，安装
> 四支火把。箱里存好 16 根任意原木、64 圆石和 4 铁锭，消耗过的材料不算
> 最终储备。所有资源自己采集、加工；完成后回门口，汇报实际物资和营地坐标。
> 一小时是上限，提前完成就结束。你自己安排采集、工具升级、熔炼和建造次序。

这是一个连续目标，不按分钟强制切阶段。观察时大致关注：前 15 分钟能否
形成木石工具/熔炉生产链；随后能否完成炼铁和铁工具；后半程能否建成、
补齐储备并返回。时间点是诊断参考，不是分数线，也不要求模型机械照表走。

运行中只通过真实用户消息干预：在一次实际长动作进行时问“做到哪一步了？”，
检查回复时原动作仍在；约 20 分钟追加“箱里的原木改成 24 根”，最终按
24 验收。若提前结束，就在结束前的工作阶段追加，不重启目标。自然障碍
如河流、坡地或采矿失败只观察 Agent 是否调整；不为凑恢复事件临时造墙。
完成后若还有时间，单独让它走向营地外 100 格，实际移动后明确取消，确认
旧控制停止。若做到一小时仍未完成，就取消当前工作并按缺少的实际结果记失败。

验收只看实物：服务器核对箱子和背包；检查屋顶、地板、围墙、入口及三个
工作方块，观察角色实际进入和返回。聊天/追加/取消从现有 trace 和实际动作
核对。成功就写完成及耗时；失败就写缺少什么、最后失败调用和主要耗时去向。
从零运行途中不补物资、不改代码、不重启拼接成功；需要修复就保留这次失败，
直接复现对应调用后另开一轮。RCON 在开跑后只读，Agent 不得到 RCON。

本轮用现有 JSONL、任务日志和开头/末尾状态即可；无需扩建评分、Gate 或
测试平台。模型调用/等待、实际采集/施工及工具内部停等从已有时间线区分。
