# Responses Agent 内核（2026-10-07）

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
