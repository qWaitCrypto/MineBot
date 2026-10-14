# 集中修复：空闲保命不继承已完成的Agent任务

实际58145e2运行 `reflex-always-on-20261014-idle-equipment-fixed`：Agent巡视seq313 completed、seq318收到完成投递。随后seq413木剑损坏，seq422 Decision选equip:stone_sword、seq425真实equip_item成功、seq435允许重做原战斗。最终RCON SelectedItemSlot1、slot1石剑Damage4；seq599身体killed，清除前RCON目标不存在；health20，无死亡。换装备真实发生，Agent没有发equip_item，read_reflex0。

但是seq440/550/576的运行题均误停战斗，触发了额外Agent交接；击杀结果seq613还选repeat，之后target_not_found。整轮威胁稳定性未通过，不能只报换装成功。

三道误停题的task仍是已经完成的走路任务，agent_intent却是反击。_execute_protection没有显式goal，_plan_context从持久Agent状态带回旧目标。空闲题本身不带它，只有空闲中实际动作的运行题受污染。属于上下文编译错误，不是Agent未写意图。

## 离线

以seq440对应的原始frame_input、compiler_input经线上_question重建请求，逐字相等；真实服务仍答call_agent。只把传入上下文的goal设空，生成请求恰好只删掉旧task，其他句子、选项、生命、主手、进度、位置完全不变。三选项六种排列各两次，12/12选keep_going。脚本与请求/响应原件logs/idle-task-context-trial-58145e2.{py,jsonl,summary.json}（脚本实际名idle-task-context-trial.py）。

## 修复与测试

没有挂起Agent工作（record=None），或已登记idle_protection的Body动作，下发时显式goal空。仍有挂起工作的保命动作保持原来的目标来源。没有清理Agent自己的持久目标或记忆，没有改题目措辞、强行继续或添加门槛。

新增回归旧代码KeyError(goal)失败；修复后六个相关文件231 passed in 17.35s。旧断言未改，原目标对象未变且工作中断仍保留其来源。日志logs/idle-task-context-{before,after}.log。此前全量2494通过属于7e4c1e6，本修复没有冒用该结果。

本修复尚待同夹具真实复验；先前装备运行的完整参与度、Agent投递和trace原件保留，不把离线12/12当作真实零误停。
