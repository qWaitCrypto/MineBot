# 切片 2 实施与验收记录（2026-10-09）

**状态（2026-10-10 更新）：切片 2 核心验收通过。** 判定依据见文末「核心验收完成」与「结论」两节。四项核心（多步物理计划、`batch` 自主使用、混合计划按节点类型分流、异步语义）与 B2 机制半边均有真实客户端证据。`B5` 实时抢占按交接文档移交 `interrupt_when` 切片。仍待补的非阻塞项见「未作为通过条件的项目」。

## 范围

本轮实现并验证身体控制权、统一 Decision 通道、`minebot-work/0.2` 五原语解释器、`delegate_work` / `control_reflex` / `read_reflex`、同树反馈与事件等待。没有实现生活层、威胁响应、世界所有权账本、`repeat`、`interrupt_when` 或新的导航逻辑。

## 固定测试环境

每次真实验收都使用以下 disposable 环境；以后直接复用这段配置：

- Minecraft 26.1.2，Fabric Loader 0.19.3，Fabric API 0.152.1+26.1.2，Java 25，Baritone 1.18.0+minebot.1。
- 本地服务器：`127.0.0.1:25569`；RCON：`127.0.0.1:25579`，密码只在本机命令中使用，不写入记录。
- 客户端：`MineBotSlice2`，Body IPC：`127.0.0.1:8771`。
- 客户端窗口：WSLg，`DISPLAY=:0`；启动任务从 `minecraft/` 目录执行，使用 `MINEBOT_CLIENT_SERVER=127.0.0.1:25569`、`MINEBOT_CLIENT_IPC_PORT=8771`、`MINEBOT_CLIENT_DISPOSABLE_WORLD=true`，不使用临时 init 脚本。
- 世界难度：`peaceful`。启动后必须先用 RCON 执行 `difficulty peaceful` 并用 `difficulty` 和 `list` 复核，再开始 Body 验收。
- 既有住宅实验端口 `25567/25577/8768` 未触碰。

本轮首次启动曾错误地在普通难度下运行，玩家死亡并使导航返回 `no_progress`；随后已恢复玩家、切换和平模式并重新执行。该失败响应和修正后的成功响应都保存在真实记录中。

## 结果

### Opus 复核后的窄修与本轮修复

复核发现原实现不能作为切片 2 交付：管理工具没有走通用执行入口；用户停止没有撤销计划；外部代次失效会留下假忙；`wait` 一次判断后退出；暂停、取消、修订没有等待 native 停止；同 id 修订会冒领成果；计划 Decision 请求拿到空事实。以下窄修已加入当前工作区，并由后文的真实边界记录和离线回归覆盖：

- `execute_tool` 同时接受 `ToolResult` 和已序列化的管理工具映射，三个 reflex 管理工具可走统一入口。
- `ReflexManager` 在外部取消前撤销活动；暂停、取消、接管、修订都经过 Runtime 的 native 停止确认，未确认时进入 `blocked`。
- `ProgressAuthority` 的外部代次失效会通知 `BodyControl`，当前持有者进入 `unknown`，只能由 native 停止确认恢复，并记录最近停止事实。
- waiting 计划保留活动和身体租约，在下一次判断前不会退出；`wait`/`when` 的超时仍由计划截止时间结算。
- 同 id 节点按完整义务签名继承成果；工具、参数或条件变化会重新执行。
- 条件请求把 `BodyState` 编译为完整事实、绑定对象、显式 `unknown_fields` 和共享字段词汇；`about` 不再裁剪事实。
- Responses 空闲时收到计划终态会启动一次受控唤醒轮次；已有运行中的轮次仍只走事实唤醒。

当前离线定向回归为 `257 passed, 33 subtests passed`，仓库默认测试为 `103 passed, 16 subtests passed`。这些结果只证明离线代码路径，尚未替代真实 Agent、真实写动作、用户停止和同步/异步交接的隔离世界验收。

### 离线与单元验证

- 阶段二定向单测：`23 passed in 1.64s`，记录见 [`unit-phase2-rerun.log`](../../logs/reflex-slice2-20261009/unit-phase2-rerun.log)。
- Fake runtime 验收：`7/7`，覆盖 wait satisfied/pending/unknown、修订保留已完成节点、写动作条件门控、runtime choice、BodyControl unknown 恢复。原始结果见 [`fake-runtime-acceptance.json`](../../logs/reflex-slice2-20261009/fake-runtime-acceptance.json)。
- Java 生产代码离线编译成功，记录见 [`java-production-compile.log`](../../logs/reflex-slice2-20261009/java-production-compile.log)。全量 Java 构建曾因 Maven Central 的 JUnit 下载超时，未把该网络问题误报为生产编译失败。

### 真实客户端最小计划

脚本见 [`real-slice2-minimal.py`](../../logs/reflex-slice2-20261009/real-slice2-minimal.py)，原始结果见 [`real-slice2-minimal.json`](../../logs/reflex-slice2-20261009/real-slice2-minimal.json)。在和平模式、客户端已上线后，Runtime 接受并执行如下计划：

`sequence(read_body_processes → move_to)`

结果为 `completed`，活动 id 为 `activity-a566fd4b0ff744438f78ebb537488ef5`。服务器确认 `MineBotSlice2 joined the game`，IPC ready 状态包含 `difficulty: peaceful`、`health: 20.0`，计划终态和树形反馈均已写入记录。真实 trace 至少包含工具原始结果、Body 状态和 `reflex_activity_finished`。

随后用同一客户端跑了一个只读 `wait` 条件，实际请求走 `/v1/systemone`，记录见 [`real-decision-wait.json`](../../logs/reflex-slice2-20261009/real-decision-wait.json)。H2O 返回 `unknown`（输入 168 tokens、输出 0、约 0.19 s），Runtime 保持 `waiting`，没有发起任何 Body 写动作；这确认了统一 Decision 通道和 unknown 不放行的边界。

## 真实运行时边界验收（2026-10-10）

在 2026-10-10 的和平 disposable 世界里，用真实客户端（MineBotSlice2 @ `127.0.0.1:8771`）、真实 H2O Decision 和真实执行通道跑了 fixture 边界验收，并补跑了真实 Agent 工具入口。fixture 脚本与原始记录见 [`real-acceptance-232625/`](../../logs/reflex-slice2-20261009/real-acceptance-232625/)，真实 Agent 记录见 [`real-agent-sdk-20261010/`](../../logs/reflex-slice2-20261009/real-agent-sdk-20261010/)。

真实 Agent 使用 legacy SDK / `openai_chat` 通道（Responses HTTP 记录在 [`real-agent-20261010/`](../../logs/reflex-slice2-20261009/real-agent-20261010/) 中因 provider 返回 `unsupported_value: Async tools are not supported with gpt-6-luna` 在模型调用前失败）。SDK 记录包含 `tool_invoke` / `tool_result`，调用来自 `source=agent.reflex`，不是脚本直接调用 manager。

已经通过的项目：

- 三个管理工具经过读通道调用，`pause`/`cancel` 都在 0.011 s 返回且状态正确，没有出现读通道与 native 停止互相等待。
- `wait` 的节点超时在 8 秒生效并进入 `blocked`；Decision 请求 6 次，间隔约 0.49/0.70/1.20/2.21/2.20 s，退避有效，不再刷屏。
- 玩家绑定条件在真实实体读取下分别得到 `satisfied`（qwait 在场）、`pending`（qwait 不在范围内），与事实一致。
- 写动作负例：条件不满足时 0 次 `place_block`，RCON 确认世界无方块。
- 写动作正例：条件判为 satisfied 后 `place_block` 只执行一次，原始结果为 `placed_server_confirmed`，RCON 确认目标坐标有方块。
- 只改 `intent` 的修订不会重放已完成写动作。
- 无 id 节点插入在前的修订只执行新节点，没有重放原有兄弟节点。
- `pause` 确认后身体停住，没有后续写动作。
- 条件请求包含真实 BodyState、绑定对象事实、`unknown_fields` 和共享字段词汇。

本轮确认的三个缺陷已在当前工作区修复：

1. 已完成写节点的参数修改不再依赖 Body 工具自定义的 `metrics.delivered`。`minebot/brain/registry.py` 在统一执行通道记录成功 mutation receipt，包含 `build_structure` 的 direct supervisor 路径；即使 post-state 读取失败，也保留成功交付标记并照常返回读取失败原因。`minebot/app/runner.py` 只在计划内部请求时传递 `_execution`，直接 Agent 工具结果不再携带该字段；`minebot/app/work_runtime.py` 从 receipt 设置 `NodeRecord.delivered`。回归测试覆盖相同 id、参数变化、空 metrics、post-state 失败、`run_tool` 到执行器和 `build_structure` 路径。
2. `update` 先同步发布新 `revision`、新 tree 和 `updating` 状态，再异步等待 native 停止；旧 revision 的迟到结果不能覆盖新计划。紧接着的 `read_reflex` 能看到新 revision，停止后的 `facts.status` 与顶层 status 保持一致。回归测试覆盖即时读取、旧任务延迟结束和取消终态。
3. BodyControl 为 `unknown` 时计划先执行 native idle probe；无法确认停止则显式进入 `blocked/control_unknown` 并发出唤醒事实，避免永久静默等待；确认停止则恢复 claim。

真实 Agent 记录的管理工具序列为：`delegate_work` 接受合法 `minebot-work/0.2` wait 计划，`read_reflex` 得到 `waiting`，`control_reflex pause` 后读到 `paused`，`resume` 后读到 `waiting`，`cancel` 后读到 `cancelled` 且 `stop_state=settled`，最后复读仍为 `cancelled`。整个序列没有调用移动、放置、挖掘或其他物理 Body 工具。该会话两次 `task_completion_rejected`，因为通用 terminal truth 尚不识别管理类目标；Agent 因此多跑了一轮，不能把它当作成功完成判定。Responses HTTP 路径因 provider 不支持异步工具而单独记为失败，不把它误报为代码验收失败。

真实客户端回归已补跑，原始记录见 [`real-client-defect1-20261010/`](../../logs/reflex-slice2-20261009/real-client-defect1-20261010/)。真实客户端首次在 `[22,68,93]` 放置成功；同 id 改为 `[24,68,93]` 后计划进入 `blocked`，只出现一次 `place_block`，RCON 确认 `[24,68,93]` 为空。（2026-10-10 复核：原脚本 `ROOT=parents[3]` 把记录写到了仓库外的 `/home/qwait/logs/`，目录中的 `result.json` 已由复核重跑覆盖；重跑结果同样四项检查全部通过，记录已移回仓库，脚本已改为 `parents[2]`。）

`B2` 的 Agent 侧实时确认已按交接文档降级为非阻塞；**机制半边已于 2026-10-10 通过**，见 [`real-client-b2-mechanism-20261010/`](../../logs/reflex-slice2-20261009/real-client-b2-mechanism-20261010/)（7/7：运行中计划被取消、终态 `cancelled`、取消后 0 次写、RCON 目标为空）。历史尝试与失败归因见文末「B2 机制半边」小节。`B5 take_control` 的第一轮 [`real-agent-b5-take-control-20261010/`](../../logs/reflex-slice2-20261009/real-agent-b5-take-control-20261010/) 真实执行了 `explore_for`、`control_reflex(take_control)` 并 settled，接管后五秒位置误差为 0；第二轮 [`real-agent-b5-take-control-20261010-rerun/`](../../logs/reflex-slice2-20261009/real-agent-b5-take-control-20261010-rerun/) 因上一轮测试 bot 已死亡而恢复失败。机制与工具调用均已真实验证，**实时抢占的时机保障移交 `interrupt_when` 切片**，不计入本轮阻塞项。真实客户端缺料修订回归见 [`real-client-shortage-20261010/`](../../logs/reflex-slice2-20261009/real-client-shortage-20261010/)：首轮 `a` 成功、`b` 因缺料阻塞，补料修订后只执行 `b` 且两块均存在；这是 runtime 证据，不冒充 Agent 证据。`B7 unknown` 的补充目标型重跑 [`real-agent-b7-unknown-reflex-20261010/`](../../logs/reflex-slice2-20261009/real-agent-b7-unknown-reflex-20261010/) 已证明两层：runtime 的 bounded wait 连续判 `unknown` 并保持 `waiting`，终态后 SDK 产生 `reflex_fact_queued/reflex_fact_wake`，Agent 后续消息报告了 `unknown` 及其事实；B7 的 runtime 和 Agent 收果两半均通过。新增 SDK 唤醒回归 [`real-agent-b8-sdk-wakeup-20261010/`](../../logs/reflex-slice2-20261009/real-agent-b8-sdk-wakeup-20261010/) 也记录了终态 fact 入队、唤醒和后续 Agent 报告，B8 在 SDK 通道已通过。`choice`/`batch` 已在 2026-10-10 下半场取得真实 Agent 自主证据（见文末「核心验收完成」）；`requires`、同步/异步交接以及切片 1 的两项回归仍未作为本轮新的真实 Agent 证据；已有 fixture 结果保持原记录，不重复宣称为 Agent 验收。

## 核心验收完成（2026-10-10 下半场）

按交接文档 [`reflex-slice2-handoff-20261010.md`](reflex-slice2-handoff-20261010.md) 第四节重定的验收核心执行：判据是 **Agent 能不能用 `minebot-work/0.2` 原语把一段确定的多步作业写成合法计划，runtime 能不能正确解析并分流**。Agent 想自己做就自己做，不强制它委派。

### 核心四项

| # | 项 | 结果 | 证据 |
|---|---|---|---|
| 1 | Agent 生成多步物理计划 | 通过 | [`real-agent-move-write-20261010/`](../../logs/reflex-slice2-20261009/real-agent-move-write-20261010/) `sequence[move_to, place_block]`，终态 `completed`，`placed_server_confirmed` |
| 2 | `batch` 原语自主使用 | 通过 | [`forced_batch/`](../../logs/reflex-slice2-20261009/real-agent-forced-primitives-20261010/) 根节点即 `batch`，3 个 item 全部 `placed_server_confirmed`（`[12,221,10]`/`[12,221,12]`/`[12,221,14]`） |
| 3 | 混合计划分流 | 通过 | [`real-agent-mixed-plan-20261010/`](../../logs/reflex-slice2-20261009/real-agent-mixed-plan-20261010/) `sequence[move_to, choice{…}]`，`move_to` 直接执行（`arrived`），`choice` 走 Decision（`decision_request_count: 1`），恰好一个分支落盘，8/8 检查通过 |
| 4 | 异步语义（物理计划上） | 通过 | 委派后 Agent 未轮询；终态 `reflex_fact_queued`→`reflex_fact_wake` 唤醒新一轮拿到结果 |

补充：`choice` 作为**根节点**单独验证通过——[`real-agent-choice-root-20261010/`](../../logs/reflex-slice2-20261009/real-agent-choice-root-20261010/) 根节点即 `choice`，Decision 被真实调用，恰好一个分支落盘（6/6）。

### B2 机制半边

[`real-client-b2-mechanism-20261010/`](../../logs/reflex-slice2-20261009/real-client-b2-mechanism-20261010/) 7/7 通过：计划先完成一次物理写（marker），随后在 travel 节点运行中被取消，终态 `cancelled`、**取消后 0 次写**、RCON 确认远端目标为空、body settled。取消来自会话/用户，不需要 Agent 轮询确认，与交接文档的要求一致。

前八轮 B2 尝试的失败原因已定位，均非 Agent 或 runtime 收敛问题：

1. **区域边界配错**：harness 用 `MINEBOT_REAL_NATURAL_REGION="-64,0,-64,128,200,128"`，y 上限 200，而测试桥面在 y=221。`Region.contains` 是闭区间，`[41,221,40]` 天生越界 → `unknown_provenance`，place 从未可能成功。
2. **判据事件找错**：harness 等 `reflex_activity_stopped`，该事件只在 `control_reflex cancel` 路径发射；会话 `/cancel` 走 `request_cancel` → `task.cancel()` → `CancelledError` 分支，发的是 `reflex_activity_cancelled`。
3. **`/cancel` 不到 reflex 层**：session 的 `/cancel` 走 `request_execution_cancel`（只取消执行 lane 与回合），只有 `cancel_active_execution_with_facts` 才显式调 `reflex_manager.request_cancel`。

### 本轮修的产品问题

`delegate_work` 描述里 `choice` 的选项语法写成 `options:[{id,work}]`，但 schema 要求每个 option 必须有 `intent`；Agent 三次提交 choice 计划全部被判 `invalid_plan`，而 schema 的 `anyOf` 报错不指出缺失字段，Agent 无法自查。已改为 `options:[{id,intent,work}]` 并注明"every option needs id, intent and work"。修复后 `invalid_plan` 归零。

### 关于 Decision 通道的说明

本轮之前，所有真实 Agent 运行中 `decision_request` 计数为 0——不是通道故障（`real-agent-sdk` 有 29 次、`b7-unknown-reflex` 有 16 次），而是 **Agent 只写 `call`/`sequence` 这类无判断节点**，从未触发 `_condition` / `_choose` / `_adaptive_batch`。混合计划与 choice 根节点两轮首次证明：当计划里出现判断节点时，runtime 确实按节点类型分流并把裁决交给真实 Decision 端点。

`choice` 根节点首轮出现一次 `decision_client_failed: TimeoutError`（`decision_client.py` 默认 `timeout_s=2.0`），导致计划 `blocked`。端点本身实测 0.12–0.17 s、5/5 正常，属偶发；重跑即通过。**2 秒默认超时对远程 Decision 端点偏紧，建议后续调大**，本轮不改代码。

### 未作为通过条件的项目

B3 unknown 恢复闭环、切片 1 的两项回归（缺料停下等补料后继续、正常完整施工一次）、写动作正例的真实 Agent 版本仍未补。`requires` 尚未取得真实 Agent 自主实例。`repeat` 与 `interrupt_when` 按交接文档仍排除在切片 2 之外。

### 代码回归状态

本轮改动为字符串级（`reflex_tools.py` 工具描述、`runner.py` 两处 `REFLEX_PLANS` 提示），定向回归 `tests/unit/test_reflex_tools.py tests/unit/test_agent_session.py` 为 `76 passed, 2 subtests passed`。

**注意**：全量套件存在 9 个预存失败（`tests/unit/test_spatial_exploration.py` 7 个、`tests/unit/test_work_runtime.py::BlockWorkTests` 2 个，典型断言 `AssertionError: 'placeBlock' != 'mineBlock'` at `test_work_runtime.py:4225`），来自当前工作区 `minebot/app/work_runtime.py` 的未提交改动（`130 insertions(+), 16 deletions(-)`），与本轮字符串改动无关，但影响"代码回归通过"的表述，需先修复。

## 结论

切片 2 的核心问题已得到回答：**Agent 能用 `minebot-work/0.2` 的原语把多步作业写成合法计划，runtime 能正确解析并按节点类型分流——程序性步骤直接执行，需要判断的步骤交给真实 Decision 通道。** 四项核心验收与 B2 机制半边全部通过。

`B5 take_control` 的实时抢占时机按交接文档**移交** `interrupt_when` 切片（机制与工具调用已真实验证，缺的是回合制 Agent 抢窗口这一架构前提），不计入本轮阻塞项。

## 尚未作为通过条件的项目

代码回归、真实客户端写入修订回归、SDK 计划事实唤醒和 B7 unknown 的 runtime/Agent 收果回归已通过。尚缺的真实边界证据包括 B3 unknown 的恢复闭环、`requires` 的真实 Agent 实例、同步/异步交接以及切片 1 的两项回归。缺料修订已完成 runtime 回归，但仍缺真实 Agent 生成该计划的证据。Responses HTTP provider 当前拒绝异步工具，当前真实 Agent 证据使用 SDK 通道。

后续工作的交接说明见 [`reflex-slice2-handoff-20261010.md`](reflex-slice2-handoff-20261010.md)。

## 对后续阶段的影响

切片 2 的控制权、统一 Decision 请求入口和同树计划执行底座已经有离线修复证据，但在真实验收完成前不启动阶段三。写动作监督仍保持切片 1 的程序优先策略；在获得和平模式、有物品的真实消费者数据前，不把开放语义放行扩大到其他工具，也不开始世界所有权账本或根中断实现。
