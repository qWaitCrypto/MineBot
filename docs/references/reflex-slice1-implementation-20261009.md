# MineBot 切片 1 实施报告：直接长调用的受阻发现与真实交还

日期：2026-10-09
分支：`codex/reflex-slice1`
Worktree：`/home/qwait/MineBot-worktrees/reflex-slice1`
隔离数据：`logs/reflex-slice1-20261009/`

## 交付行为

本切片把直接长施工接成了完整的控制闭环：动作有真实进度时继续；程序发现明确终态或明确材料耗尽时，在总超时前撤销准入、请求 Body 停止、等待 native 终态，再把已经核验的成果、剩余部分和阻断事实交回 Agent。Agent 可以调整计划并再次调用同一个施工工具，原生 builder 会跳过已经匹配的方块。用户停止走独立控制通道，不等待 Decision 或 Agent。

监督是程序优先。Java/Body 的目标格变化、有效寻路进度和终态是确定事实；只有限定读取仍无法解释时才向 H2O 发一个 `continue/inspect/yield` 单题请求。Decision 超时、不可用或答案非法都保留为 `unknown`，不会自行制造 `blocked`。这次没有把开放语义判断或自动补料塞进 Body。

## 代码接缝

- `minebot/app/direct_call_supervision.py`：直接 `build_structure` 的进度监督、预算、有界读取、H2O 请求和交还。
- `minebot/app/construction_facts.py`、`client_body_tools.py`：限定施工区域的匹配/剩余/未知核验，保留完整度和实际材料来源。
- `minebot/game/real_client_body.py`、`real_client_ipc.py`、Java `MineBotClientBody.java`：轻量进度回调、主手与库存完整度、`construction_progress`、真实停止确认和未加载区块的 UNKNOWN 表示。
- `minebot/app/runner.py`、`responses_kernel.py`、`contract/execution.py`、`contract/messages.py`：事实通知、同步/异步续接、用户停止入口和控制未知状态。
- `minebot/brain/registry.py`：在 post-state 或 preemption 替换前保存原 `ToolResult`，迟到终态只归档，不重发旧建议。
- `tests/unit/test_direct_call_supervision.py` 及相关既有单元测试：覆盖原结果保留、回调/终态、控制未知、迟到回执和同步/异步事实通知。

工作区里已有的 `BODY_EXECUTION_STATE.md` 修改属于其他会话，本切片没有纳入提交、没有回退。

## 复现与窄修

先在独立 flat 世界测试历史调用的形状。当前客户端已包含施工期间开启、结束后恢复 `allowInventory` 的既有修改：材料放在主库存、空手启动的 100 格施工在 27.445 s 完成；主手拿其他物品的 9 格施工在 6.509 s 完成。历史“背包有料但空手停滞”没有在这版客户端复现，因此没有为它再添加自动补料或自动装备。

改用真实缺料作为受阻正例：改动前仅有 8 块材料的 100 格施工在放置 8 格后一直等到 18 s 总超时，返回 `deadline_exceeded`。已有设置恢复在成功、缺料超时、用户取消及最终普通失败中均核对；复现原始记录为 `logs/reflex-slice1-20261009/reproduction-before.json`，每个 fixture 和正在运行的采样都保留。

随后确认状态表示需要两个窄修：主手必须区分“已观测为空手”和“未读到”，库存必须给出 `inventory_complete` 与 `inventory_includes_selected`。未完整读取的库存不再伪造成空字典。对远处未加载区块，第一次尝试 `hasChunkAt` 仍错误地把读取判为可用；最终改成与环境查询一致的 `getChunk(..., FULL, false) != null`，未加载格明确进入 UNKNOWN。证据保留在：

- `final/unloaded-cells-before.json`
- `final/unloaded-cells-haschunk-failed.json`
- `final/unloaded-cells-after.json`
- `final/final-loaded-smoke-trace.jsonl`

最终混合读取保留已知圆石并标出远格 UNKNOWN；已加载区域仍核验为 100/100。

## 隔离验收结果

最终数据以 `logs/reflex-slice1-20261009/final/acceptance-summary.json` 为准，早期日志没有覆盖它。

| 验收 | 结果 |
| --- | --- |
| 正常施工、不误停 | 27.974 s，100/100，4 组“位置几乎不动但目标格增加”的进行中采样，未误停，结构核验通过 |
| 材料不足、程序交还 | 7.076 s 发现，7.645 s 结算；完成 8 格；原总超时 65 s；native 停止 106 ms，Baritone 3 ms |
| 用户停止 | 4.870 s 结束；停止后没有新的身体动作；native 136 ms，Baritone 36 ms |
| Decision 不可用 | 1 次 `unknown` 请求，约 2.033 s；不误停，仍由原 18 s 超时兜底 |
| 真实 Decision 不明 | 2 次 `inspect`，输入 928/898 tokens，`output_tokens=0`；模型请求约 251/183 ms；预算后保持控制并由原 18 s 超时结束，不能写成模型诊断了阻塞 |
| 原生普通失败 | `invalid block` 的真实 `process_start_failed`，0.800 s，设置恢复 |
| 迟到回执 | 明确注入 adapter 延迟 2 s；用户停止仍生效，6.513 s 结算，14 格；旧结果归档且不触发新动作 |

同步和异步都完成了真实 Agent 闭环：

- 同步首调用实际 `async=false`，7.343 s 发现，61.153 s 完成；Agent 取出 92 块并续建到 100/100。
- 异步首调用实际 `async=true`，7.098 s 发现，69.995 s 完成；Agent 使用 `take_control`，取出 92 块并续建到 100/100。
- 首次施工差量为 0→8，续建为保留 8、新增 92。最终背包为空，箱中余 4 块（初始 8 + 箱中 96 − 余 4 = 100），没有重复重建或重复耗料。
- Agent 重新提交了同一 cuboid，由原生 builder 跳过已匹配格，并未生成逐格 92 格蓝图；同步为 6 次模型响应/5 次工具调用，异步为 7 次模型响应/6 次工具调用。
- 两种模式均保持原 call 的单次最终回执；迟到结果只进入归档。

两轮 Agent 使用已授权 API 网关、`gpt-6.1-sol`、`medium`；工具列表限定本切片验收需要的调用，没有修改生产的全能力词汇。箱子和初始库存只在运行前布置，运行中没有开发者补料、装备或继续提示。核心缺料闭环的 H2O 请求数为 0。

Agent 运行的原始记录在 `final/agent-sync/`、`final/agent-async/`；监督回归请求在 `final/semantic-regression.jsonl`，其中 `gold=null`，只表示真实消费者请求样本，不冒充独立语义金标评测。

## 验证与来源

- Fabric/Java 构建：`./gradlew :client:body:build --offline --console=plain` 成功。
- 相关 Python 测试：178 passed，9 subtests passed；JUnit 结果为 `final/unit-release.xml`。
- 合并原 `codex/real-client-body-spike` 的后续运行时修复后重新执行相关测试：180 passed，16 subtests passed；结果为 `final/unit-after-merge.xml`。合并内容包括 relay 流恢复、实际 async 判定、执行事实唤醒和显式 continuation 的准入恢复。
- 正常、缺料、停止、迟到、Decision 不可用/不明、普通失败的 trace 和 JSON 全部保留在 `logs/reflex-slice1-20261009/final/`。
- 构建和加载 provenance、隔离端口、类与 jar 哈希在 `logs/reflex-slice1-20261009/isolation.json`。
- 本轮隔离服务器 25568、RCON 25578、IPC 8769、Xvfb :91 已在保存世界后关闭；原住宅 25567/25577/8768 和公共服务未触碰。
- 清理与源码 provenance：`final/cleanup.json`、`final/source-provenance.json`。原始日志与世界保留在 worktree 本地，没有混入源码提交。

## 限制与后续影响

施工读区上限为 4096 格，Java 约每 20 tick 提供一次有界进度；监督预算和复核间隔是保护性上限，不代表毫秒级读取整个世界。`attributable_placed_cells` 在无法由原生工具证明时保持 UNKNOWN，世界差量没有冒充工具放置计数。

本切片没有实现五原语解释器、嵌套计划、`repeat`/`interrupt_when`，也没有证明 Decision 能可靠执行开放自然语言条件；真实核心闭环中的材料阻断由程序确定事实完成。切片 2 可以复用本切片的直接调用控制、事实通知、结果保留和交还接缝，但语言规格、Decision 上下文及开放语义仍需独立离线验证后再进入生产；不应借本次施工验收扩展自动技能或全工具诊断。
