# 切片 2：身体控制权、统一 Decision 与五原语解释器

状态：复核修复中（2026-10-09）

## 用户行为

- 身体任一时刻只有一个真实控制者，控制权变更必须经过 native 停止确认。
- Agent 可以一次提交 `minebot-work/0.2` 计划；Runtime 执行 `call`、`sequence`、`batch`、`choice`、`wait`，并在条件不明或身体受阻时交还 Agent。
- 计划反馈、真实结果和修订继续使用同一棵树；已核验成果由 Runtime 保留，不能由模型伪造覆盖。
- 所有 Decision 请求经过一个有界、单请求在途的通道；切片 1 施工监督迁移后行为保持不变。
- Agent 回复结束后进入事件等待，聊天、事实、计划终态或控制请求能唤醒，不自动注入“继续”。

## 顺序与停止条件

1. 先实现并测试 BodyControl 与统一 DecisionClient，再迁移切片 1 监督。
2. 实现候选语言的结构校验、引用解析、树反馈和五原语最小解释器。
3. 接入 `delegate_work`、`control_reflex`、`read_reflex`，保持既有直接工具入口兼容。
4. 用 fake Body/Decision 和既有回归覆盖 11 项边界；若隔离世界可用，再做短验收。
5. 相关单元测试、Java 构建和切片 1 回归通过后停止，不扩展生活行为、方块所有权、repeat、interrupt 或其他工具监督。

## 复核后的补充验收

Opus 复核发现原实现的 B1–B8 缺口。当前已补离线窄修和回归；在以下真实证据补齐前，计划保持 active，不进入阶段三：

- 真实 Agent 经过 `execute_tool` 调用三个 reflex 管理工具。
- 用户停止、暂停、取消和修订均有 native stop confirmation；停止后无后续 Body 写动作。
- 外部代次失效后能进入明确 unknown，并由 native probe 恢复。
- 同一 wait/when 在 pending 后继续判断，直到 satisfied 或超时；事件终态能唤醒空闲 Agent。
- 同 id 改变义务不会继承旧成果；真实 BodyState、绑定事实和 unknown_fields 进入 Decision 请求。
- 和平 disposable 世界补齐真实 Agent 计划、写动作正负例、同步/异步交接及切片 1 监督回归。

## 明确不做

- 不实现生活层行为、威胁响应、所有权账本、`allow_break` 改动。
- 不把切片 1 监督推广到所有 Body 工具，不做新导航、平台、Gate、数据库或查询 DSL。
- 不修改住宅实验、共享服务器或 `BODY_EXECUTION_STATE.md` 的其他会话改动。

## 记录

- 原始单元测试和语义请求写入新建的 `logs/reflex-slice2-20261009/`。
- 实施报告写入 `docs/references/reflex-slice2-implementation-20261009.md`。
