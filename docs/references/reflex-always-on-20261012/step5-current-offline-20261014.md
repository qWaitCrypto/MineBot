# 第5步当前源码离线补测

原始 native 帧来自 reflex-always-on-20261014b-step5-native-frames。是控制台人造 NoAI 场景，不是 Agent 的动作证据。线上 idle_question / idle_options 编译，18帧各12问。没有改题目文字。

| 帧 | 选择分布 | 原脚本建议命中 |
|---|---|---|
| zombie_3_full_empty | {"move_away": 12} | 只报分布 |
| zombie_3_full_sword | {"engage": 12} | 12/12 |
| zombie_3_hurt_empty | {"move_away": 12} | 只报分布 |
| zombie_8_full_empty | {"stay": 12} | 只报分布 |
| zombie_8_full_sword | {"stay": 12} | 只报分布 |
| zombie_8_hurt_empty | {"move_away": 12} | 只报分布 |
| zombie_20_full_empty | {"stay": 12} | 12/12 |
| zombie_20_full_sword | {"stay": 12} | 12/12 |
| zombie_20_hurt_empty | {"stay": 12} | 只报分布 |
| skeleton_3_full_empty | {"move_away": 12} | 只报分布 |
| skeleton_3_full_sword | {"engage": 12} | 只报分布 |
| skeleton_3_hurt_empty | {"move_away": 12} | 12/12 |
| skeleton_8_full_empty | {"stay": 12} | 只报分布 |
| skeleton_8_full_sword | {"stay": 12} | 只报分布 |
| skeleton_8_hurt_empty | {"move_away": 12} | 12/12 |
| skeleton_20_full_empty | {"stay": 12} | 12/12 |
| skeleton_20_full_sword | {"stay": 12} | 12/12 |
| skeleton_20_hurt_empty | {"stay": 12} | 0/12 |

原脚本8个建议计分帧中7个12/12；共84/96。18帧总216问，0unknown。建议标准不能代替用户对具体人造帧的裁定；NoAI/generic damage也不等于正在远射。此轮不宣称真实反击或退回验收通过。

原始请求与响应：logs/step5-native-threat-replay-6db65ec.jsonl。
