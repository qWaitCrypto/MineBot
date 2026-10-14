# 集中修复：回答执行接线（2026-10-14）

继续 `codex/reflex-always-on`，未合并、未推送。读了核心理念、常在线执行计划，以及上一轮第7/8步证据。本次不改题面、不加语义规则。

| 项 | 状态 |
|---|---|
| 运行答案执行丢掉住处/食物/实体事实 | 已修；195项定向通过；真实复验待做 |
| 工具结束后迟到的运行答案 | 已修；终态由唯一结果题接管；定向通过 |
| 死亡交接延迟 | 增加停止各阶段计时；原因尚未定位，未宣称解决 |
| 109个原始回家帧 | 109/109题面逐字不变；原快照0/109能绑定，出题帧109/109能绑定 |
| 全量 | 上一源码2483通过、1失败、1跳过；本修复尚未全量 |
| 真实运行 | 下一次专用夹具复验；旧世界旧背包，不冒充全新开局 |

## 改动和测试

`body_loop.py` 保存每个在途问题的完整事实帧，消费答案时用该帧绑定工具和参数。最新原始快照只用于检查是否已经终态。reader读回的新事实也进入保存帧。worker结束取消悬挂运行题，不等Decision服务。

`runner.py` 仅增加trace中的停止阶段计时：request、interrupt、writer_idle、owner_idle、native_probe。工具结果及停止路径不变。

新增 `test_body_loop_answer_dispatch.py`：回家、看向、吃东西、远离威胁四种答案保留出题时的事实/目标；终态不执行迟到停止；worker结束取消在途题。不修改旧断言。

定向命令覆盖agent_runner_spine、answer_dispatch、body_loop、terminal_frames、step3–6、direct_call_supervision、life_profile。真实输出：`195 passed in 12.82s`。日志 `logs/answer-dispatch-targeted-v4.log`。此前v3因一次编辑缩进错误收集失败，已修正，旧日志保留。

`logs/replay-step7-answer-bindings.py` 重建上轮109帧并经线上编译器比较，`questions_equal=109`。输出 `logs/answer-bindings-replay.json`。未问新模型、未执行身体，不算真实验收。

## 延迟证据的边界

旧服务器日志在死亡前有29.870秒tick停顿（`logs/step7-current-b-server.log:415`），之后还有3.354秒停顿。测试宿主/服务器超载确实发生过，但不足以确定Python停止结算各阶段的35秒耗时。新增计时用于复验，当前不归因于Decision模型。

本次未运行真实模型，因此新增Decision请求和Agent唤醒均为0。真实复验另报用途请求率、实际动作和唤醒原因；不以109个可绑定答案代替109次身体执行。
