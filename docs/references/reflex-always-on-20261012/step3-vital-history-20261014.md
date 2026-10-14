# 打断历史补生命事实的离线试验

已读核心理念、常在线计划和每帧题目约定第15–17节。产品源码 `edc89ff` 未改。用户允许保留未通过项继续推进，故本试验之后补第5步真实威胁证据，不将第3步标为通过。

原始记录：`logs/reflex-always-on-20261014-step3-current/trace.jsonl`。线上编译五个保命结果题，请求体逐字比较；旧答案 **5/5复现 resume**。对应结果seq为980、1053、1141、1286、1506；打断时原始运行帧seq为956、1024、1105、1184、1482。

从记录的原始frame_input、时钟和VitalWindow采样重建打断时生命、氧气、头部液体状态，再补入历史。没有从现成身体句子反推。三个候选只在离线脚本里，未写入生产。每帧六种唯一顺序各问两次，共12问，不能称十二种唯一顺序。

| 结果seq / 已打断次数 | body_at_interruption | chronological | body_then_result |
|---|---|---|---|
| 980 / 1 | resume 12 | resume 12 | resume 12 |
| 1053 / 2 | resume 12 | resume 12 | resume 12 |
| 1141 / 3 | resume 12 | resume 12 | resume 12 |
| 1286 / 4 | resume 12 | resume 12 | resume 12 |
| 1506 / 5 | resume 12 | resume 12 | resume 12 |

**180个历史候选全部resume，未改善。** 第一帧无历史对照为resume10、call_agent2，受顺序影响；它也没有完全通过。候选加对照共192问：resume190、call_agent2，unknown0。连同五个旧基线共197问。

第三轮题已有三次氧气下降和上浮正常结束的记录，背包仍只有leaf_litter1，模型依然选择回去采8根原木。这里只能确认补历史生命事实未解决重复下水，不据此推断服务忽略历史，不按次数强制call_agent，不加概率门。

首次试验因夹具错取compiler_input.facts失败，原文件保留；v2改为经线上_frame恢复事实。不是产品失败。本轮未改产品代码或单元测试，不重复全量；最近全量仍为d050458的2491 passed、1 skipped、291 subtests passed，收尾shell退出码记录缺陷见集中报告。

脚本：`bench/step3-vital-history-trial.py`。完整请求和响应：`logs/step3-vital-history-trial-edc89ff-v2.jsonl`；逐帧分布：同前缀`.summary.json`。此结果不支持将任一候选上线。
