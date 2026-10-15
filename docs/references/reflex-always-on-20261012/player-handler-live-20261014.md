# 玩家事实与中断后续接线：窄修实测

读了核心理念全文、常在线任务书第6–8步、第15节。产品提交 `5703f3f`，报告前一提交 `2860d02` 保留完整30分钟失败证据。分支 `codex/reflex-always-on`，未合并、未推送。

**玩家看向和中断恢复接线已真实验证；完整巡视未完成，整体验收仍不通过。**归类为 runtime/Decision 行为未收敛，不是夹具配置错误，也不是 Agent 给错计划。

## 改动与测试

- `reflex_question.nearby_players` 接受原生 `entities`、既有 `nearby_entities` 和 `entities_near`；显式空列表保持权威。不加新的事实类别、判断阈值或选项措辞。
- `work_runtime._handle_interrupt` 把已经解析并执行过的挂起调用编入 handler 的后续选项。sequence最后一步、choice选中子树、batch最后一项继承后续调用。既有 handler 终态仍由 Decision 判断，然后既有结果题由 Decision 选 resume；没按工具success自动推进。
- 新增9项单元用例：3种原生玩家字段接题面、选项和真实工具绑定；显式空采样不复活旧实体；call/sequence/choice/batch/adaptive_batch五种handler末端有正确后续，内部步骤不提前指向原工作，resume仍必须由Decision选择。
- 旧源码同一回归用例7 failed/2 passed；修复后9 passed。完整定向命令覆盖新用例、life_profile、body_loop_step6、work_runtime、reflex_question、body_loop_connection、direct_call_supervision：**220 passed in 16.74s**。旧断言未修改。
- `5703f3f` 真正全量：**2518 passed, 1 skipped, 291 subtests passed in 888.37s (0:14:48)，exit 0**；结束 tracked source dirty=[]。

## 真实场景和证据

独立端口25569/25579/8771，SDK，deepseek-v4.1-flash。新Agent SQLite/日志，保留人工高台世界和角色。先开启档案并控制Visitor6格，随后清空装备、给木剑与备用石剑、让真实Agent写巡视和中断树。不是自主生存，不是30分钟生活复验。launcher exit0，机器人health20、无死亡，独立进程已退出。

| 项 | seq / 实际发生 |
|---|---|
| 玩家事实和动作 | seq32选look_player，33派发，40原生结果success；RCON朝向0°→−89.4°、pitch15.15°，位置未动 |
| Agent下发树 | delegate_work 1次；计划原件在tool_result.full_result.metrics.tree；不是脚本调用manager.delegate |
| 正常移动 | seq376到[250.43,121,220.5]，seq793到[190.57,121,220.5]，两次arrived |
| 条件触发 | 夹具seq339之后移走主手木剑；seq795答interrupt但第二段已经自然到达，没有handler；第三段seq810再选interrupt |
| 真实停止和handler | seq819既有通道停第三段，822 handler_started，825装备成功，835由Decision判next_step，836 handler_finished |
| 恢复 | seq841选resume，842记录请求/答案；随后同一原move_to重新运行并真实移动 |
| 提前停止失败 | seq862题：目标[220,121,220]、当前[205.8,121,220.5]、phase=moving、最近进展movement；seq869选next_step，870原生取消，最终x206.74，没到220 |
| 终态通知 | seq882计划报告completed，seq883入队，888真实唤醒（约0.42s），但物理目标未达成，不能当作巡视成功 |

从夹具移走木剑到第一次interrupt回答约17.5s，到实际选中并停止约18.5s。不是即时发现；没有省略这段延迟。没有为了验收强制选择、换答案、概率门或新的停止入口。

## Decision参与度

trace 61.314s：

| 用途 | 请求 | 每会话分钟 |
|---|---:|---:|
| idle_frame | 16 | 15.66 |
| running_frame | 23 | 22.51 |
| plan_result_frame | 4 | 3.91 |

回答：`{'unknown': 1, 'stay': 14, 'look_player': 1, 'keep_going': 17, 'next_step': 4, 'interrupt': 2, 'resume': 1}`。中位133.173ms。不同事情为看向玩家、中断后装备、恢复移动、提前宣告终段完成；同一中断条件2次选择/1次真实handler单列，不能报2种能力。

可观测原生工作19.507s；工作区间running题20，61.52题/分。余量41.807s包括Agent推理、结算和无原生计时的瞬时动作，不能全称物理空闲。delegate_work1、read_reflex0；反射事实真实唤醒1次，原因为计划终态completed，非handler额外叫醒。

## 原始帧离线回放

- 提前停止seq862：按原始frame_input/vital_samples/时钟走线上BodyLoop编译，题面逐字相等，原next_step答案复现。12种顺序：**keep_going10、next_step2**。未通过。完整题目原文在 `patrol-early-stop-replay-5703f3f.jsonl`。题面已有目标、起点/当前位置、阶段和实际推进，暂未证明是某一事实遗漏，不武断归因。
- 旧中断装备后seq718：候选只补具体后续调用next_step12；只补背景call_agent12；两者next_step12。补充的后续恢复题使用同一真实装备后身体事实，resume12（是前瞻题，未冒充原记录里的结果题）。新运行seq835/841给出实际闭环证据。
- R3负对照仍未过。原题call_agent6/equip6；仅在离线把target_not_found翻成人话“the selected entity was not found (target_not_found)”后call_agent10/equip2。原答案先复现，选项/字段均不改。**候选未部署**，没加“工具无关就不给装备”的检测器。

## 还没过

完整巡视、正常长调用零误停、R3负对照、重复下水、同目标反击重复、同客户端同SDK重连、30分钟正确互动与正常任务结束均仍有缺项。此前完成验证器exit_code7的已记录缺陷仍暂不修。存活和测试通过不能替代这些验收。

原件：`/home/qwait/MineBot-always-on/logs/reflex-always-on-20261014-player-handler-fixed`，完整trace/client.log/SQLite/夹具均保留。提交证据 `evidence-player-handler-20261014/` 包括所有结果题、关键事件、原生终态、定向测试和原始帧回放请求/回答。
