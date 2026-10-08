# Responses Agent 内核（2026-10-07）

## 2026-10-08：不限时双层小屋实验已启动

用户已明确启动 [TESTING.md](TESTING.md) 的空库存建房任务：外轮廓 10×10、
双层实际可用、内部设施合理；材料与物品数量自主决定，无总时限。
沿用生产 Responses HTTP 内核和 `gpt-6.1-sol`，交互会话 `max_steps=None`。
独立新世界种子 `20261008`，生存、和平难度、正常昼夜和随机刻；
出生点 `(-6.5,66,6.5)`，服务端确认库存为空、经验为零，客户端生命和饱食均满。
出生附近有天然橡树和露石，没有赠送物品、预建场地或矿脉坐标。
旧实验世界及旧 Baritone 地形缓存均保留，客户端为本轮重新建立缓存。

当前只确认启动，尚无建成结果。记录在 `logs/house-20261008-145412/`：
`goal.txt`、开局服务端/Body 状态、`agent.log`、`trace.jsonl` 和 `run.json`。
桌面 `MineBot - house LIVE` 已实际核对有真实客户端画面。
第一次启动命令错误解析了虚拟环境解释器的符号链接，报缺少 `nbtlib`；
当时没有 Agent/Body 调用，错误保存在 `startup-interpreter-error.log`。
纠正启动命令后生产会话已就绪；模型已制定初步计划，并成功读取状态、
库存、近处方块与实体。开跑后不补物资、
不修改执行代码或拼接重跑结果；最终仍按实物验收。


## 2026-10-08：客户端动作朝向已实现

行走/探索使用 Baritone 路径朝向；可见镜头在原生移动与发包朝向处理后
按最短角度转动，跨 ±180 不绕远路。挖掘、放置和近战/弓弩的精确目标
绕过行走平滑。地面动作临时应用朝向设置，完成、失败、取消或断线恢复；
Elytra 保留原生设置。近处跟随看已选对象，路径和视线绑定同一实体；
菜单看实际方块；显式看向和空闲视线保持。实现见 [ATTENTION.md](ATTENTION.md)。

直接调用 Body 的朝向场景 25.491 秒通过：±180 行走、绕墙转弯、采集高处
原木、指定对象移动后的跟随/注视、箱子转移、显式看向与移动中取消。
54 个有效移动样本均处于行进方向 45° 内；每个采样动作结束后四个原
朝向设置恢复。这个样本支持当前可见行走效果，不代表所有地形都只向前走。
另跑现有近战/弓/弩、菜单合成/转移/熔炼和真实双客户端交接，物理结果通过。

交接首次失败：两颗钻石丢出后被发送者重新捡回，接收者库存为空。
身体在完成投掷后增加原生 GoalRunAway 的短距离让位，离开重叠拾取区，
然后看回接收者。相同工具复验，接收者实际得到两颗钻石；满背包时返回
receiver_pickup_unconfirmed，未报成功。原失败 `handoff_tools.json` 保留，
修复后是 `handoff-retry.json`，不覆盖失败报告。另一次跨堆叠交接
80 颗钻石，2.281 秒完成：服务端接收者 80、发送者 16，记录在
`handoff-80.json`。

证据根目录：`logs/attention-20261008-141829/`；`result.json` 为朝向场景，
`combat_tools.json`、`menus.json`、`handoff-retry.json` 为对应物理回归。
Java 编译、实际 Mixin 加载和 72 项客户端接口/协议回归通过。
没有重跑无关全量或模型长任务。本轮未重飞 Elytra。

启动前，[TESTING.md](TESTING.md) 的任务已按用户要求改为：空库存开始，
建成外轮廓 10×10 的双层小屋，内部设施合理、两层实际可用。无物品数量
配额，无总时限，资源和施工由 Agent 自行安排；限定和平难度与自然开局，
无赠送物品。实际启动状态见本文件上节。


## 2026-10-08：连续采集与大资源体量

`collect_resource` 在 disposable 真实客户端上复用原生 Baritone 数量采矿，
不再逐块搜索、接近、挖掘和拾取。小树先读取相连树干/枝干，用上游
Builder 清理选中的原木，再拾取；下一棵在本棵完成后才开始。收尾是偏好，
不是“连通就必须清空”：只探查本次所需数量加少量收尾余量，余量随剩余
时间缩小；超出时使用原生 MineProcess 按库存目标执行，不报树体过大。
没有遍历整片森林或整座山。未授权 disposable mutation 的路径沿用原有事务。

同一生产 `collect_resource` 入口的定向实验 87.589 秒通过，无模型参与：
两棵各 8 根的小树先后完成、实际净增 16（请求净增 9），第三棵未动；
72 格石体净增 40 圆石后停止且仍留石头；12 铁矿实际入包 12 粗铁；
80 根、2×2×20 的雨林木场景仅净增所需 8 根，上端仍保留；采集取消至
终态 103 毫秒，最终 pathing/any_process_active 均为 false。场地明确加载，
提供铁斧、铁镐和垫脚泥土；库存、目标方块由服务端独立核对。这验证了
上述场景，不将其写成自然森林/所有巨树或完整长任务均已通过。

本轮失败保留：早期测试台未实际完整建成，原木掉到台下；第一次还暴露
“库存达到最低值掩盖本棵拾取失败”，已改为采集终态也必须成功。随后在
正确加载的石体上，masked Builder 挖掉约 27 块后原地挥镐、保持 active，
没有继续移动；操作员停止该失败。石头/矿石改走现有上游 MineProcess，
修后同场景 29.373 秒净增 40 且真实移动。没有声称修复上游 Builder 的
所有清除行为，也没有改 Agent 规划或加入另一个无进展 Gate。

最终报告：`logs/collection-native-demand-20261008-135519/result.json`；
失败现场：`logs/collection-continuity-loaded-20261008-134756/`，更早场景
失败分别在 `collection-continuity-20261008-133839/` 和
`collection-continuity-retry-20261008-134504/`。相关行为回归 182 项、
14 个 subtests 通过，Java 编译及实际加载通过；未跑无关全量。
朝向见 [ATTENTION.md](ATTENTION.md)，仅设计，尚未应用。


## 2026-10-08 可见长任务：三项均未完整通过

用户授权后，使用生产 Responses HTTP 内核、gpt-6.1-sol/xhigh 和同一真实
Fabric/Baritone 客户端，实际执行三项任务。实时观察窗口显示该客户端的
画面；任务成功由服务端库存、方块和位置核对，不以模型接受命令为完成。

| 任务 | 上限／实际运行 | 实际结果 | 未完成项或具体故障 |
|---|---|---|---|
| 自然森林资源生产 | 30 分钟／约 30 分钟 | 32 原木、164 圆石、铁镐、32 火把、工作台、1 熔炉、16 粗铁 | 未炼成 16 铁锭，未补齐追加的 4 熔炉，离起点约 48 格 |
| 供料营地建造 | 45 分钟／约 45 分钟 | 大部分木屋和设施、两个水源、24/32 株小麦 | 房屋 8 处不符目标，余下 8 株未种；建造超时、站位失败和支撑块清理拒绝均保留 |
| 三点跨区运输 | 60 分钟／约 11 分钟提前失败 | 到达煤炭点，取走 128 煤炭，立好第一根四格路标 | 第二段导航发生 Body IPC 超时，Agent 退出码 8；基地箱仍为空 |

资源任务遇到升级工具缺木后，自行回地面采木再继续；营地任务自行拆分
施工、离开占据的屋顶缺口后补板、调整水坑附近的耕作站位。普通聊天与
追加要求实际送达，未取消原工作；短暂模型连接错误后也出现了自动恢复。
这些局部行为不等于完整长任务成功。

营地场景遗漏了旧实验留下的 `random_tick_speed=0`，运行中恢复为 3，
属于有环境干预的失败实验。脚本以后明确设置正常随机刻；湿润耕地按
`moisture>0` 判断，修正了首次验证器额外要求最大值 7 的问题。原失败
报告不改写，未种满和房屋缺失独立足以判定失败。施工支撑被旧合法性
检查以 `break_denied:player_structure_risk` 拒绝，未扩建世界保护工程。

运输首次准备场景时，新区块生成导致服务器落后约 21 秒、客户端超时
掉线，尚未启动 Agent；该失败单独保留。预生成目的地附近区块并重连后
才进行上表的实际运输。运输中还暴露了请求 300 秒却被外层工具的
120 秒执行预算截断的问题；本轮未更改该预算。

运输退出的直接原因是 Body IPC，不是模型服务拒绝。相同起点直接调用
`near:2600:94:2240:720`、60 秒期限，状态读取再次超过 10 秒。线程快照
定位到 `MineBotClientBody.state → estimatedTicksToGoal → GoalNear.heuristic`
占据客户端主线程。仅删除状态采样中的非必要耗时估算，没有改导航算法。

重新编译并加载后，同一导航约 15 秒到达，状态读取 24–40 毫秒。该次
取消发在完成之后，取消分项未通过，原报告保留。另一次在实际移动中
取消，状态读取 5 毫秒、取消至终态 144 毫秒，客户端报告进程已停止，
随后一秒位置变化约 0.05 格。此为直接 Body 复验；没有重跑完整运输，
运输后的模型取消用例也未执行，不能据此把原长任务改成通过。

证据根目录：`logs/visible-agent-20261008-111455/`。`resources/`、`camp/`
保存原任务结果；`logistics/` 保存场景启动失败；`transport-retry/logistics/`
保存实际运输、线程快照、修复前后直接复现和移动中取消结果。
`summary.json` 汇总，`post-run-body-state.json` 确认最终客户端空闲。
本轮只运行这些真实任务、定向复现、Java 编译和脚本检查，未运行无关全量。

## 2026-10-08 第二轮：小幅上下文优化与异步复验通过

用户授权小幅优化上下文，并继续复测原生异步。本轮复习 Issue #1 P01–P40；只改善已有任务上下文和 HTTP 请求，不新增长期记忆平台、可执行计划或 30 分钟模型运行。

上下文沿用现有目标、短计划、每轮 Body 状态和工具观测归档。原生 input 已包含聊天，因此 instructions 不再重复最近八条消息；短计划提示保留玩家追加要求、坐标、具体障碍和改策略理由。明确区分新读取的身体状态与历史观察。

HTTP input 超过已有 64,000 字符软阈值时，仅缩减最近 12 个结果之前、已有可查询完整归档的成功大观察：保留真实 success/reason、traceRef、observationHandle 和原始 call_id，详细 summary 按需通过观测工具读取。玩家原文、reasoning、调用、失败、近期结果不裁剪；原始历史不修改。没有归档或可缩减内容时允许超过软阈值，不为凑预算破坏调用/结果关系。此处不是完整上下文窗口压缩，原生 compaction 尚未通过当前端点实测，未启用。

真实 API 验证：gpt-6.1-sol 使用生产 provider 配置的 xhigh 和生产 HTTP 连接，载入合成的归档扫描历史；请求视图从 113,033 字符缩减至 3,073 字符，模型仍准确返回玩家指定的 (10,70,20) 与最新失败 target_unreachable。这里只验证上下文处理和 API 续接，不是 Minecraft 行动或长任务成绩。

原生异步已重新完整跑通两次：均通过生产 ResponsesKernel 调用真实 API，实际运行一个等待聊天结束才返回计数的异步 Python 工具；实际调用为 async=true，工具未完成时回复 ACK，随后在原始 call_id 上提交结果并回复 COUNT=3，工具各只执行一次。一次 low/512/parallel_tool_calls=false 耗时 9.413 秒；另一次使用现有生产配置 xhigh、不手工关闭并行调用，耗时 11.318 秒。没有修改生产模型、增加模拟异步或自动降级。

本轮较早的 HTTP 400、连接失败和超时仍保留。请求差异对照出现有成功、拒绝和无原生异步标记的结果，不能据此归因于单一 strict/tool_choice/parallel 设置。Astra 对照出现返回模型标签与原生错误中的 Luna 名称不同，用户已说明存在降级映射并指示不继续该项；后续只验证 gpt-6.1-sol。两次成功证明当前链路可用，不抹除端点失败。

新增两个上下文边界回归；直接相关集合 90 项 / 19.06 秒通过，覆盖原文、失败、调用与结果保留、归档查回引用及原始历史不改。没有运行无关全量。报告：`logs/context-async-review-20261008/sol-production.json`、`sol-configured.json`、`context-configured.json`；同目录保留失败与请求差异对照。Body 未修改，长任务尚未测试。

## 2026-10-08：Body 连接和原生异步复查

按 Issue #1 P01–P40 检查现有运行链。本次只补 Agent 入口和异步续接的具体缺口；上下文管理留待调研讨论，不启动 30 分钟模型运行。

原有 44 个基础工具继续复用 ToolRegistry。真实客户端增加 `mine_resource`、`build_structure`、`clear_area`、`farm_area`、`elytra_travel`、`read_body_processes`、`build_schematic` 七个薄入口，不暴露任意命令。批量变更复用客户端已有 disposable-world 标志；Java 后端不注册这些客户端入口。文件蓝图限定本次验证过的 Sponge v2、零 Offset；使用 nbtlib 读取，并在施工结束后通过 Body 批量核对每个实心/空气格。区域建造和清除也核对目标格。

工具分派按声明的 JSON Schema 校验参数；错误字段、类型、数量和缺参在执行前返回具体 `invalid_tool_input`，模型可修正。`finish_task` 等到动作结果实际提交给模型后才可关闭任务。HTTP 流中断保留完整调用项、原始 call_id、正在执行的工具和未提交用户消息；恢复不重放物理动作，也不重复添加已提交的工具结果。

生产分派实验使用 `--through-agent`，经过与 Responses 相同的调用/投影入口，`model_calls=0`；它证明连接，不冒充模型任务。批量采矿净增 24 根原木；72 格区域建造/清除；80 株农事；Sponge 房屋 100 格核对；250 格飞行均有真实结果。飞行首次进入飞行后摔死、最终超时；相同参数直接调用和再次 Agent 分派均成功，首次摔死原因未定位，失败报告保留。

2026-10-08 原生协议复验 **未通过**：gpt-6.1-sol / SDK 2.36.0 / 当前私有端点 / HTTP SSE，先收到 async=true 调用、待完成期间收到 ACK，但迟到结果请求返回 HTTP 400：`Async tools are not supported with gpt-6.1-sol.`。此前 2026-10-07 的成功记录仍作为历史事实；不能用它覆盖本次拒绝，也不自动换模型、换内核或启动 provider 平台。直连与代理请求的连接失败同样保留。报告在 `logs/agent-body-review-20261008/protocol*.json`。

相关接口回归 180 项和 116 个 subtests 通过；包含流中断后原始结果续接、用户消息保留、错误参数不执行和完成前等待结果。真实实验见 `logs/body-complex-20261007/` 的 2026-10-08 记录，可复现：

```bash
python -m tests.e2e_real_client_body_complex --disposable-local-server --case <任务> --through-agent
```

## 用户行为与目标

用户已授权替换普通 Agents SDK Runner 的模型—工具循环，采用基础 OpenAI SDK 的 Responses 原生异步工具；WebSocket steering 是次要能力。机器人执行长动作时可以聊天、接收追加要求；真实动作结果自动进入后续模型请求；明确取消真正停止身体。每次模型请求使用最新目标、任务状态、动作结果和身体状态。

理念来自 [Issue #1](https://github.com/qWaitCrypto/MineBot/issues/1) 的完整 P01–P40。本计划不把已有契约、治理或进度机制当作必须保留的产品理念。

2026-10-07 用户进一步明确：原生异步工具是主线，原生 response.steer 是次要能力。随后用户明确不继续 WebSocket 工作：当前目标只要求已实测的 HTTP 原生异步路径，WebSocket 代码保留，未经真实验证，不再作为当前待办。

## 范围与接口

- 复用 ToolRegistry、真实客户端 Body、工具结果投影和物理取消。
- 长动作独立于模型协程存活；按原始 call_id 保留 pending 调用，终态以真实 function_call_output 返回。模型轮次结束和普通聊天不取消身体。
- 同一身体只有一个写动作；读取和取消不能排在长写动作后面。
- HTTP 路径在当前模型响应结束后提交排队的新消息，身体工具独立运行。WebSocket 路径在活跃响应期间使用 response.steer；accepted、pending、steered 和自动后继响应遵循原生协议，避免重复消息。
- 工具结果在合法续接点按原始 call_id 提交。HTTP 使用完整 input/output 历史、store=false 与 reasoning.encrypted_content；WebSocket 引用最新 response_id。每次请求重新提供工具定义和最新 instructions。
- 只固定一个真实可用的模型、SDK、端点组合。先核对现有端点；不自动退回同步 Runner 并声称异步已完成。
- 不移植 Pi，不建立 provider 平台、Gate、世界保护工程或长期记忆平台；不重写整个 Brain/Body。

## 实施切片与提交

1. 核实模型与协议组合，记录实际支持或具体拒绝；调整最小依赖。
2. 实现薄 Responses 循环：完整调用分派、pending 结果、单写约束、继续响应与实时上下文；接口定向回归。
3. 接通生产入口、聊天 steering、追加要求和明确取消；移除这条路径对旧模型轮次/进度终止的依赖。
4. 运行下面的真实实验，记录实际结果和具体失败原因，更新运行命令与状态。

每个有意义的切片单独提交。只跑对应接口回归与真实实验；不跑无关全量，不用源码形状测试锁实现。

## 验收与停止条件

在现有 disposable 本地世界与固定客户端组合运行真实模型任务：“采集原木并交给玩家”。采集期间发送普通聊天和追加的“交接后回到出发点”；观察原动作继续、追加要求进入后续计划、真实库存/交接/位置确认结果。另开一次长动作，明确取消后独立读取身体确实空闲，旧动作停止。

必要定向回归覆盖原始 call_id 的迟到结果、重复事件、聊天不取消动作、写动作互斥与取消。真实实验失败则写明任务、初始条件、实际结果及具体原因；接口回归不能替代真实任务成功。

达到这些行为后停止扩张。若模型端点不支持原生协议，保留已实现的切片并明确缺少的配置，不将兼容模拟包装成原生支持。

## 当前进度（用户移除 WebSocket 后，本次目标已完成）

- [x] 目标已创建，当前执行计划已切换。
- [x] 固定 gpt-6.1-sol / OpenAI SDK 2.36.0 / 现有端点的 HTTP 原生异步组合，普通请求和原生 SSE 均已实测。
- WebSocket steering：用户已明确移出当前工作范围；保留已有代码，不继续端点验证。
- [x] Responses 内核与定向接口验证。
- [x] 生产入口、消息 steering、明确取消、短计划和最新上下文接通；会话回归覆盖 SQLite 消息队列与同步执行线程。
- [x] 真实模型 + Minecraft 连续任务和物理取消验收，83.779 秒通过。

## 启用与验证

配置为 `openai_responses` 时默认使用已通过真实任务的新内核；`openai_chat` 和 `litellm` 继续使用 SDK 路径。可显式指定 `MINEBOT_AGENT_KERNEL=sdk` 使用旧循环，不在 API 报错后自动切换。

已验证的配置是：

```dotenv
MINEBOT_AGENT_KERNEL=responses
MINEBOT_LLM_KIND=openai_responses
MINEBOT_LLM_MODEL=gpt-6.1-sol
MINEBOT_RESPONSES_TRANSPORT=http
```

沿用现有端点和密钥配置；需要指定网络代理时使用 `MINEBOT_LLM_PROXY`。新内核默认选 HTTP/SSE，可显式设置 MINEBOT_RESPONSES_TRANSPORT=websocket；使用基础 SDK 的两种原生接口，不升级整套 Agents SDK，也不在失败后自动切换传输。模型实际返回的调用 `async` 标志决定是否允许在该结果未返回时继续响应，不能把工具声明支持异步当作调用已经异步。

```bash
python -m pytest -q
python tools/probe_responses_protocol.py --env-file /private/provider.env --model gpt-6.1-sol
python -m tests.e2e_real_client_agent_collect --disposable-local-server --continuity --env-file /private/provider.env --model gpt-6.1-sol
```

协议实验只验证真实 API，不启动 Minecraft。默认 HTTP 验证原生异步调用、未完成调用期间的新输入和原始 call_id 的迟到结果；--transport websocket 另验 steering 自动后继响应。没有成功就返回具体阶段与错误。原生接口定义见 [异步工具](https://developers.openai.com/api/docs/guides/async-tool-calling)、[steering](https://developers.openai.com/api/docs/guides/steering)。

新内核用 `update_plan` 和 `finish_task` 承载短计划与模型判断；计划 revision、记录 ID 由运行时填写。模型完成记录明确标注 `completion_authority=model`，不套用旧物资目标解析器，也不标成服务端已独立验证。真实任务实验仍用库存、交接与位置观测确认结果。

## 当前结果（2026-10-07）

原生异步主线已通过真实验收：gpt-6.1-sol / OpenAI SDK 2.36.0 / HTTP Responses SSE / 当前私有端点。原生异步工具不要求 WebSocket；模型调用实际带 async=true，工具未完成时可以继续聊天，再按原始 call_id 接回迟到结果。[官方异步工具文档](https://developers.openai.com/api/docs/guides/async-tool-calling)。

真实模型通过生产交互入口完成以下任务，耗时 83.779 秒：

- 空库存出发，采集石墙后的三根橡木原木；采集期间发送普通聊天和“交接后返回出发点”。模型实际回复了两条消息，原采集调用继续运行，结果按原始 call_id 续接。
- 交接初次距离不足，模型自行靠近并重试；服务器独立确认 Receiver 库存为三根原木。
- 完成交接后返回出发点附近（33.710, 150, 28.255），距指定点约 1.24 格。
- 单独开始行走，观察到实际移动后明确取消。身体终态已收尾，客户端 pathing=false、any_process_active=false；服务器一秒间隔的两次位置均为（35.079, 150, 28.488）。

结果、用户消息、原生异步调用/结果确认、取消回执和服务器观测在 [result.json](logs/agent-collect-1791368744299662501/result.json)；对应 trace.jsonl 和 agent.log 位于同目录。实验仅使用已选择的 disposable 本地世界；Agent 不持有 RCON。

对应接口回归 70 项 / 15.75 秒通过；配置默认值与旧路径选择回归 15 项、3 个 subtests / 3.01 秒通过；真实客户端 Java 编译及加载通过。未运行历史全量，也未增加 Gate 或另一套测试平台。移除了会阻塞 HTTP 聊天的多余 wait_for_tasks 工具，结果就绪时由内核自动续接。请求打开事件流前的连接错误交给基础 SDK 最多重试两次；收到事件后的物理工具不会被 SDK 重放。

失败实验保留在 logs/：交互入口误依赖 Java event_head；blockAt 缺少 interaction_ready；没有专用采集后端时 lambda 返回 None；即时挖掘只有客户端预测；拾取没有匹配 UUID 和距离。这些实际阻断已分别修正，挖掘复用上游 Baritone 的单方块 clear 过程，未扩建自研导航。一次客户端启动 SIGSEGV 和一次 HTTP 连接失败也记录为失败；最终实验通过并不覆盖或删除它们。

## WebSocket steering：已移出当前工作范围

代码保留原生 response.steer 路径及定向协议回归，尚未通过当前 provider 的真实 steering 验收。此前同一模型握手可达，但首次 response.create 在任何响应事件前被关闭：1013 / no available account。HTTP previous_response_id 也被该端点拒绝，当前 HTTP 路径因此使用完整历史、store=false 与 reasoning.encrypted_content。

Sub2API 源码核对固定为 0.2.14 / 3f1a2ea0a760730e3bc528105c00b4ee4f23e469，未确认 provider 部署版本。ctx_pool 与 http_bridge 只接受 response.create；passthrough 能透传其他事件，但本端点尚未实测。需要 provider 的可用 WebSocket 账户与相应配置才能继续这一项。[入口](https://github.com/Wei-Shaw/sub2api/blob/3f1a2ea0a760730e3bc528105c00b4ee4f23e469/backend/internal/service/openai_ws_forwarder_ingress.go#L131-L264)、[透传](https://github.com/Wei-Shaw/sub2api/blob/3f1a2ea0a760730e3bc528105c00b4ee4f23e469/backend/internal/service/openai_ws_v2_passthrough_adapter.go#L1150-L1162)、[账户选择](https://github.com/Wei-Shaw/sub2api/blob/3f1a2ea0a760730e3bc528105c00b4ee4f23e469/backend/internal/handler/openai_gateway_handler.go#L2700-L2729)。

HTTP 在当前模型响应结束后处理排队消息；WebSocket 原生 steering 可以在模型响应进行中注入要求，两者不混称。[官方 steering 文档](https://developers.openai.com/api/docs/guides/steering)。用户已明确不继续这项工作。以下源码与失败记录保留为历史证据，不触发后续开发或验证。


## 上下文管理调研（2026-10-08，待讨论）

这一节记录首次调研的分析和候选方案；后来用户授权的小幅优化与验证见本文开头，没有新增可执行计划。检索窗口为 2026-07-08 至 2026-10-08；有明确发布日期的研究与当前官方能力分开描述，不把旧功能写成最近三个月的新发布。

现状：旧 SDK 会话已有 12 轮 / 96 items / 64,000 字符窗口；Responses HTTP 的 `_HttpResponsesConnection.history` 仍完整累积并在每轮重发。`AgentContext` / `ContextCompiler` 已能组织目标、任务、身体状态、近期结果，但这不限制原始 Responses 历史。现有 TaskWorkspace 和完整工具观测归档可以复用，无需另建存储平台。

近期值得借鉴的研究：

| 日期与来源 | 具体进展 | 对 MineBot 的判断 |
| --- | --- | --- |
| 2026-07-11，[Compaction as Epistemic Failure](https://arxiv.org/abs/2607.13071) | 记录压缩将超时/终止过程的部分输出写成已确认结果的案例 | 摘要不能把 pending、失败或客户端预测改写成成功；动作事实保留真实终态与观测引用。该研究是案例分析，不证明所有压缩都会这样。 |
| 2026-08-24，[The Compaction Cliff](https://arxiv.org/html/2608.22752v1) | 分类型保留约束、程序、事实、偏好和事件，研究多轮均匀压缩丢失约束的问题 | 玩家明确要求与项目适用约束原样注入；旧工具输出允许缩短。借鉴保留策略，不引入论文的分类器/验证平台。 |
| 2026-09-29，[PAIR](https://arxiv.org/html/2609.36526v1) | 从同一个环境状态比较压缩前后的后续执行，定位有害压缩并修改对应摘要段落 | 用真实续行动作判断压缩是否有效，失败时检查丢了哪条事实。论文实验在 AppWorld、OfficeBench 和零售任务，不是 Minecraft；不搬入离线训练/反复 rollout 管线。 |
| 2026-09-30，[ReCAP](https://arxiv.org/html/2609.40118v1) | 根据当前请求、过去 attention 重要度与消息依赖，挑选历史及其支持证据 | 借鉴按当前任务找回相关事实、调用与结果一起保留。完整方法需要 attention；闭源 API 要额外代理模型估算，当前不划算。论文性能不代表本项目性能。 |

当前成熟接口也可用作候选，但以下文档没有足够发布日期证据，不能当作近三个月发布：

- [OpenAI Responses compaction](https://developers.openai.com/api/docs/guides/compaction)：支持 `context_management` 阈值触发的服务端压缩和独立 `/responses/compact`。支持 `store=false` 的 input-array 续接，不依赖 WebSocket 或 `previous_response_id`；压缩状态是不可读的加密 item。自动压缩后可按文档删去最新压缩项之前的历史；独立 compact 返回的窗口则必须整体保留，不能套用前一种裁剪方式。当前 provider 的 compaction 尚未实测，不能宣称支持；异步 pending 与压缩的组合也需验证。
- [Pi compaction 文档](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/compaction.md)：结构化目标/约束/进度/决策/下一步摘要，加最近完整消息，调用和结果不得被裁开。这个模式可以借鉴，不需要为它更换内核。

建议首先做任务工作上下文，而不是先做跨会话长期记忆：

| 内容 | 来源与更新方式 |
| --- | --- |
| 玩家当前目标、追加要求、适用约束 | 持续保留有效原文；取消、完成或用户修改后更新，不让摘要自行改写要求。 |
| 当前阶段、缺少材料、障碍、改变策略的理由 | 复用现有任务/短计划；由模型根据实际行动更新，运行时不写游戏策略。 |
| 位置、库存、健康、目标是否仍存在 | Body 新读取的状态；旧观测写明时间，不把旧库存当作现在的库存。 |
| 最近完整行动、失败、未完成原始 call_id | 保留调用与结果关系；pending 和未交付结果不剪掉，不因压缩重执行身体动作。 |
| 更早的动作细节、地点、资源和蓝图证据 | 完整记录复用现有归档，工作上下文只保留任务相关结论和可查回引用。 |

历史压缩优先考虑已验证 provider 下的原生 compaction；明确任务状态仍单独注入。若该接口实际不可用，采用小型结构化滚动摘要加完整近期行动窗口，不扩成兼容平台。最初只在已完成行动的边界压缩，避免先引入 pending 压缩竞态；这是实施建议，不是官方对异步组合的保证。

例子：采木交接后玩家追加“回出发点”。上下文需要保留追加要求、出发点坐标、原采集调用、交接结果及距离不足后靠近的理由；当前库存重新读取，不反复携带完整方块扫描。验证看压缩后是否继续正确行动、是否遗忘要求或重复动作，以及实际输入量/延迟；不以摘要长度或模块数量验收。
