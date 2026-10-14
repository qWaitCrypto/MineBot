# 集中修复：保命动作结束后也能选择备用工具

依据真实step6-backup-next的seq759：默认engage_entity返回tool_broke、canRetry=false、metrics.broken_item=wooden_sword；背包stone_sword1、主手空。此前结果只提供resume/call_agent，漏掉既有R3装备项。已读核心理念、常在线计划第6步和题目约定15.3。

## 离线先验

从记录的compiler_input、frame_input生成题，不读现成state代替原始事实。旧选项请求体逐字复现；真实Decision原答案resume与记录一致。使用普通工作结果已有的装备文字补同一候选，三项六种排列各两次：12/12选equip:stone_sword。请求、原始响应在logs/default-equipment-trial-17af08d.jsonl。

实现后再次直接调用线上after_default_options和同一_question编译，保留所有原始事实与选项范围：旧基线仍resume；12/12选equip:stone_sword。原件logs/default-equipment-online-17af08d.jsonl及summary.json。没有调题目措辞、改概率门槛或按失败原因强制装备。

## 修改

reflex_question.py抽出equipment_options，普通结果与默认动作结果共用一句选项文字与同类别、已有库存的条件。成功结果不提供装备项，运行帧不提供装备项，canRetry=false仍可以先装备再重新调用。

body_loop.py将该选择接入现有_run_result_equipment。装备本身是单节点树，要由Decision判断装备结果；通过后重做刚才的保命调用，之后再问回原工作还是交Agent。装备持有者用既有threat，装备期间仍逐帧监督。沿用原调用剩余timeout，没有新状态锁、停止入口或次数规则，没有自动选装备。

## 定向

两项新测试在旧代码上失败：保命结果缺装备项；选择装备后未经过装备节点重做保命动作。修复后覆盖共同结果编译、原调用/保命持有者、中断、unknown、终态和工作树的六文件测试：

```text
227 passed in 17.44s
```

pytest tests/unit/test_body_loop_step6.py tests/unit/test_reflex_question.py tests/unit/test_body_loop.py tests/unit/test_direct_call_supervision.py tests/unit/test_body_loop_terminal_frames.py tests/unit/test_work_runtime.py -q。没有改旧断言；前后日志logs/default-equipment-{before,after}.log。

## 未验证

这是接线与离线语义通过，尚未重跑客户端实战，不报真实备用装备验收通过。真实参与度仍按旧step6-backup-next：idle15、running13、default_result2、plan_result91，装备0次、wake0；这些旧失败没有被新单元替代。针对同一目标的默认动作重入、目标消失后的repeat91次，仍需分别处理。旧全量2494通过属于7e4c1e6，本次不冒用。
