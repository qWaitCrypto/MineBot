# 集中修复：空闲保命动作也交结果给Decision

真实复验 `reflex-always-on-20261014-default-equipment-fixed` 使用6053ca4，独立SDK Agent /goal、沿用人工平台世界、25569/25579/8771。工作中选了move_away，巡视完成并投递completed；空闲后才engage。seq900木剑真实tool_broke，随后没有结果题，直接回空闲再engage，备用石剑没有装备。后续多次running call_agent。这轮不算备用装备通过，存活health20，目标清除前health7.604。Agent/client exit0。

原件是该目录trace.jsonl、fixture-events.jsonl、metrics.json；人工武器、传送、NoAI目标均明确记录，不当自主生存。根因是_run_idle_protection仅记录结果和经历，未接_after_default_action；此前修复只补齐有挂起工作的保命结果路径。

## 补齐

空闲保命动作结束后，沿用同一个终态排队和_after_default_action，不另开Decision循环。没有被挂起的工作时，不提供虚构的resume；提供既有stay、同调用repeat、失败时已有装备候选、call_agent。stay文字复用idle_options。task为空，意图为身体刚才启动的保命动作目的；不带已经结束的Agent目标。

装备仍是既有单节点树；运行中带原保命选项身份、threat持有者，避免装备期间错误切换为另一种保护状态。装备完成后由Decision允许重做原保命动作。unknown不写动作，结果等待只用刚才工具的参数/sidecar timeout_s，超时返回实际结果、回空闲，不新增数字。只有选call_agent才通知Agent；否则只记近期经历。

## 证据

新增空闲结果接线回归，旧代码0道结果题而失败；补齐后通过。另覆盖unknown使用工具超时、不重复写动作，以及call_agent恰好一次通知。旧测试未改断言。

```text
230 passed in 17.41s
```

同前六个相关文件，日志logs/idle-protection-result-{before,after}.log。

离线使用本次真实seq900工具回执、紧接着seq907读帧的compiler_input.facts。先逐字恢复原seq907空闲题，真实服务engage与记录一致；再用线上after_default_options和_question生成新结果题，三项六种顺序各两次，12/12选equip:stone_sword。logs/idle-equipment-trial-6053ca4.jsonl及summary.json。新结果题历史上不存在，不声称复现过一个未记录的终态请求。

这是离线与接线验证，尚未重新实战。最新完整全量仍是7e4c1e6的2494通过，不将它标成本提交全量。新真实短验收另记。
