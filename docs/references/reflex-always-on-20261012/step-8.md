# 第8步最新状态（2026-10-14）

当前源码648e073；本轮五项集中修复定向232通过，备用装备短场景真实闭环通过，整批仍未验收通过。未合并、未推送。

| 项 | 证据 |
|---|---|
| 最新全量 | 7e4c1e6：2494 passed、1 skipped、291 subtests passed，exit0 |
| 最新源码全量 | 尚未运行；不把旧全量冒作41c28d7结果 |
| 最新真实场景 | idle-equipment-dead-fixed：断剑、装备、击杀、stay；无误停/额外唤醒/死亡 |
| 断线验证 | 原生踢出停止、新客户端重进完成移动；同进程重连仅定向验证 |
| 未完成 | 工作中断线/IPC硬断、中断handler、重复下水、完整生活与互动、其余旧验收缺口 |
| 详情 | equipment-fixes-live-20261014.md、summary.md顶表；完整trace保留 |

---

# 原始第8步报告（历史）

已读核心理念全文、常在线执行计划第 8–10 节、题目约定第 14–17 节，以及当前第 2–7 步报告。用户明确授权先推进、遗留问题集中处理。

产品源码固定在 `d454341591c7a78e45b3eb9418e506a98ed85ff7`。本步只改统计/回放脚本和文档，没有边跑全量边改产品，也没有合并、推送。第 8 步已执行，**零失败的验收未通过；整批未完成**。

## 全量真实结果

命令：`/home/qwait/MineBot/.venv/bin/python -m pytest tests/unit -q`。外层 `timeout 2700`，工作区 MineBot-always-on；启动时清除继承的 MINEBOT_ / OPENAI_ / ANTHROPIC_ / RCON 环境变量。

```text
SUBFAILED(mode='keepalive') tests/unit/test_responses_kernel.py::ResponsesKernelTests::test_http_keepalive_survives_event_gap_but_idle_and_eof_fail
1 failed, 2483 passed, 1 skipped, 290 subtests passed in 1412.58s (0:23:32)
```

exit_code=1。完整日志 `logs/final-suite-d454341/pytest.log`，运行源码 `running.json`，最终 `result.json`。提交内副本放 `evidence-step7-step8-20261014/pytest-*`。

唯一失败是 keepalive 子测试：本地 loopback SSE 每25ms发一个注释心跳，HTTP读超时100ms；这次在收到 response.created 后触发 `httpx.ReadTimeout`，最终变为 `ResponsesTransportError: HTTP Responses stream interrupted (ReadTimeout, last_event=response.created)`。不是 Decision 服务错误，也不是旧断言与反射修改冲突。

在同一源码、未修改代码/断言、游戏和大测试已退出的环境下，单独重跑该测试三次，**3/3通过**；每次为 `1 passed, 3 subtests passed`。日志在 `logs/step8-keepalive-reruns/`，提交内有副本。该测试及 Responses kernel 相对 ab602a2 没有差异。证据表明并非稳定必现；缺少调度与包时序记录，不能证明具体是何处延迟，也不能用单测复跑覆盖全量的1失败。本轮按用户要求留待集中处理。

旧的6db65ec全量是2477通过、1跳过、291子测试通过，1640.17秒；不是当前源码结果。133a4b5那次全量被主动终止以修异步settings，exit=-15；不当作通过。当前定向113通过/114子测试通过，详见step-7。当前相比6db65ec增加7个普通测试；失败使这次普通测试通过数为2483，不能写成2484全过。

## 统计脚本

`metrics.py` 读取运行目录或trace路径。现在同一份算法输出：每分钟按状态/用途计数、会话分钟率、可观测身体工作区间内分钟率、unknown比例、Decision中位耗时、状态时间/切换、每次投递、反应链、死亡与native重生时延、最长idle状态零题间隔。

身体工作用记录中带native action_id/elapsed_ms的终态区间并集，收件时刻作终点。未有时长的视线动作/缺失终态可能少算；余量含启动、dead等，**不是精确身体空闲**。idle状态也可有生活动作。死亡观测、投递、agent_start、runtime_intent_wake分开计数。选择、默认动作请求、native执行也分开。

本步重新用同一脚本统计三份真实记录，完整结果在 `evidence-step7-step8-20261014/metrics-all.json`，各源目录有 `step8-metrics.json`。第2步记录来自6db65ec，5步是已有独立人造威胁运行；不能都说成d454341刚跑出的验收。

交付前另做本地来源校验：将133次离线请求各自保存的原始facts和选项重新交给当前线上idle_question，**133/133请求体逐字相等**。这是编译来源检查，不是新问133次模型、更不是133/133语义全对；玩家20格仍保留10/2。trace、夹具事件、运行元数据的SHA256见delivery-validation.json。

| 源运行 | 时长秒 | 用途请求数 | 会话每分钟（idle/运行/结果类） | 工作区间内运行题/分 | 中位ms | unknown |
|---|---:|---|---|---:|---:|---:|
| step3-current（2/3/4步） | 743.775 | idle140、running459、result4、default_result19、plan_result13 | 11.294 / 37.027 / 2.904 | 57.714 | 131.848 | 5/634，0.79% |
| step5-threat | 59.146 | idle25、running26、result2 | 25.361 / 26.375 / 2.029 | 75.164 | 137.627 | 3/52，5.77% |
| step7-current-f | 1178.575 | idle670、running282、result27 | 34.109 / 14.356 / 1.375 | 66.174 | 142.608 | 4/979，0.41% |

第二、第五步最后有未收回请求，所以回答分母分别634、52，不拿请求数代替回答数。当前统一统计纠正旧第5步报告的80.3ms和若干手工计数；以上脚本结果为准。

| 源 | loop状态请求数 | 可观测工作/余量秒 | 投递次数与原因 | 最长idle状态零题秒 | delegate/read_reflex |
|---|---|---|---:|---|
| step3-current | idle153、work400、survival82 | 394.012 / 349.763 | reflex7：handoff_requested1、cancelled2、completed2、idle_check1、blocked1；death2、respawned2 | 5.413 | 2 / 0 |
| step5-threat | idle25、work17、threat11 | 19.158 / 39.988 | reflex1，handoff_requested | 2.867 | 0 / 0 |
| step7-current-f | idle906、work73 | 45.335 / 1133.240 | death1、respawned1；另runtime recovery_reconcile2，不混计 | 37.689 | 0 / 0 |

第5步4条反应链（event→request / request→answer / answer→first native progress，总秒）：

| seq | 三段秒 | 总秒 |
|---:|---|---:|
| 600 | 1.262 / 0.197 / 0.419 | 1.878 |
| 637 | 1.271 / 0.197 / 0.390 | 1.858 |
| 830 | 0.0002 / 0.198 / 0.342 | 0.540 |
| 1043 | 0.0001 / 0.206 / 0.457 | 0.664 |

样本仅4条，不报分位数。不能从反应链本身推出“最多挨两下”，这项专门实测尚未补齐。

第7步停止时存活、health20，但此前被蜘蛛杀死一次，native3.621秒重生。木板任务完成，后续走路任务失败；没有遗漏死亡。生活看向/走近/回家真实发生，109次运行中go_home选择没执行、频繁look/stay切换、死亡通知延迟等见step-7。

## 约定与收工条件

总状态逐条在summary.md。关键未过项没有被测试通过数替代：第2步正常调用仍有提前停止；第3步第三轮起叫Agent语义未过；第5步真实反击/退回/恢复未补全；第6步handler和备用装备实际选择未过；第7步30分钟生活已因死亡失败；本步全量1失败。

当前源码没有重新加入概率门、次数兜底、租约/冷却、第二状态锁或新停止入口。后续统一处理必须沿现有事实、准入和停止路径，不能靠这些机制把数字凑对。

复核顺序已明确：完整事实用于执行所选动作；停止结算/死亡通知的等待位置；生活动作的终态竞态；Agent走路的地形权限；最后用真实证据重验未过项。没有在本步改这些产品逻辑。
