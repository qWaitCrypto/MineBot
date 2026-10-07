# Responses Agent 内核（2026-10-07）

## 用户行为与目标

用户已授权替换普通 Agents SDK Runner 的模型—工具循环，采用基础 OpenAI SDK 的 Responses 原生异步工具与 WebSocket steering。机器人执行长动作时可以聊天、接收追加要求；真实动作结果自动进入后续模型请求；明确取消真正停止身体。每次模型请求使用最新目标、任务状态、动作结果和身体状态。

理念来自 [Issue #1](https://github.com/qWaitCrypto/MineBot/issues/1) 的完整 P01–P40。本计划不把已有契约、治理或进度机制当作必须保留的产品理念。

## 范围与接口

- 复用 ToolRegistry、真实客户端 Body、工具结果投影和物理取消。
- 长动作独立于模型协程存活；按原始 call_id 保留 pending 调用，终态以真实 function_call_output 返回。模型轮次结束和普通聊天不取消身体。
- 同一身体只有一个写动作；读取和取消不能排在长写动作后面。
- 活跃响应期间使用 response.steer 接收用户消息；模型空闲时以普通新输入续接。accepted、pending、steered 和自动后继响应遵循原生协议，避免重复消息。
- 工具结果在合法续接点提交，引用最新 response_id；每次 response.create 重新提供工具定义和最新 instructions。
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
- [ ] 固定一个完整协议可用的端点组合：gpt-6.1-sol / OpenAI SDK 2.36.0 的 HTTP 原生异步工具已实测通过；WebSocket steering 仍因账户筛选失败而未验证。
- [x] Responses 内核与定向接口验证。
- [x] 生产入口、消息 steering、明确取消、短计划和最新上下文接通；会话回归覆盖 SQLite 消息队列与同步执行线程。
- [ ] 真实模型 + Minecraft 连续任务和物理取消验收。

## 启用与验证

旧私有配置保持可用，默认仍为 `sdk`。新路径通过私有环境显式选择，不在 API 报错后自动切换：

```dotenv
MINEBOT_AGENT_KERNEL=responses
MINEBOT_LLM_KIND=openai_responses
MINEBOT_LLM_MODEL=gpt-6.1-sol
```

沿用现有端点和密钥配置；需要指定网络代理时使用 `MINEBOT_LLM_PROXY`。基础 SDK 的原生 WebSocket wire 方法承载新协议，不升级整套 Agents SDK。模型实际返回的调用 `async` 标志决定是否允许在该结果未返回时继续响应，不能把工具声明支持异步当作调用已经异步。

```bash
python -m pytest -q
python tools/probe_responses_protocol.py --env-file /private/provider.env --model gpt-6.1-sol
python -m tests.e2e_real_client_agent_collect --disposable-local-server --continuity --env-file /private/provider.env --model gpt-6.1-sol
```

协议实验只验证真实 API，不启动 Minecraft。它依次验证原生异步调用、未完成调用期间的新输入、原始 call_id 的迟到结果和 steering 自动后继响应。没有成功就返回具体阶段与错误。原生接口定义见 [异步工具](https://developers.openai.com/api/docs/guides/async-tool-calling)、[steering](https://developers.openai.com/api/docs/guides/steering)。

新内核用 `update_plan` 和 `finish_task` 承载短计划与模型判断；计划 revision、记录 ID 由运行时填写。模型完成记录明确标注 `completion_authority=model`，不套用旧物资目标解析器，也不标成服务端已独立验证。真实任务实验仍用库存、交接与位置观测确认结果。

## 当前结果（2026-10-07）

默认行为回归通过 76 项，耗时 10.05 秒。7 项新内核行为回归覆盖迟到结果、重复事件、自动 steering 后继、同步调用所需结果、写动作互斥、独立读取、普通聊天、同步执行工作线程取消、短计划写入与模型完成记录；还检查只读超时不中断写动作，以及动作超时清理后仍能执行后续动作。明确取消先等待身体和异步工具结果收尾，再重置会话。这里的模型和 Body 为协议/会话替身，不是 Minecraft 实验。本次取消/超时修正对应的旧绑定定向回归 4 项 / 1.54 秒通过，没有运行历史全量。

现有私有端点 `2.1885566.xyz` 对 gpt-6-astra 和 gpt-6.1-sol 的 HTTP Responses 请求成功，但链式 HTTP 请求返回 400：`previous_response_id is not available for this user`。经显式网络代理，gpt-6.1-sol 的 WebSocket 握手可达，首次请求在任何响应事件之前被关闭：`1013 (try again later) no available account`。固定 SDK 的可复现实验停在 `async_dispatch`，返回 success=false。

现有真实采集脚本已增加 `--continuity` 模式，复用生产交互入口验证采集交接、途中聊天、追加返回和独立取消。模式先执行真实原生协议请求；本机 WSL 尝试在 WebSocket 连接阶段失败（ConnectionResetError），报告明确记录 Minecraft 任务未启动。Windows 的同模型协议复核仍为上游 `1013: no available account`。这些是失败记录，后续世界行为尚未执行。

补充实测：同一端点的 gpt-6.1-sol 使用 HTTP Responses、完整 input 历史、store=false 和 reasoning.encrypted_content，避开 previous_response_id，三次请求均返回 200。首轮调用实际带 async=true；暂不提交工具结果，发送普通聊天后收到 ACK；随后用原始 call_id 提交 count=3，收到 COUNT=3。报告：logs/responses-http-async-20261007.json。工具结果为实验数据，尚未接入 Minecraft。

原生异步工具本身不要求 WebSocket；HTTP 可以在工具仍运行时发起下一轮模型请求。原生 response.steer 用于模型响应进行中注入要求，当前官方协议要求 WebSocket。HTTP 下等待当前模型响应结束再消费新消息，与原生 mid-turn steering 是不同的行为，不能混称。此前将整条原生异步路径判为不可用的结论已被以上实测修正。

Sub2API 源码核对固定为 0.2.14 / 3f1a2ea0a760730e3bc528105c00b4ee4f23e469（未确认现有 provider 部署版本）：默认 ctx_pool 与 http_bridge 入口只接受 response.create，会拒绝 response.steer；passthrough 会透传其他事件，具备尝试原生 steering 的通路，但尚无本端点的完整实测。1013/no available account 在网关账户选择失败时生成，需要管理员检查全局 WebSocket 配置、账户模式、分组/模型资格及账户可用性，不能仅凭该报错认定模型不支持。源码见 [入口](https://github.com/Wei-Shaw/sub2api/blob/3f1a2ea0a760730e3bc528105c00b4ee4f23e469/backend/internal/service/openai_ws_forwarder_ingress.go#L131-L264)、[透传](https://github.com/Wei-Shaw/sub2api/blob/3f1a2ea0a760730e3bc528105c00b4ee4f23e469/backend/internal/service/openai_ws_v2_passthrough_adapter.go#L1150-L1162)、[账户选择](https://github.com/Wei-Shaw/sub2api/blob/3f1a2ea0a760730e3bc528105c00b4ee4f23e469/backend/internal/handler/openai_gateway_handler.go#L2700-L2729)。

HTTP 原生异步的真实 API 验证已通过；原生 WebSocket steering 和 Minecraft 连续任务仍未完成，goal 保持未完成。后续可以明确采用 HTTP 原生异步推进身体/聊天的连续运行验收，同时继续核实 provider 的 passthrough 配置；若保持原生 mid-turn steering 为验收项，仍需该 WebSocket 通路实际成功。当前生产 Responses 内核仍使用 WebSocket，没有在接口失败后自动改成 HTTP。
