# 第 2 步：终态题回到生命周期消费者

读了活动执行计划全文、第 1/2/3 步及总报告；本轮沿用已读的核心理念和每帧约定。分支 `codex/reflex-always-on`，未合并、未推送。

| 项目 | 状态与证据 |
|---|---|
| 第 1 步空背包到石镐 | 已有真实证据；`reflex-always-on-20261014-step1-live` seq 4165。未重新运行。 |
| 运行帧消费者 | 已做；已有 work/protection/switch 定向测试保持。 |
| 直接、计划、保命终态题消费者 | 本轮统一；三个来源由同一 lifecycle 消费，最多一个请求在途。 |
| unknown 后刷新事实 | 已保留；变化位置、零额外动作、只记录真实尝试的旧测试保持。 |
| 保命结果判断超时 | 原调用 `timeout_s`；在途请求取消，结果槽释放。没有新增上限。 |
| 死亡时正在等终态题 | 本轮定向证明及时处理 death，取消旧请求，只通知一次。 |
| 118 个运行帧基线 | 新旧等价测试通过；不是原始事实重放，旧记录没有完整快照和时钟。 |
| 16 个结果帧基线 | 线上调用链比较保持通过。 |
| 新补丁真实验收 | 尚未执行。既有运行不能作为本补丁真实证据。 |

## 实现与测试

上一提交 `754790b` 只把部分保命结果题移回 monitor，仍留下外层兼容重问；还会丢失保命历史上下文。这次用既有结果槽加一个终态请求队列替换它。CallSupervisor 仍负责实际工具执行、停止结算；BodyLoop 的一个 lifecycle task 串行调度运行、空闲和三类终态请求。队列只是请求传递，不是第二个身体持有者或状态锁。

`_after_default_action` 只编译既有事实、提交问题、消费答案。每次 unknown 下一帧刷新事实再提交；原调用自己的超时取消 Future 和在途 Decision 请求，不再从外层补问。保命结束后保留现有恢复/重试路径和最近五次尝试；题目文字没有改。

第一次新测试发现 unknown 间隔中插入 idle 题的竞态，已用原有 `_result_pending` 槽覆盖整个判断事务。没有用选项特判解决。

命令：`python -m pytest -q tests/unit/test_body_loop_terminal_frames.py tests/unit/test_body_loop.py tests/unit/test_body_loop_step3.py tests/unit/test_body_loop_step4.py tests/unit/test_body_loop_step5.py tests/unit/test_body_loop_step6.py tests/unit/test_direct_call_supervision.py tests/unit/test_reflex_question.py tests/unit/test_reflex_tools.py tests/unit/test_work_runtime.py tests/unit/test_body_loop_step2_baseline.py tests/unit/test_reflex_running_intent_frames.py tests/unit/test_reflex_trace_replay.py tests/unit/test_work_language_runtime.py`。

结果：**302 passed in 19.08s**。日志 `/tmp/reflex-terminal-final.log`；compileall、diff --check 通过。旧测试仅加强 lifecycle 调度断言；新增串行、原超时取消、death 三项测试。测试使用假 Decision，证明代码调度，不是语义验收。上次全量 `2474 passed, 1 skipped, 291 subtests passed` 属于 `7e8d020`，不冒充本次代码全量结果。

## 空闲真实 Decision 回放

原脚本仅换输出路径，未改输入、题目或标准。源 `reflex-stage-20261012i/trace.jsonl`；线上 RealClientBody → 读通道 → BodyLoop → 出题函数，真实服务，29 帧、348 次，保留每一请求及回答。

| 帧 | 回答分布 |
|---|---|
| 87、2286 | 各 stay 12/12 |
| 2197 | stay 12/12；记录是氧气 122→211，缺少 head_in_liquid，不能伪造为缺氧下降帧。用户此前已允许此帧待着继续推进。 |
| 2288–2484，共 23 帧 | 各 surface 12/12，合计 276/276 |
| 2287（不评分） | surface 12/12 |
| 2485、2486（不评分） | 各 move_away 12/12；恢复氧气、近期受伤、远处敌对实体同时存在，照实报分布。 |

脚本保留原评分给出 312/348，明确缺氧原口径 276/288（包括 2197 这一恢复帧）；unknown 0，中位延迟 176.64 ms。按用户接受 2197 待着的补充指示，26 个有评分帧为 312/312；这不是 348/348，三个过渡帧不算通过或失败。平静帧只重复同一种顺序，其余按原脚本十二种题目/危险项顺序，不夸称每种保护选项排列都覆盖。

完整请求/回答：`logs/step2-idle-direction-current.jsonl`；逐帧状态：同名 `.summary.json`；控制台 `/tmp/step2-idle-direction-current.log`。本轮没有每分钟身体工作或新 Agent 唤醒指标，因为没有启动身体或 Agent。
