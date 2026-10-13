# 第 3 步推进与第 4 步实测（2026-10-14）

已读核心理念、常在线计划第 2–8 步、题目约定第 15–16 节、现有第 3–7 步报告，以及 BodyLoop 的历史、结果调度和安全站位代码。代码版本 6db65ec。本轮没有修改产品源码、没有换分支、没有合并或推送。

用户授权「直接进下一步，之后统一解决小问题」。据此保留第 2–3 步未通过项并继续验证，不把它们改记为通过。

## 当前状态

| 项目 | 结论 |
|---|---|
| 第 2 步统一终态调度 | 302 项定向测试已通过；直接、计划、保命结果都由生命周期消费 |
| 第 2 步空闲离线 | 26 计分帧 312/312；另 3 帧只报分布；2197 的待着已获用户接受 |
| 第 3 步安全站位、求生状态和历史接线 | 现有实现，定向测试通过，真实上浮/恢复链路已发生 |
| 第 3 步三轮内交 Agent | 未通过，真实结果和离线结果均记录在下面 |
| 第 4 步 kill 与溺水后重生 | 已真实发生，Agent 收到恢复事件 |
| 第 4 步死亡存档重新进服 | 启动夹具失败，IPC 未就绪，不能归因于死亡恢复 |
| 第 5 步 | 已复跑已有真实 native 人造帧，未补齐新的真实反击/退回验收 |
| 第 6–7 步 | 工程已有实现，完整真实验收仍未完成 |
| 当前源码全量 tests/unit | exit 0；2477 passed、1 skipped、291 subtests passed |

## 第 3 步：历史离线

旧源 reflex-stage-20261012i 五帧：按原键顺序的 5 次基线均复现 resume。三种现有写法加时间顺序试写，共 252 次变体请求，251 次 resume、1 次 unknown；第五次仍全部 resume。unknown 没有取得合法回答，不算模型选错。这是历史题面比较，旧记录没有完整原始快照，不叫「原始事实重放」。没有将试写加入生产。

新源 reflex-always-on-20261014-step3-current：使用记录的 frame_input.execution、compiler_input.decision_context、节点参数和 Agent 意图，调用线上 BodyLoop._question 和 after_default_options。五个请求体逐字相等，5 次基线答案也全部复现。每帧六种唯一选项顺序，各重复两次；不是十二种唯一顺序。

| 结果帧 seq | 历史次数 | attempts | events | outcomes |
|---|---:|---|---|---|
| 980 | 1 | {"resume": 12} | {"resume": 12} | {"resume": 12} |
| 1053 | 2 | {"resume": 12} | {"resume": 12} | {"resume": 12} |
| 1141 | 3 | {"resume": 12} | {"resume": 12} | {"resume": 12} |
| 1286 | 4 | {"resume": 12} | {"resume": 10, "call_agent": 2} | {"resume": 12} |
| 1506 | 5 | {"resume": 12} | {"resume": 12} | {"resume": 12} |

180 次变体里 resume 178、call_agent 2；两次 call_agent 都在第四次的 events 试写，其余全为 resume。第 3 次起应叫 Agent 的验收未通过，不按次数强制停止，不添加概率门，不把零误报或单测通过当成语义通过。

完整题目和响应：
- logs/step3-history-current-6db65ec.jsonl
- logs/step3-raw-current-replay-6db65ec-v2.jsonl

第一次 raw 回放启动因测试构造缺少 decision_client/trace 失败，未发请求；修正测试构造后用 v2 新文件，旧空文件保留。这是夹具错误，不是产品错误。

## 第 3 步：真实运行

新世界 step3-current-6db65ec-20261014、空背包、Step3CurrentBot；端口 25569 / 25579 / 8771。真实 DeepSeek v4.1 Flash、openai_chat、SDK 内核；不是脚本代 Agent 调工具。住宅端口未动。

人工灌水记录：fixture-events.jsonl。第一次灌水用的是 seq710 的帧，但随后 seq713 已选 next_step，目标是 8 原木而背包仍空，属于正常调用的提前误停。夹具随后才注水，不能把它说成这个原调用运行中成功抢断的证据。后续 Agent 的新调用在已有水池里真实工作，被反射层多次停止、上浮和恢复，能验证水下工作链路。

seq980/983、1053/1056、1141/1144、1286/1289、1506/1509、1560/1563 的六道保命结果题都选择 resume。100 秒测试窗口内没有选择 call_agent；水池清除。不能宣称三轮以内换计划。

这次任务没有完成石镐和 4 生铁；实验中的强制死亡也打断了任务。不能把此运行称为石镐目标的成功验收。旧第 1 步成功记录独立保留，不替代当前源码证据。

## 第 4 步：真实死亡与恢复

- 第 1 次：death seq3517 → respawn seq3520，处理事件间隔 0.977s，native 采样时钟间隔 0.098s；位置 [153.4987992572808, 70.0, 170.2549500516102]，记录氧气 300。
- 第 2 次：death seq8140 → respawn seq8143，处理事件间隔 0.568s，native 采样时钟间隔 0.207s；位置 [177.69999998807907, 62.19500004768371, 149.50101287895484]，记录氧气 0。

第一次是控制台 kill；第二次是专门的封闭水池，人造不可逃离的溺水场景，只验死亡输送和重生，不是上浮寻路通过的证据。封闭水池已移除。body_event_wake 是恢复事实投递，不能把 death 事件或 agent_start 另计成唤醒。

Agent 的明确证据：seq3686 说「我死了一次（在(153.5,70,170)附近，血量从20瞬间归零），已在上方重生点复活，背包清空。没关系——重来。现在先砍树」。第二次有 death/respawned 投递记录，未把投递次数直接当模型回合数。

重新进服单独保留 logs/reflex-always-on-20261014-step4-restart-current：客户端暂停后才 kill，RCON 验证 Health=0.0，然后断开客户端并保留同一世界/玩家。新启动器在客户端 IPC 前超时（300秒），尚未开始 Agent 会话，没有 trace；该项未验证。dead-save-fixture.json 与 client.log、launcher-console.log 保留。

另一次启动尝试保留 logs/reflex-always-on-20261014-step7-current：试图复用 JVM 参数文件时，夹具错误地把只含附加参数的文件当成完整 Java 启动命令，缺少 main/classpath，客户端直接退出1；没有进入世界或开始生活验收。错误入口已改回原有 Gradle 启动器，尚未重跑。不删除失败目录，不把它归为产品失败。

## 参与度与测试

原运行 743.8s，问题按用途 {"idle_frame": 140, "result_frame": 4, "running_frame": 459, "default_result_frame": 19, "plan_result_frame": 13}；选择 {"stay": 118, "call_agent": 4, "keep_going": 428, "next_step": 15, "surface": 42, "resume": 18, "unknown": 5, "repeat": 4}；Decision 中位延迟 131.8ms。

唤醒投递按通道 {"reflex_fact_wake": 7, "body_event_wake": 4}；另有 runtime_intent_wake 2 次。delegate_work 2，Agent read_reflex 0。各次唤醒及原因保存在 metrics-current.json；不把它们直接等同于大模型回合次数。

可观测 native 动作并集 394.0s，余下时间估计 349.8s；按 Body 工作分钟归一的请求数见同一 JSON。统计使用 native terminal 的 elapsed_ms，以 IPC 收件时刻作为结束，有收件时延误差、遗漏终态可能少算工作；不是精确墙钟空闲测量。旧 metrics 把 death 算 wake、把跨工作段 idle 题间距算空闲的算法不再采用。

本轮新跑定向测试：第3步+终态调度 16 passed；第4/5/6步+生活档案 26 passed，共42。首次命令误填不存在的 test_body_loop_step7.py，0测试运行；已改为现有 test_life_profile.py。产品测试和断言未修改。

全量真实结果：`pytest tests/unit -q`，2477 passed, 1 skipped, 291 subtests passed in 1640.17s (0:27:20)。exit_code=0，日志 logs/final-suite-6db65ec/pytest.log、result.json。测试源码6db65ec，本轮只提交文档和回放/统计脚本，产品源码未再修改。此前7e8d020的2474通过是旧基线，本轮新增的3个终态并发/取消/死亡回归计入2477。

## 下一项与未完成

第5步的真实反击/退回、威胁消失后恢复，仍缺新的物理证据。第6步的 handler 和备用装备真实选择、第7步30分钟生活验收未完成。当前不宣布全计划完成。
