# 集中修复：断线不再成为死亡事实

依据第7步step7-next：seq1034的health0、missing=true、complete=false来自投影，不是原生生命观测。客户端日志确认断线；退出存档Health20、DeathTime0。已读核心理念、执行计划5.5死亡和第4步要求。

`RealClientBody.get_reflex_facts`仍保留公共BodyState的别名和缺失标志；但原始采样没有health/food/oxygen/time/dimension时，去掉投影提供的默认值；投影missing时去掉虚构pos。公共get_state兼容性不变。真实采样health0原样保留，原生death事件仍从原通道通知、停止、恢复。

四项新回归在旧代码上全失败：断线缺生命、部分位置采样缺生命、无位置但真实health0不能附带虚构原点、断线样本不应停止当前工作或发死亡通知。修复后三个相关文件全跑：

```text
80 passed in 2.20s
```

命令：pytest tests/unit/test_real_client_body.py tests/unit/test_body_loop_step4.py tests/unit/test_body_loop_running_perception.py -q。没有改旧断言。前后日志原件logs/missing-native-state-{before,after}.log。

这是Body事实边界修复，没有改Decision题目、执行门槛、状态锁或停止入口。本轮未新开游戏；真实断线/重新进服验收尚未重跑。旧全量2494通过属于7e4c1e6，不当成本修复全量。上一轮生活运行参与度及误唤醒仍按step7-next-20261014.md保留，未用单元通过覆盖真实失败。
