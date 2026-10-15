# 集中修复实测：断剑、换装、继续战斗、结束

已读核心理念、常在线计划第4–8步、题目约定15–17节。用户授权先推进后集中处理。仍在codex/reflex-always-on，未合并、未推送。

## 本轮修改

| 提交 | 修改与证据 |
|---|---|
| 17af08d | 缺失原生状态不再投影成死亡/饥饿/原点；旧代码4项失败，新代码相关80 passed |
| 6053ca4 | 保命动作失败结果也提供已有R3装备项，复用装备单节点树；227 passed，真实断剑帧12/12装备 |
| 58145e2 | 空闲保命动作也进入统一结果排队；没有原工作时用stay，unknown用工具自身timeout；230 passed，真实断剑结果12/12装备 |
| 06bca71 | 空闲身体动作不继承已完成Agent目标；231 passed，去旧task后正常战斗帧12/12继续 |
| 41c28d7 | 已观测health0的死亡动画实体不再当活敌对目标；232 passed，两个原误重试帧各12/12待着 |

最后定向命令：pytest tests/unit/test_body_loop_step6.py tests/unit/test_reflex_question.py tests/unit/test_body_loop.py tests/unit/test_direct_call_supervision.py tests/unit/test_body_loop_terminal_frames.py tests/unit/test_work_runtime.py -q。最终232 passed in 17.39s。新增回归均先在旧代码失败；旧断言未改。compileall和diff --check通过。

最新真正全量仍是7e4c1e6：2494 passed、1 skipped、291 subtests passed，pytest exit0。**本轮五个修复之后没有重跑全量；232不是全量。**整批修复尚未结束，不把旧全量当新提交结果。

## 四次运行保留全部失败

均为独立SDK/真实deepseek-v4.1-flash Agent、真实Decision、用户启动器；25569/25579/8771。沿用人工高台世界和角色，不是全新世界/自主生存。传送、清包、给损坏木剑和备用石剑、生成NoAI目标、时间控制均有fixture记录。脚本只注入场景和/goal，没有直接delegate或指定Decision答案。

目录前缀为 `/home/qwait/MineBot-always-on/logs/reflex-always-on-20261014-`。

| 后缀 / 源码 | 观察与结论 |
|---|---|
| default-equipment-fixed / 6053ca4 | 巡视完成，工作中选躲避；空闲后断剑但没有结果题，未装备，call_agent9次；Agent实际投递2次（completed、handoff），目标清除前health7.604，角色20 |
| idle-equipment-fixed / 58145e2 | 断剑→装备真实发生；战斗运行题仍带已完成走路task，call_agent3次、实际投递2次；最终击杀后repeat1次，角色20 |
| idle-equipment-context-fixed / 06bca71 | 装备、4次攻击、击杀；正常战斗误停0、额外Agent投递0；尸体health0仍被写成活敌人，结果repeat2次，角色20 |
| idle-equipment-dead-fixed / 41c28d7 | 完整换装备与击杀链走通；误停0、repeat0、额外Agent投递0；结果选stay，角色20 |

最后一次关键seq：316 Agent移动计划completed，321完成事实投递；412空闲engage；416 wooden_sword tool_broke；425 Decision选equip:stone_sword；428 native equip_item completed；438装备结果题next_step；536原目标killed（4次攻击），之后default_result_frame选stay，不再发原目标调用。最终RCON主手槽位1、slot1石剑Damage4、目标不存在、角色Health20；夹具清除命令返回No entity。不是夹具替它击杀。

该短场景验证的是备用装备闭环与这类死亡动画误重试。它没有验证长时间生活、中断handler、重复下水、不相关失败的装备负对照、工作中同一engage被默认engage重复提供。不能算整批通过。

## Decision参与度

| 运行 | 秒 | idle / running / plan_result / protective_result | 答案 | 中位ms | 实际Agent投递 |
|---|---:|---|---|---:|---|
| default-equipment-fixed | 53.53 | 20 / 30 / 2 / 1 | stay9 keep15 next2 dodge1 resume1 engage11 call_agent9 | 131.06 | 2 |
| idle-equipment-fixed | 27.30 | 14 / 8 / 2 / 3 | stay11 keep1 next2 engage4 equip1 call_agent3 repeat1 | 129.32 | 2 |
| idle-equipment-context-fixed | 21.76 | 12 / 6 / 2 / 4 | stay12 keep3 next2 engage1 equip1 repeat2 | 131.87 | 1（原任务完成） |
| idle-equipment-dead-fixed | 23.33 | 12 / 6 / 2 / 2 | stay12 keep3 next2 engage1 equip1 | 132.30 | 1（原任务完成） |

各运行delegate_work1、read_reflex0、没有死亡、Agent/client exit0。请求/回答差额来自终态取消在途运行题，unknown均0；投递事件与Agent回合次数分开。

最后一次Decision改变流程按不同事情去重：反击、换备用装备、结束后待着三件；实际保命选择engage1、装备选择1，没有call_agent或repeat。同类失败重复次数另外列在前表，不拿它们增加参与度。

原metrics从公共tool_raw_result推native时间，会漏掉战斗回执省略的elapsed_ms。本次补读client.log中原生MINEBOT_PHASE_A terminal JSON，以action_id去重，原生sampled_at_ms为终点、elapsed_ms为区间长度。没有给工具回执加字段。最后一次三条可观测native终态合计2.961秒；这些区间内running题3次，60.79题/工作分钟。其余20.367秒包括Agent思考、启动、结算等，不当精确身体空闲。短样本不报分位数或称长期稳定。

详细每分钟用途/状态、反应链、投递逐条、native时长原件为各运行metrics.json、native-terminal-timing.json。旧统计仍保留原算法，新增native_client_timing明确单列来源和局限。

## 状态与交付

记录和脚本打包到evidence-equipment-fixes-20261014，完整trace留在四个运行目录。此前误报死亡修复有单元及真实旧证据，还未专门重跑断线/重连验收。所有owned客户端和Agent退出、专用服务器RCON stop、测试端口释放；保护端口未动。

未过项：第3步反复下水的语义判断，interrupt_when真实handler，普通engage的同目标重复选项，30分钟生活/玩家互动、断线后的循环生命期与专门重连验证，以及本轮后的最终全量。继续集中修，不标整个目标完成。
