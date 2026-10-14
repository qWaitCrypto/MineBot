# 全量中keepalive测试的时序修复

上轮全量唯一失败：`test_responses_kernel.py::ResponsesKernelTests::test_http_keepalive_survives_event_gap_but_idle_and_eof_fail` 的keepalive子项。socket读取在100ms内没收到下一块数据。原样独立复跑三次通过；没有证据支持修改生产传输代码，也不能把独立通过当全量通过。

本次只改测试的时间尺度：读取超时100ms→500ms，心跳25ms→50ms，次数10→20。事件间隔1秒仍长于读取超时，心跳间隔仍明显短于读取超时；silent必须超时、EOF必须缺少终态、keepalive必须收齐20个注释并完成。这保留了原测试验证的三个行为，减少它对测试宿主100ms调度的依赖。未改生产超时或Responses内核。

整个文件真实输出：`21 passed, 16 subtests passed in 28.51s`。日志 `logs/keepalive-scheduling-fix.log`。最终全量尚待修复冻结后运行；原全量失败保留。

本改动仅在测试，没有Decision请求、没有Agent唤醒或身体动作。
