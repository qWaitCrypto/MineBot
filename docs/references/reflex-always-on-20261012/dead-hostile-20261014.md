# 集中修复：死亡动画里的实体不是活敌对目标

06bca71真实运行的seq454身体返回killed，seq457/464/472原始实体facts仍含同一实体health0，但题面都写“Nearest hostile mob ... 1 blocks away”。这不是新增事实缺失，是已有生命事实被忽略。seq460/467选repeat，产生两次target_not_found。

hostiles编译器排除**已经观测到health<=0**的实体。没有生命观测时仍保留候选；没有固定生物名单、危险分数或按次数停止。共享hostiles的事实句、反击/躲避选项和工具目标绑定保持一致。死亡是确定的游戏终态，计划是否完成仍由Decision选择。

从git提取06bca71旧出题代码到临时目录，给新旧同样的原始compiler_input/frame_input。两帧旧请求逐字复现、真实服务都复现repeat。新编译结果只把threats改成既有“No hostile mob is in view.”，其他字句、选项、结果和历史不变。各六种顺序两次：seq457 stay12/12，seq464 stay12/12。没有编辑原始health、删历史或改canRetry。日志logs/dead-hostile-trial-06bca71.{jsonl,summary.json}，脚本dead-hostile-trial.py。

新增回归在旧代码上失败（dead成为最近目标），修复后验证：忽略已死近处实体、仍看见后面的活实体、只有死亡实体时无反击/躲避项、未知生命实体仍保留。六个相关文件232 passed in 17.39s，旧断言未改；日志logs/dead-hostile-{before,after}.log。

最新全量2494通过仍属于7e4c1e6。当前修改尚未重新实战；没有合并、推送。原真实运行正常唤醒1次、额外交接0、换装/击杀真实发生，但两次误重试原样保留，不用离线通过抹掉它们。
