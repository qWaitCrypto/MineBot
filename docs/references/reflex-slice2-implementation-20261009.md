# 切片 2 实施与验收记录（2026-10-09）

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

### Opus 复核后的窄修（当前提交未重新做真实世界验收）

复核发现原实现不能作为切片 2 交付：管理工具没有走通用执行入口；用户停止没有撤销计划；外部代次失效会留下假忙；`wait` 一次判断后退出；暂停、取消、修订没有等待 native 停止；同 id 修订会冒领成果；计划 Decision 请求拿到空事实。以下窄修已加入当前工作区：

- `execute_tool` 同时接受 `ToolResult` 和已序列化的管理工具映射，三个 reflex 管理工具可走统一入口。
- `ReflexManager` 在外部取消前撤销活动；暂停、取消、接管、修订都经过 Runtime 的 native 停止确认，未确认时进入 `blocked`。
- `ProgressAuthority` 的外部代次失效会通知 `BodyControl`，当前持有者进入 `unknown`，只能由 native 停止确认恢复，并记录最近停止事实。
- waiting 计划保留活动和身体租约，在下一次判断前不会退出；`wait`/`when` 的超时仍由计划截止时间结算。
- 同 id 节点按完整义务签名继承成果；工具、参数或条件变化会重新执行。
- 条件请求把 `BodyState` 编译为完整事实、绑定对象、显式 `unknown_fields` 和共享字段词汇；`about` 不再裁剪事实。
- Responses 空闲时收到计划终态会启动一次受控唤醒轮次；已有运行中的轮次仍只走事实唤醒。

当前离线定向回归为 `256 passed, 33 subtests passed`，仓库默认测试为 `103 passed, 16 subtests passed`。这些结果只证明离线代码路径，尚未替代真实 Agent、真实写动作、用户停止和同步/异步交接的隔离世界验收。

### 离线与单元验证

- 阶段二定向单测：`23 passed in 1.64s`，记录见 [`unit-phase2-rerun.log`](../../logs/reflex-slice2-20261009/unit-phase2-rerun.log)。
- Fake runtime 验收：`7/7`，覆盖 wait satisfied/pending/unknown、修订保留已完成节点、写动作条件门控、runtime choice、BodyControl unknown 恢复。原始结果见 [`fake-runtime-acceptance.json`](../../logs/reflex-slice2-20261009/fake-runtime-acceptance.json)。
- Java 生产代码离线编译成功，记录见 [`java-production-compile.log`](../../logs/reflex-slice2-20261009/java-production-compile.log)。全量 Java 构建曾因 Maven Central 的 JUnit 下载超时，未把该网络问题误报为生产编译失败。

### 真实客户端最小计划

脚本见 [`real-slice2-minimal.py`](../../logs/reflex-slice2-20261009/real-slice2-minimal.py)，原始结果见 [`real-slice2-minimal.json`](../../logs/reflex-slice2-20261009/real-slice2-minimal.json)。在和平模式、客户端已上线后，Runtime 接受并执行如下计划：

`sequence(read_body_processes → move_to)`

结果为 `completed`，活动 id 为 `activity-a566fd4b0ff744438f78ebb537488ef5`。服务器确认 `MineBotSlice2 joined the game`，IPC ready 状态包含 `difficulty: peaceful`、`health: 20.0`，计划终态和树形反馈均已写入记录。真实 trace 至少包含工具原始结果、Body 状态和 `reflex_activity_finished`。

随后用同一客户端跑了一个只读 `wait` 条件，实际请求走 `/v1/systemone`，记录见 [`real-decision-wait.json`](../../logs/reflex-slice2-20261009/real-decision-wait.json)。H2O 返回 `unknown`（输入 168 tokens、输出 0、约 0.19 s），Runtime 保持 `waiting`，没有发起任何 Body 写动作；这确认了统一 Decision 通道和 unknown 不放行的边界。

## 尚未作为通过条件的项目

真实世界中尚未用有物品的写动作正例验证施工监督，也没有把真实 gpt-6.1-sol Agent 接入本次最小计划生成；这两项需要单独准备物料和 Agent/API 预算，不能用空库存或手写计划冒充通过。阶段二的离线写门控和同树修订证据仍以 fake/runtime 单测结果为准。真实 Agent 调用、写动作正负例、停止/聊天并行、同步/异步交接、条件改写和事件唤醒的独立 trace 尚未补齐，因此本报告现在不能把切片 2 标成“完整验收通过”。

## 对后续阶段的影响

切片 2 的控制权、统一 Decision 请求入口和同树计划执行底座已经有离线修复证据，但在真实验收完成前不启动阶段三。写动作监督仍保持切片 1 的程序优先策略；在获得和平模式、有物品的真实消费者数据前，不把开放语义放行扩大到其他工具，也不开始世界所有权账本或根中断实现。
