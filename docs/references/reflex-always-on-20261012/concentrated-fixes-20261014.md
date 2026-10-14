# 集中修复与复验（2026-10-14）

读了核心理念、常在线执行计划、第7/8步证据。分支仍为 `codex/reflex-always-on`，未合并、未推送。整批未完成。

| 项 | 状态 |
|---|---|
| 运行答案丢事实，109次回家未执行 | 已修；离线109/109题面不变；g真实运行题切到回家并执行 |
| 工具结束后的迟到运行答案 | 已修；195项定向通过；g/h各丢弃1个迟到答案 |
| SDK读取排在反射写动作后 | 已修；旧版2项超时，新版100项通过；h读取任务完成，长动作并行实测未得到 |
| 死亡35秒延迟 | 新增停止阶段计时；受控死亡405ms结算，旧延迟未复现，原因未定 |
| keepalive单测偶发 | 仅调整测试时间尺度；整个文件21项、16子项通过；不改生产超时 |
| 全量 | d050458：2491 passed、1 skipped、291 subtests passed；无测试失败；1252.35秒 |
| 生活真实验收 | 失败：回家导航失败、look/stay反复切换；未跑满30分钟 |
| 环境 | 两场会话exit0；专用测试服务器停止；25569/25579/8771释放；原世界和trace保留 |

## 提交与测试

- `9023943`：运行答案绑定它所回答的完整帧；worker终态取消悬挂运行题；停止阶段计时。新测试6项，不改旧断言。定向真实结果：195 passed in 12.82s。
- `1d986c8`：keepalive测试读取超时500ms、心跳50ms×20。事件间隔仍长于读取超时，silent/EOF仍必须失败。21 passed、16 subtests passed in 28.51s。具体原因和边界见 `keepalive-test-fix.md`。
- `d050458`：life/threat占用写通道时，Agent读取走现有读通道。修复前2 failed；修复后100 passed in 3.79s。不加锁、不变更持有者/停止入口。

109个原始回家帧经线上编译函数重建：109个题面逐字相等，旧原始快照能绑定0个，完整出题帧能绑定109个。没有再问模型，不算109次身体执行。

最新全量是真正的 `python -m pytest tests/unit -q`，低优先级、单CPU后台运行：`2491 passed, 1 skipped, 291 subtests passed in 1252.35s (0:20:52)`。失败测试：无。日志 `logs/final-suite-d050458/pytest.log`，完整副本在证据目录。

收尾shell语句在跨PowerShell/WSL传参时错误展开了退出码变量，空参数导致 `bash: line 1: exit: : numeric argument required`，外层脚本退出1，退出码文件只有空行。因此不报“脚本exit0”，也不把这个收尾错误列成pytest测试失败。原日志/空退出码文件保留。未来用 `logs/run-unit-suite-recorded.py` 直接记录子进程returncode；本轮没有为修收尾再重跑全量。

## g真实复验：答案接线及死亡计时

目录 `/home/qwait/MineBot-always-on/logs/reflex-always-on-20261014-step7-fixed-g`。加载9023943；SDK、DeepSeek v4.1 Flash；旧世界旧背包，初始背包空，但不宣称全新空背包实验。仅25569/25579/8771。配置隔离，没有默认房屋目标。

14分19秒，共817请求、798个反射答案；其余请求可能被调用结束/会话停止取消，不能当超时答案。idle_frame516、running_frame281、result_frame20。会话每分钟分别36.05、19.63、1.40；有native时长证据的工作区间内running_frame为54.85题/分。Decision中位144.36ms，unknown2。按loop_state：idle774、work43。

| 答案 | 次数 |
|---|---:|
| stay | 399 |
| keep_going | 188 |
| look_player | 117 |
| go_home | 70 |
| repeat | 14 |
| call_agent | 6 |
| unknown | 2 |
| move_away | 1 |
| next_step | 1 |

按不同的事报告，不把同一失败算多种流程变化：

- `seq2768`：看向玩家期间选go_home；`2769–2775`走现有停止结算；`2776–2788`进入真正move_to，参数是档案住处。这证明运行答案能执行，最终导航失败，不证明到家。
- 70次go_home调用全部 `baritone_stopped_without_result`，是同一导航失败重复。停在树冠 `(173.5,74,163.5)`，到受控死亡前没有走回住处。住处脚部和头部格均为空；不是凭空把目标设在实心格里。未另改导航。
- look_player有49次completed、67次preempted；117个选择不能写成117个成功动作。运行题stay73次，存在反复look/stay切换，生活稳定性未过。
- 结果题repeat14次、call_agent5次；另外1个call_agent在运行题。未出现此前90次同节点重试，但仍有同失败重试，不宣称语义全部通过。
- 一次move_away结果 `already_safe`，不能算实质逃离。

普通用户任务在生活移动期间发送（fixture记录life_request_seq2784），Agent自主调用move_to和读取地形；delegate_work0，read_reflex1（失败诊断）。Agent的走路任务在受控死亡前未完成。受控死亡后重生在另一位置，再正常导航到家：不能拿这个当最初任务按约束走路脱困成功。

生活验收在fixture的 `life_acceptance_ended_early` 明确结束：导航失败、反复切换，未做30分钟验收。之后单独 `/kill Step3CurrentBot` 测死亡链；这是**受控死亡1次**，不是自然死亡。Agent猜可能creeper，外部fixture证据证明是kill，报告以fixture为准。

死亡 `seq7567`，重生 `7570`；native重生240ms，Python死亡至重生事件805ms；停止结算405ms（interrupt233ms、native_probe172ms，其余不足1ms）。死亡/重生投递分别 `7572/7596`，共2个body_event_wake，reflex_fact_wake0；不把Agent启动计作唤醒。请求最长间隔5.55秒。旧35秒延迟未复现，不能宣称查清。

终场存活，生命6.5、饥饿17、氧气300。生活验收失败。native终态可量出的工作93.0秒；完整受监督调用区间183.0秒，区间外675.8秒。后两者含结算/等候，不是人物实际移动时长，缺native时长的视线动作不冒充空闲。

## h真实复验：最新源码及只读任务

目录 `/home/qwait/MineBot-always-on/logs/reflex-always-on-20261014-step7-fixed-h`。加载d050458，保留服务器/世界/角色，独立新Agent会话。2分50秒，197请求，idle_frame114、running_frame83；179答案（look84、keep_going64、stay27、unknown4）。中位138.95ms，Agent投递唤醒0，delegate_work0、read_reflex0。

普通用户读取任务在生活运行帧146之后发送，Agent在338调用read_inventory，347拿到空背包回执，541输出正常回答。83个look completed、1个preempted。读取结束时此前视线动作已结束，**没有取得“长反射动作尚在跑，Agent读取已完成”的真实证据**；这一点仅由定向测试验证。

终场存活，生命6.5、饥饿17。此场未验证回家、危险反应或30分钟生活。不含native elapsed_ms，不能把工具执行时间记成0；受监督调用区间23.2秒、区间外147.1秒，仅为调用层时间。

## 证据与仍未过的项

本目录 `evidence-answer-dispatch/` 保存定向输出、旧版读取失败、109帧绑定结果、g/h指标、每次动作/交接事件和fixture时间线。完整trace在上述两个run目录的 `trace.jsonl`；旧f的75MB trace未删除。

metrics补了受监督调用区间，并将“没有任何native时长记录”的空闲估计设为缺测，避免h场83次视线执行被误记为全程空闲。这只改报告统计。

尚未过：30分钟生活、树冠导航/回家、生活动作稳定性、旧死亡延迟根因、长反射动作与Agent读取的真实重叠、第3步打断历史语义、第5步完整反击/退回/恢复、第6步handler与备用装备。没有加次数上限、概率门或语义阈值来让它们通过。
