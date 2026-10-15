# 第 7 步：最小空闲生活（2026-10-14 当前结果）

已读核心理念、题目约定第 14–17 节、常在线执行计划第 7–10 节、已有生活与威胁报告，以及当前选项编译、动作绑定、BodyLoop 和 SDK 回合代码。

产品提交 `133a4b5`、`d454341`，分支 `codex/reflex-always-on`。第 7 步**未通过真实验收**；用户授权先推进下一步、之后集中处理问题。因此已收齐证据并做第 8 步测试和报告，没有把未过项改记为通过。

## 改动与测试

- `reflex_question.py`：空闲和生活运行帧共享生活选项；加入世界时间、玩家距离；继续当前动作排第一；Agent 思考时保留看向、走近、待着、保命项；无档案不主动社交。
- `direct_call_supervision.py`：生活运行帧不提供 next_step；生活选项绑定已有工具和参数。
- `body_capability_tools.py`、`phase1_runtime.py`：模型不可见的 `__reflex_look_at` 复用现有视线事务，没有增加公开 Agent 工具。
- `body_loop.py`：生活工具通过现有写通道和停止通道运行；动作前关闭 Baritone 破坏/放置，之后恢复原值。`d454341` 修正异步设置：accepted 不是 applied，等待已有终态后才移动，恢复也等待终态。沿用工具 timeout_s。
- `runner.py`：SDK 模型回合期间标记 agent_thinking，finally 清除。

定向最终：**113 passed, 114 subtests passed in 6.71s**。日志 `logs/step7-targeted-tests-v2.log`，副本 `evidence-step7-step8-20261014/targeted-tests.log`。

| 旧测试文件 | 调整 | 原因 |
|---|---|---|
| test_life_profile.py | 原选项断言加入 look_player；新增 7 个事实、过滤、运行帧、设置恢复/终态测试 | 对照第 7 步范围和真实设置通道，不强制模型答案 |
| test_body_loop.py | 假 registry 改成 RegisteredTool 和假 Body | 在已准入工具上包装地形设置，旧 SimpleNamespace 没有工具契约 |
| test_agent_body_capability_registry.py | 两个公开集合断言排除内部 attention adapter，同时断言它不在 model_names | 只加入内部入口，公开工具集合不变 |

全量最终不是零失败，见第 8 步报告；没有修改 keepalive 断言隐藏失败。

## 离线：真实原始帧，经线上编译

源 `logs/reflex-always-on-20261014-step7-current-e/trace.jsonl`。这是隔离默认服务器配置后、确实没有初始任务的 SDK 会话。白天连续帧 seq6–313；玩家帧 seq582。调用线上 `idle_question` / `idle_options`，没有读现成 state 句子造事实。

第一题请求体与记录相等，按编译器键顺序请求真实 Decision，stay 与记录一致。随后连续问 60 次，上一题选择决定下一题当前动作。

| 试验 | 选择分布 | 结论 |
|---|---|---|
| 原请求基线 | stay 1 | 复现 |
| 连续 60 帧，白天无人 | stay 60；切换 0 | 通过不超过 6 次切换要求 |
| 同帧白天，12 种顺序 | stay 12 | 没选回家 |
| 同帧黄昏，12 种顺序 | go_home 12 | 通过 |
| 同帧夜晚，12 种顺序 | go_home 12 | 通过 |
| 玩家 2 格，12 种顺序 | look_player 12 | 如实报分布，距离标准未自行裁定 |
| 玩家 6 格，12 种顺序 | look_player 12 | 同上 |
| 玩家 20 格，12 种顺序 | stay 10、look_player 2 | 受顺序影响，不写 12/12 |

共 **133 请求，0 unknown**。天色/距离组只改一个记录事实，是对照，不是这些场景各自的原生帧。排列从全部 permutations 以 Random(7) 打乱后取 12，无重复标签。v1 取前 12 排列得到玩家20格 stay12；最终采用 v2 的10/2，两份原始输出都保留。

脚本 `bench/step7-life-replay.py`；61 个原始 compiler_input 在 `bench/step7-life-native-e-inputs.json`。完整题目/响应 `logs/step7-life-current-e-replay-v2.jsonl`；汇总副本 `evidence-step7-step8-20261014/life-replay-summary.json`。d454341 只修设置终态，没有改变这些题面。

## 夹具归因更正

| 运行 | 结论 |
|---|---|
| step7-current，复用 JVM 参数 | 没带主类/classpath，客户端退出；无生活验收 |
| step7-current-b / c | 默认 natural 档案注入房屋任务，seq6 user_message/start 是证据；不能计作无任务生活 |
| step7-current-d | 私有配置误放仓库内，启动器拒绝；未开始验收 |
| step7-current-e | 仓库外独立 XDG 配置、空 servers.toml，有效短帧采集 |
| step7-current-f | 同样隔离配置；最新 d454341，有效测试，真实死亡后判失败 |

b 的死亡存档重新进服证据仍成立；但有默认房屋任务，不能同时证明无任务生活。房屋任务是**夹具配置错误，不是死亡恢复主动决定盖房子**。未接管住宅世界。无效目录都保留。

## 真实运行 f

目录 `logs/reflex-always-on-20261014-step7-current-f`；SDK/openai_chat，DeepSeek v4.1 Flash，端口 25569 / 25579 / 8771。未动 25567 / 25577 / 8768。

**旧测试世界、旧背包**：world=step3-current-6db65ec-20261014，Step3CurrentBot；开局 health≈11.9、food16、oak_log24、oak_planks10。不是新世界空背包。档案 home=[151.5,70,163.5]，白天附近待着、玩家来看看、天黑回家。

控制台生成/移动 Visitor、设黄昏、移除 Visitor、之后设白天，是人工夹具；自然怪物保持启用。死亡后提前结束，trace **1178.575 秒（19分39秒）**，不是30分钟。launcher exit0，自己的 Agent/客户端关闭；服务器经其 RCON stop 保存退出。世界/trace保留。

| 验收项 | 证据 | 结论 |
|---|---|---|
| 玩家来时看过去 | look_player请求222，completed181、preempted41；native yaw改变，allow_break/allow_place=false | 真实看向发生；频繁切换未满足无抽动 |
| 走近玩家 | seq2496请求，seq2518 arrived | 真实完成1次 |
| 黄昏回住处 | running_frame选go_home109次未绑定；idle seq4643请求，seq4740 arrived，位置≈[152.7,71,162.3] | 真回家1次；运行帧丢动作是缺陷 |
| Agent中途任务 | seq2514用户消息；craft_item seq2720；work seq2722→idle seq2740；原木24→23，木板10→14；Agent seq2862确认 | 真实Agent任务完成并回空闲 |
| 生活动作立即让出 | 首次走近在消息前约0.08秒已完成；第二个实际回家窗口helper记unavailable | 未证明抢断，while_life_move标记不算证据 |
| 30分钟没有死亡 | 服务端 `[08:16:36] Step3CurrentBot was slain by Spider`；dead seq5289、death seq5300 | 未通过，非控制台kill |
| 方块零损坏 | 生活段无world_edit；Agent移动有seq5567/5599，两块oak_leaves→air | 整场不是零破坏；未有玩家放块来源证据 |
| 不超过5分钟零题 | idle状态最大37.689秒；跨dead为41.880秒 | 未有5分钟零题，但明显停问，不能称持续每秒一题 |

为了造新的生活抢断窗口，seq5285 发了普通用户走路任务，不是直接注入Body。之后角色死亡/重生，Agent在树冠处多次寻路失败；seq8948明确报告**没到达**(185,67,134)，该任务未完成。它同时称没破坏，与seq5567/5599实际树叶破坏矛盾；报告采用Body事实。

## 死亡与交接

- 死亡位置≈[156.2,70,162.3]、health0，native采样1791936997926ms。
- native重生采样1791937001547ms，间隔**3.621秒**；重生位置[169.5,82,161.5]、health20。
- dead seq5289（672.913s）→reflex_death seq5300（708.461s）：**35.548秒**。
- body_event_wake：death seq5320（722.599s）、respawned seq5333（725.491s），共2次；reflex_fact_wake=0。另有recovery_reconcile runtime_intent_wake2次，单列；不把agent_start=2当投递数。
- Agent seq5459（739.186s）：“刚复活过来（上一秒我确实死了——血量从11.9直接归零，现在在出生点重生，背包物品全掉了……）”。确实知道死过。

`body_loop.py:102` 设置dead后await `cancel_active_execution_with_facts("death")`，之后才emit/投递；这是通知串在结算后的代码位置。没有证明哪个内部await花了35秒，不归因于Decision延迟。native重生与Python/Agent延迟分开报告。

## Decision参与度

979请求/979回答；idle906、work73。用途idle_frame670、running_frame282、result_frame27；运行帧中生活236、Agent工作46。中位**142.608ms**，unknown4/979（0.41%）。

选择：stay525、look_player222、keep_going90、go_home110、approach_player1、next_step2、repeat20、call_agent5、unknown4。**选择不等于动作**：默认请求/结算look_player222、approach_player1、go_home1；109次运行帧go_home没有请求。不同事情为看向、走近、回家三类；没有保命动作实际发起。

会话每分钟：idle题34.109、运行题14.356、结果题1.375，总49.840。按可观测native工作区间内统计：运行题66.174/工作分钟。33个带elapsed_ms的native动作并集**45.335秒**；余下**1133.240秒**只是估计，缺时长/遗漏终态会少算工作，dead/启动也在余量里，不称精确空闲。

delegate_work=0、read_reflex=0；read_body_processes=1用于寻路失败诊断。逐分钟与各次投递见 `step8-metrics.json`。

## 集中待修，第8步不偷改

1. **出题和执行答案使用两份事实。** `body_loop.py:322` 用_frame合并life_profile/ambient；`:362` 消费答案重取原始_snapshot；`:406` 用它绑定动作。`direct_call_supervision.py:485` go_home缺life_profile返回None。须统一事实流，不为go_home加特判。
2. **死亡前后停问/通知延迟。** 37.689/41.880秒与await位置留证，具体耗时源未查明。
3. **生活抽动与终态竞态。** look_player/stay频繁切换，41个视线调用preempted；不加冷却、租约、概率门掩盖。
4. **Agent移动权限。** “只走路”没有阻止两次自然树叶破坏，不能用自述覆盖实情。
5. **真实抢断和完整30分钟生活**没验成；需要新的真实证据。

用户可见启动方法（当前没有重新启动）：

```text
在WSL MineBot-always-on，用仓库外独立XDG_CONFIG_HOME（空servers.toml），沿用launcher与life profile。
保留 --env-file ~/.config/minebot/provider-deepseek-v4.1-flash.env、专用25569/8771与新run-dir，不带初始目标。
用已有Python运行 minebot.shell.app panel <该run-dir> 打开同一会话；不能将Panel当可执行文件。
```

未合并、推送；没有宣布第7步或整批通过。以下早期短运行是历史，不能替代上面的验收。

# 早期短运行记录（保留）

## 改了什么

提交 `da9aab9`。新增 `minebot/app/life_profile.py`，从 `MINEBOT_LIFE_PROFILE` 读取运行目录中的 JSON 档案；空闲事实带生活意图。`idle_options` 只使用通用玩家类别、位置和档案住处，提供靠近玩家、跟随玩家、回住处、待着和保命项；没有档案时保持待着和保命项。空闲动作复用已有 `move_to` / `follow_entity` 工具和同一个 BodyLoop。

## 定向测试

`pytest -q tests/unit/test_life_profile.py tests/unit/test_body_loop_step6.py tests/unit/test_body_loop.py`：**41 passed**，`logs/step7-directed.log`。

## 真实短验收

运行目录：`logs/reflex-always-on-20261014a-step4/step7-live/trace.jsonl`。使用专用 25569 / 25579 / 8771，25567 / 25577 / 8768 未使用。档案为 `logs/step7-profile.json`，没有玩家实体，所以生成的线上空闲题包含生活意图、`go_home` 和 `stay`；第一帧的请求在 seq 89，状态实际写出：

`This is the idle-life intention: “Stay near home and notice nearby players. quiet and cautious Approach and follow Visitor when nearby.”`

运行约 30 秒，23 个 idle frame，未执行写世界动作，身体存活。该运行只证明档案被读入和题面选项编译，未证明玩家出现后的靠近/跟随物理验收，也未证明黄昏回住处。

客户端、Agent 和 launcher 已停止；服务器保留。未合并、未推送。

## 未验证

- 附近玩家出现时的靠近、跟随和切换次数。
- 天色变化后回住处。
- 30 分钟生活验收、无玩家方块损坏。
- 全量 pytest 留到第 8 步。
