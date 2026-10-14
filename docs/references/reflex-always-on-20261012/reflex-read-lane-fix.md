# 集中修复：反射动作期间Agent读取

旧代码的 `_off_writer_lane` 只认Agent计划和后台调用。SDK模式下，`life` / `threat` 持有者占着写通道时，它仍把Agent读取排入同一个写通道。读取必须等反射动作结束；与三层并行的约定冲突。

新增 `tests/unit/test_reflex_read_lane.py` 用真实SerialExecutionLane和一个保持运行的写任务复现：life、threat两个参数在旧代码上均读取超时（2 failed in 2.50s）。不是模型或真实游戏验收证据。

修复仅在 `_off_writer_lane` 中识别现有BodyControl的life/threat持有者，改用现有 `_read_lane`。不改变准入、持有者、停止入口或状态锁。

修复后新测试、agent_runner_spine、step3、step4合跑：`100 passed in 3.79s`。测试保证读取完成时原写任务仍在运行，持有者没有被读取改变。日志 `logs/reflex-read-lane-before.log`、`logs/reflex-read-lane-after.log`。旧测试未改。

当前g真实进程加载9023943，没有热改它；此修复尚未被它加载，不能把g结果写成这处修复的真实证据。没有为此新增Decision请求或Agent唤醒。死亡35秒延迟目前仍未归因于这处问题。
