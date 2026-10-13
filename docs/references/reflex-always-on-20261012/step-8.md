# 第 8 步：全量测试和阶段报告

## 读了什么

- `docs/exec-plans/active/reflex-always-on-20261012.md`
- `docs/design-docs/core-idea.md`
- `docs/design-docs/reflex-frame-question-20261012.md`
- 第 1–7 步报告、现有 `metrics.py` 和已保留的真实 trace。

## 测试

命令：

`/home/qwait/MineBot/.venv/bin/python -m pytest tests/unit -q`

结果：**2474 passed, 1 skipped, 291 subtests passed in 1417.87s (23:37)**。
失败：0。日志：`/tmp/always-on-unit-suite.log`。

另外运行仓库默认收集的 `pytest -q` 得到 127 passed、16 subtests；它没有收集 `tests/unit`，不作为本阶段全量数字。

## 真实 trace 指标

由 `docs/references/reflex-always-on-20261012/metrics.py` 从原始 trace 计算，没有重新启动客户端：

| 运行 | 请求/分钟 | Decision 中位数 | unknown | Agent 唤醒 | read_reflex | 最长 idle 间隔 |
|---|---:|---:|---:|---:|---:|---:|
| 第 2 步 `logs/reflex-always-on-20261013-step2-running-intent-b/trace.jsonl` | idle 28.037；running 29.669；result 1.728；default-result 0.192；plan-result 0.096 | 131.8 ms | 0.16% | 4 | 0 | 38.19 s |
| 第 5 步 `logs/reflex-always-on-20261014a-step4/step5-live4/trace.jsonl` | idle 30.479；running 19.969；plan-result 2.102；default-result 0.526 | 132.5 ms | 1.98% | 1 | 0 | 36.55 s |
| 第 7 步 `logs/reflex-always-on-20261014a-step4/step7-live/trace.jsonl` | idle 43.740 | 135.3 ms | 0% | 0 | 0 | 2.36 s |

第 5 步反应链 2 次，总时延 0.540 s、1.338 s。以上运行均未触碰住宅实验端口 25567 / 25577 / 8768。

## 当前结论

- 单元测试全量通过。
- 真实运行的 trace 和指标已保留。
- 第 2 步的完整真实验收仍有未通过项：运行帧/结果帧逐字基线、保护动作完整交接、玩家和黄昏生活验收等证据不完整；不能把全量测试通过写成第 2 步或整批真实验收通过。
- 第 6 步的 `interrupt_when` 真实触发、备用装备真实接管，以及第 7 步的玩家/黄昏/30 分钟生活验收仍按各步报告列为未验证或未通过。
- 本步没有改源码、没有启动真实客户端、没有合并或推送。
