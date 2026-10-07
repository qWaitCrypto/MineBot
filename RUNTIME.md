# Responses Agent 内核（2026-10-07）

## 用户行为与目标

用户已授权替换普通 Agents SDK Runner 的模型—工具循环，采用基础 OpenAI SDK 的 Responses 原生异步工具；WebSocket steering 是次要能力。机器人执行长动作时可以聊天、接收追加要求；真实动作结果自动进入后续模型请求；明确取消真正停止身体。每次模型请求使用最新目标、任务状态、动作结果和身体状态。

理念来自 [Issue #1](https://github.com/qWaitCrypto/MineBot/issues/1) 的完整 P01–P40。本计划不把已有契约、治理或进度机制当作必须保留的产品理念。

2026-10-07 用户进一步明确：原生异步工具是主线，原生 response.steer 是次要能力。当前优先使用已实测的 HTTP Responses 完成连续任务；WebSocket 路径保留，实际 steering 验证单独记录。

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

## 当前进度

- [x] 目标已创建，当前执行计划已切换。
- [x] 固定 gpt-6.1-sol / OpenAI SDK 2.36.0 / 现有端点的 HTTP 原生异步组合，普通请求和原生 SSE 均已实测。
- [ ] WebSocket steering 真实验证：现有 provider 仍未提供可用账户，按用户优先级放在异步工具主线之后。
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

## WebSocket steering：待验证的次要能力

代码保留原生 response.steer 路径及定向协议回归，尚未通过当前 provider 的真实 steering 验收。此前同一模型握手可达，但首次 response.create 在任何响应事件前被关闭：1013 / no available account。HTTP previous_response_id 也被该端点拒绝，当前 HTTP 路径因此使用完整历史、store=false 与 reasoning.encrypted_content。

Sub2API 源码核对固定为 0.2.14 / 3f1a2ea0a760730e3bc528105c00b4ee4f23e469，未确认 provider 部署版本。ctx_pool 与 http_bridge 只接受 response.create；passthrough 能透传其他事件，但本端点尚未实测。需要 provider 的可用 WebSocket 账户与相应配置才能继续这一项。[入口](https://github.com/Wei-Shaw/sub2api/blob/3f1a2ea0a760730e3bc528105c00b4ee4f23e469/backend/internal/service/openai_ws_forwarder_ingress.go#L131-L264)、[透传](https://github.com/Wei-Shaw/sub2api/blob/3f1a2ea0a760730e3bc528105c00b4ee4f23e469/backend/internal/service/openai_ws_v2_passthrough_adapter.go#L1150-L1162)、[账户选择](https://github.com/Wei-Shaw/sub2api/blob/3f1a2ea0a760730e3bc528105c00b4ee4f23e469/backend/internal/handler/openai_gateway_handler.go#L2700-L2729)。

HTTP 在当前模型响应结束后处理排队消息；WebSocket 原生 steering 可以在模型响应进行中注入要求，两者不混称。[官方 steering 文档](https://developers.openai.com/api/docs/guides/steering)。按用户最新优先级，这项不阻断已经完成的原生异步主线，不为它扩建 provider 平台。
