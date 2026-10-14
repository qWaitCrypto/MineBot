# 明确末端报告与保留实体观察事实

读了核心理念全文、常在线执行计划、第15节和每帧题目约定全文。继续在 `codex/reflex-always-on`，不合并、不推送。产品提交 `c0ed3ee`。这是当前整体验收中的窄推进，不宣布“让 Agent 自己生活”完成。

## 为什么改

- `player-handler-fixed` seq862：目标[220,121,220]，当前x205.8，phase moving，有movement进展。原选项说“this work is finished”，真实选择next_step并提前停下。原始事实编译逐字相同，原答案复现。选项不增删，仅明确报告Agent原意图已达成。
- `step6-native/unrelated-frame.json`：原生target_not_found已有search_radius32/entities_observed0，Agent projector的summary也保留。结果题以前只编失败码，丢了范围和数量，重排时会误选换剑。现在在同一失败句后编入“The tool observed 0 entities within 32 blocks.”，不推断目标在世界不存在。
- `minebot/body/combat.py` 真实生产处：实体读取成功但请求目标不匹配时，记录该次读取的实体总数和请求半径。不是战斗次数，不是目标匹配数，未读取成功时没有该计数。不加按工具/失败码白名单，不改canRetry，不加结果字段、停止入口或概率门。

## 离线方法和结论

36帧、每帧1次基线和12次候选，共468次真实Decision请求。5个最近窄问题帧先恢复线上键顺序并逐字匹配记录题、复现记录答案。15个运行帧和16个结果帧使用保存的原始输入，结果帧走WorkPlanExecutor._call_question；候选在唯一出题器上临时替换后重编，未修改事实或动作集合。历史fixture不能声称逐字等于早期线上题：既有修复已经增加主手和装备选项。本报告只对5个最新问题帧作原题复现断言。

18个有明确标准的帧：**216/216命中**。包括正常工作、未到目标继续、已到目标报告、真断工具换备用、不相关目标缺失交还Agent。候选只改6帧，其他30帧题面不变。三选项只有6种不同顺序，二选项只有2种；重复到12次，不冒充12种不同排列。unknown单列（本次无unknown）。

16个历史结果帧没有新指定标准，只报分布，不纳入命中数。running3482是旧边界帧：keep_going4/next_step4/call_agent4；四个旧采铁失败结果帧3785/3805/3825/3845仍受顺序影响；其题面本轮未改变，不能算本次修复通过或偷偷补规则。

| 帧 | 基线 | 候选分布（12次） | 命中 | 题面变化 |
|---|---|---|---:|---|
| player-handler-fixed:333 | keep_going | {'keep_going': 12} | 12 | False |
| player-handler-fixed:862 | next_step | {'keep_going': 12} | 12 | True |
| life-30-final-v2:4863 | next_step | {'next_step': 12} | 12 | True |
| running:471 | keep_going | {'keep_going': 12} | 12 | False |
| running:957 | keep_going | {'keep_going': 12} | 12 | True |
| running:1479 | keep_going | {'keep_going': 12} | 12 | False |
| running:2881 | keep_going | {'keep_going': 12} | 12 | True |
| running:3237 | keep_going | {'keep_going': 12} | 12 | False |
| running:3241 | keep_going | {'keep_going': 12} | 12 | False |
| running:3245 | keep_going | {'keep_going': 12} | 12 | False |
| running:3249 | keep_going | {'keep_going': 12} | 12 | False |
| running:3253 | keep_going | {'keep_going': 12} | 12 | False |
| running:3257 | keep_going | {'keep_going': 12} | 12 | False |
| running:3268 | keep_going | {'keep_going': 12} | 不计分 | False |
| running:3375 | keep_going | {'keep_going': 12} | 12 | False |
| running:3380 | keep_going | {'keep_going': 12} | 12 | False |
| running:3482 | next_step | {'next_step': 4, 'call_agent': 4, 'keep_going': 4} | 不计分 | False |
| running:4965 | keep_going | {'keep_going': 12} | 12 | True |
| plan_result:596 | call_agent | {'call_agent': 12} | 不计分 | False |
| plan_result:1511 | next_step | {'next_step': 12} | 不计分 | False |
| plan_result:1523 | repeat | {'repeat': 12} | 不计分 | False |
| plan_result:1538 | next_step | {'next_step': 12} | 不计分 | False |
| plan_result:1552 | next_step | {'next_step': 12} | 不计分 | False |
| plan_result:2450 | next_step | {'next_step': 12} | 不计分 | False |
| plan_result:2562 | next_step | {'next_step': 12} | 不计分 | False |
| plan_result:2712 | repeat | {'repeat': 12} | 不计分 | False |
| plan_result:2720 | call_agent | {'call_agent': 6, 'repeat': 6} | 不计分 | False |
| plan_result:3009 | next_step | {'next_step': 12} | 不计分 | False |
| plan_result:3024 | next_step | {'next_step': 12} | 不计分 | False |
| plan_result:3112 | next_step | {'next_step': 12} | 不计分 | False |
| plan_result:3785 | repeat | {'repeat': 6, 'equip:wooden_pickaxe': 6} | 不计分 | False |
| plan_result:3805 | repeat | {'repeat': 6, 'equip:wooden_pickaxe': 6} | 不计分 | False |
| plan_result:3825 | equip:wooden_pickaxe | {'equip:wooden_pickaxe': 8, 'repeat': 4} | 不计分 | False |
| plan_result:3845 | call_agent | {'call_agent': 4, 'equip:wooden_pickaxe': 8} | 不计分 | False |
| broken-tool-frame-v3 | equip:stone_sword | {'equip:stone_sword': 12} | 12 | False |
| unrelated-frame | equip:stone_sword | {'call_agent': 12} | 12 | True |

## 实现与定向测试

- 所有产品改动仅在reflex_question.py。无next_call的末端报告引用Agent下发intent，内部下一步仍是具体调用。空intent兼容纯编译器测试；线上意图仍必填，没重新引入assistant播报回退。
- 原生metrics和projector.summary编出同一句观察事实。观察字段缺一个就不编造；成功仍仅“has ended normally”，不写数量或reported success。
- 新增8项回归：运行/结果末端意图两项，projector/raw观察一致一项，缺字段及bool不冒充计数四项，成功结束句不变一项。
- 两个既有fixture协议测试只允许这次故意的末端报告措辞变化及对应字符数变化；历史fixture原件不改，其他事实、内部next_call和原选项键全部逐字比较。
- 定向：pytest tests/unit/test_reflex_question.py tests/unit/test_reflex_running_intent_frames.py tests/unit/test_body_loop.py tests/unit/test_direct_call_supervision.py tests/unit/test_work_runtime.py tests/unit/test_work_language_runtime.py -q：**243 passed in 21.28s**。
- 上线编译与离线候选逐字比较：**36/36**，输出verify-specific-outcome-deployed.json。

## 真实运行与全量

b084609全量还在后台进行，不报通过数。完整日志logs/final-suite-b084609/pytest.log。

第一遍c0ed3ee真正全量：**1 failed, 2525 passed, 1 skipped, 291 subtests passed in1325.73s，exit1**。失败名test_step2_new_old_protocol_on_118_inputs_except_intent_wording_and_main_hand，seq981的末端报告文字仍要求旧句子，是本轮漏同步的测试预期；不是把未知失败称作“旧断言不一致”。b084609仅更新该测试：允许同样已离线验证的末端intent报告，旧模块输入和内部next_call仍逐字比较，改名为except_verified_question_changes。新增基线在内的定向244 passed in27.63s；产品源码未再改，c0ed3ee的真实运行仍对应最终行为。

磁盘版期间观察到测试进程jbd2_log_wait_commit，I/O full avg60约74.76%、内存压力0；没有假称模型或测试死锁。内存目录诊断版c0ed3ee-tmpfs发现已有基线预期失败后主动终止（exit−15，源码仍干净），不报通过；修正预期之后用b084609重跑全部tests/unit，和已完成的磁盘版记录分开保存。

首次specific-outcome无效：控制脚本240秒等不到反射会话，在客户端进入世界前发了/quit，未开始巡视。确认为夹具启动窗口问题，不当成Agent/runtime失败。原件保留，不删除/覆盖。客户端重新以已有缓存的offline Gradle启动，同一个自有服务器，新建Agent状态，改正控制脚本等待窗口。没有改产品逻辑或答案。

有效场specific-outcome-rerun：SDK/deepseek-v4.1-flash；同一人工高台世界，25569/25579/8771。观察trace 71.649s，不是30分钟生活或新世界生存验收。机器人**health20、无死亡**，launcher exit0，测试端口已释放。

| 验证 | seq / 原生事实 |
|---|---|
| 看向Visitor | seq34派发、41结束；yaw0→−89.7°，位置仍[220.5,121,220.5] |
| 真实Agent计划 | delegate_work1；Agent自己写三段巡视与中断树，未由脚本调用manager.delegate |
| 第一站 | seq371原生arrived，x249.303，调用pos[250,121,220]/radius1；380选下一步 |
| 第二站 | seq627原生arrived，x191.692；639选repeat，641再次arrived，x191.596；652再repeat，654arrived位置不变；666才下一步 |
| 最后一站 | seq870原生arrived，x219.277；881选报告目标完成。RCON最终[219.373,121,220.500]，到起点附近，未在x205–206远处早停。调用radius1，不能称精确踩在x220.0 |
| 中断漏判 | 24道有interrupt的运行题，其中23道主手空、有石剑；一直没选interrupt，无handler、无resume。本轮中断验收不通过 |
| 通知 | seq882计划completed，883入队，887真实唤醒；queued→wake0.432s。没有把Agent启动算唤醒 |
| Agent后续 | checkpoint_task/write_memory/read_task各1，read_reflex1发生在终态唤醒之后（seq1068），不是工作中轮询；本场人工/quit收尾，未声称目标完成验证器通过 |

**物理巡视三站到达，末段远处误停本场未再出现；仍有第二站两次近目标重复和中断条件漏判。**不把工具arrived当作整个常在线目标达成；这里同时给实际位置及半径，用户可核对。

Decision按用途：idle20、running25、plan_result5，共50道；回答unknown2（其中一次TimeoutError）、stay17、look_player1、keep_going20、next_step3、repeat2。工作中选中停止/中断0。延迟中位136.655ms；所有unknown未启动动作。

可观测原生工作21.061s，区间内running题21，**59.83题/工作分钟**。余量50.588s包括Agent推理、结算和其他未计时动作，未冒充准确物理空闲。Agent真实反射唤醒1次，原因计划completed；delegate_work1、read_reflex1。

Decision改变流程按不同事情去重：看向Visitor一件、推动巡视下一站/末端报告一件、原调用重试一件。重试第二站2次单列，不能报成2种新能力。中断装备、resume和备用装备本场没有执行，不计入本场动作数；R3负对照改善仅有本轮真实服务离线证据。

## 剩余与继续顺序

23帧有条件/事实仍未中断，是新的真实漏判，不说“没有触发窗口”。seq339完整题面已保存；不在这轮临时加条件检测器、阈值或参数生成来凑通过。先留作集中处理。

正常长调用零误停、重复下水、同目标engage重复、同客户端同SDK重连、30分钟正确互动与任务正常终结、玩家块完整保护等整体验收仍有缺项。此前完成验证器exit_code7和初始化保护的边界继续保留。这轮没有修改Java或扩张导航。

完整trace、client.log、SQLite、夹具：`/home/qwait/MineBot-always-on/logs/reflex-always-on-20261014-specific-outcome-rerun`。无效启动保留在 `logs/reflex-always-on-20261014-specific-outcome/`。提交证据 `evidence-specific-outcome-20261014/` 包括全部离线请求/回答、36帧逐字部署检查、50道真实问题和关键原生终态。未合并、未推送。
