# Responses Agent 内核（2026-10-07）

## 2026-10-08 23:14：全新世界的尖顶双层住宅实验

用户授权新建世界，从空库存重新做一次建房实验，并增加尖顶、内饰、外摆和装饰。
当前记录为 `logs/house-decor-20261008-231202/run.json`；`current_attempt` 指向该目录。
旧实验 `logs/house-20261008-145412/` 仍为用户停止状态，世界、背包与建筑保留。

本轮使用新随机种子 `6706783447242231180`，原生 Minecraft 26.1.2 服务端
`127.0.0.1:25567`，生存模式、和平难度、自然昼夜天气。角色 `MineBotHouse`
自然出生于 `[6.5,113,1.5]`，客户端、服务端与 Agent 读取均确认空库存。
新 SQLite、明确的新 server/world identity 和独立 `game/baritone/` 缓存，没有沿用旧探索记录。
客户端 Fabric / Baritone 1.18.0，WSLg `DISPLAY=:0` 原窗口显示，应用已提交的右键转向修复。
使用官方 ChatGPT Auth、`gpt-6.1-sol`、effort=`medium`、Responses HTTP，未设总时长或总步骤上限。

目标为外墙外轮廓 10×10、两层实际可用、楼梯与入口畅通，完整坡面和屋脊的尖顶；
门窗、床、工作台、熔炉、储物和照明合理，室内有功能分区和装饰，室外有协调的外摆与景观。
模型根据真实环境自主决定选址、风格、材料、数量与探索目的地。使用新探索工具，物理寻路仍由 Baritone 执行。
完成后要求实际上下楼走访并核对真实结构与设施，不以工具启动或图纸代替建成。

已收到真实模型响应并成功读取状态、背包与环境。模型在樱花林山地开始采木，
首次只读服务端检查已确认背包有 8 个樱木原木、角色移动且满血。任务运行中，尚无建成结论。
启动后只读观察，不补给、传送、代建、自动注入继续或中途修改执行代码；明确失败如实记录。
首个 Gradle 启动脚本假定已有 `--gameDir`，在 Minecraft 启动前退出；修正为无参数时追加，
随后启动成功，实际 Baritone 日志已确认使用本实验新缓存路径。该准备错误保留在启动日志中。
观察提醒已切换到新实验，普通推进和模型等待保持安静。

### 23:18–23:22：采木收尾的真实拾取卡顿，尚未修复

用户发现角色不动后只读检查。`collect_resource(logs,16)` 已取得 24 根原木，
收尾 `collectBlocks` 动作 `4948892d-10eb-4842-8ed9-9792090929d2` 选中 9 格原木；
服务端逐格确认 9 格全为空气，终态库存只增加 8 根，现场还存在一根原木掉落物。
卡在拾取，不能误记为还有一格树枝没砍。角色在 `[-7.0302,116,-6.9818]` 静止，
同一动作出现 118 次 `CALC_FAILED`，最终 300120 ms 以 `deadline_exceeded` 返回。

源码显示收尾拾取将 Baritone FollowProcess 半径设为 0；过程保持 active 时，
反复路径失败没有结束动作，只等整体期限。精确位置不可达的原因仍需独立直接复现，
没有将它归为模型探索规划故障，也未修改运行中的代码。
Agent 随后自行读背包并成功制作 48 块木板、工作台和木镐，实验继续。
没有开发者取消、注入、补给、代建或重启。证据为本实验目录
`observed-collection-stall-diagnosis.json`，此次卡顿不因随后继续而算已修复。

### 2026-10-09 00:18–00:23：树顶最后一格反复重算路径，尚未修复

`collect_resource(logs,160)` 的 `collectBlocks` 动作
`1b4765fa-0153-4545-8eb1-de25c1714d0c` 选中 `[-89,70..75,-264]` 六格原木。
角色停在 `[-89.7000,70,-263.5110]`，原木总量停在 102，斧头耐久没有变化。
终态记录 2911 次 `CALC_STARTED`、2910 次 `CALC_FINISHED_NOW_EXECUTING`，
300169 ms 后返回 `timeout / deadline_exceeded`，该子动作库存增加 4 根。
服务端逐格确认 y70..74 已为空气，y75 仍为橡木原木；与前次全部已砍掉的拾取卡顿不同，
此次卡在树顶剩余方块的物理执行。站位、遮挡或瞄准的具体原因尚未独立直接复现。
反复重算但无实际进展仍只等总期限，是此次观察到的收尾缺陷。
Agent 自行读取库存、更新计划并导航返回住宅，实际位置已继续变化。
没有人工取消、注入、补给、施工、重启或执行代码变更。
证据：本实验目录 `observed-tree-last-block-stall.json`、
`observed-stall-0024-blocks.json` 和 `observed-stall-0022.png`。

### 2026-10-09：模型接口、代理与 SSE 对照，原超时尚未复现

用户要求先厘清环境与接口差异，不据字符数重做上下文。仅独立 API 实验，
没有向建房 Agent 注入消息、执行游戏工具、改配置、重启或更改执行代码。
Codex 桌面日志实际为 `wss://chatgpt.com/backend-api/codex/responses`；
MineBot 为新 SIWC Auth 的 `https://api.openai.com/v1/responses` HTTP/SSE。
两个 OAuth 注册使用同一账户邮箱；Windows 系统代理和 WSL HTTP/HTTPS 代理均为
`127.0.0.1:7897`，不据此假定不同域名的代理规则完全相同。

固定 `gpt-6.1-sol / medium`，服务端 usage 与完整 SSE 终态记录如下：

| 对照 | Input tokens | 耗时（秒） |
| --- | ---: | ---: |
| WSL / 新 Auth / 短请求 | 27 | 4.556 |
| WSL / Codex Auth / 短请求 | 27 | 4.252 |
| WSL / 新 Auth / 长记录 | 72,858 | 5.827 |
| WSL / Codex Auth / 长记录 | 72,858 | 5.347 |
| Windows / 新 Auth / 短请求 | 27 | 5.181 |
| Windows / 新 Auth / 长记录 | 72,858 | 5.456 |
| 生产 Registry + SDK SSE / 长记录 / 不触发压缩 | 72,858 | 7.018 |

长记录为真实实验至 seq1090 的工具结果和对话回放，使用仅报告下一步的离线工具，
不包含原请求加密推理和完整生产工具表，不能当作原超时请求的精确重放。
同一长记录设置 64k-token 原生自动压缩，新 Auth 32.001 秒、Codex Auth 29.569 秒，
都收到 `response.output_item.done` 的 compaction item；生产 SDK 启用压缩的一次为
51.141 秒。压缩真实可用并有额外等待，尚未证明它导致原来的 120 秒超时。
终态 `output=[]` 会使仅从终态统计的 compactions=0 漏计；不能据此宣称压缩未工作。
生产日志脱敏器也把 numeric token usage 屏蔽，无法从旧 trace 还原失败轮的实际 token 数。
截至检查时 129 个已结束请求中有四次流故障：一次 incomplete chunked read、三次事件等待超时；
本组对照全部完成，未复现这些故障，不能称已修好，也未通过调大超时掩盖它们。
证据：`logs/provider-latency-20261009/result.json` 及同目录逐事件时间记录。

随后只读核对 Mihomo 运行时：TUN 已启用，gVisor / auto-route，规则模式；
Windows 系统代理也启用，WSL mirrored / autoProxy，Agent 继承 HTTP/HTTPS_PROXY
指向 `127.0.0.1:7897`。控制器实际连接采样中，api.openai.com 的 HTTPS 代理入口
与 chatgpt.com 连接使用相同代理链；TUN 开启不能证明所有应用使用相同入口。

不带凭据的 `/v1/models` 握手对照抓到新的间歇性连接故障：WSL Python 3.13.13 /
OpenSSL 3.5.6 / httpx 0.28.1 多次 TCP 毫秒级接通后 TLS 握手达到本次 8 秒诊断上限；
同期 Windows httpx 1.595 秒收到预期的 401，WSL curl 另一次为 1.504 秒。
但 Windows curl 也有连接超时，后续 WSL 默认 TLS 请求自行恢复至 1.539 秒。
仅在独立探针限制 TLS 1.2 的三次请求为 1.848 / 1.903 / 1.790 秒；单独限制
X25519 的请求仍超时。这些样本不能证明 TLS 版本或 WSL 是根因，更不解释已收流后的故障。
证据：`logs/provider-latency-20261009/tls-proxy-controls.json` 保存最后一组交错对照。
没有修改代理、TLS、运行中 Agent 或执行代码；原 SSE 超时仍未精确复现或修复。

### 2026-10-09：SSE 心跳与连接复用修复（已接入建房 Agent）

已用真实 OpenAI SDK 和本地 SSE 服务复现两处运行时问题：
持续发送心跳的连接被额外的“模型事件等待”计时器误判为超时；
收到 terminal 后提前退出迭代，使两轮请求建立了两个 TCP/TLS 连接。
修复后 HTTP 收流由 HTTPX 的实际字节空闲超时负责，SDK 正常读到流结束，
相同两轮请求复用一个连接。静默断流和无 terminal 的 EOF 仍返回具体错误。
连接预算缩至 10 秒、写入/连接池预算最多 30/10 秒，读取预算保持原配置；
继续使用基础 SDK 原有的打开请求重试，没有新增重试层、TLS 降级或传输切换。
日志补充响应头到达耗时、具体传输异常和最后事件类型，允许非负整数 token usage，
按流事件统计 compaction；凭据内容仍脱敏。

真实新 Auth / gpt-6.1-sol / medium / 生产 Registry 和内核通过长记录原生异步：
73,077 input tokens 的首次调用为 16.180 秒，工具未完成时聊天为 4.081 秒，
原始 call_id 的迟到结果续接为 5.305 秒；调用只执行一次。这是离线延迟工具实验，
没有调用 Minecraft，也不能替代建房实验的验收。

生产工具工厂的 74 个模型可见注册工具与运行中 manifest 一致；
当前模式实际发送 73 个原生工具定义。用完整工具、克隆的任务/当前状态及长记录
做真实推理对照：压缩后的内核续轮为 11.789 秒，而直接 SDK 同一输入为 86.712 秒，
后者首事件为 78.498 秒。长记录冷请求也出现 SDK 186.648 秒、首事件 134.449 秒。
这些结果揭示接口/连接的长等待，不能把所有延迟归因于 Agent 上下文。
独立连接探针已确认：提前关闭 SSE 后下一轮重新握手；SDK 正常读完后下一轮没有 TLS 握手。
修复后用完整工具、克隆的任务/状态和真实生产提示词发起正常续轮（19,819 input tokens）：

| 路径 | 完成耗时（秒） | 首事件（秒） |
| --- | --- | --- |
| Agent 工厂 + 内核 | 8.133 / 8.306 / 7.192 | 3.833 / 2.982 / 3.070 |
| 相同请求直接 SDK | 7.173 / 6.613 | 3.511 / 2.844 |

五次均完成；首轮握手后，后四轮都没有 TCP/TLS 建连事件。模型自主选择工具，
没有强制 tool_choice 或微小输出模板；生成的游戏动作未执行。
长记录压缩请求（86,759 input tokens）在新内核完成于 61.183 秒，首事件 3.612 秒。
一次后续离线探针把多调用结果误当成单个 read_state 调用，断言退出，记录为探针错误，
不能算模型或 Body 失败。上述正常续轮独立补测已完成；间歇性网络故障仍需运行中观察。

定向接口回归 39 项、10 个 subtests / 24.28 秒通过，随后补充已接收 terminal 不被尾部断流撤销的回归；包括原生异步、取消、
静默断流、EOF、心跳、HTTP 连接复用、配置和 Auth；未跑历史全量。
修复先在独立 worktree 提交 `a00b441`，用户随后明确授权应用并只重启 Agent，
当前工作分支对应 `e5f4646`。旧 Agent 正常 `/quit`，复制一致的 SQLite 后用同一
world/server identity 和原任务/计划续跑；客户端、服务器、世界、背包和房屋保留。
续跑启动脚本首次误用非阻塞 stdin，读取线程提前退出；该次没有模型请求或 Body 动作。
修正为阻塞 FIFO 后，新的 `attempt-3-responses-resume` 通过一次授权 `/continue` 恢复原任务，
未替换目标或丢弃计划。启动错误、重启和取消原采集动作均记在根目录 `run.json`。

真实 Agent 已完成 8 次请求，平均 8.611 秒、中位 7.316 秒、
最长 19.933 秒，输入 11,867–16,231 tokens，
流故障 0 次；实际调用了采集并同时查询归档，继续推进房屋原任务。
这是截至此刻的实测窗口；建房仍运行，最终房屋验收未完成。
诊断证据在 `logs/provider-latency-20261009/`，运行后统计在当前 attempt 的
`model-latency-after-restart.json`；监控继续读取实际请求与游戏进展。

## 2026-10-08：模型选择探索目的地，Baritone 执行

新增 `observe_environment`，真实客户端的 `explore_for` 改为接受模型选定的坐标、方向或区域。
环境按方向提供生物群系、海拔、水面、地表位置、当前深度、难度、时间和搜索历史。
模型可以提出未知区域的目的地；程序不声称那里已经有目标，也不代替模型选择哪个地区更值得找。
工具与参考项目取舍详见 [探索设计](EXPLORATION.md)。

所有物理寻路仍调用上游 Baritone GoalNear/GoalXZ。沿途读取期间保留同一个导航动作，
发现目标后等待真实停止；区域按条带调查，预算结束后恢复进度，无需模型传查询游标。
已有 coverage 存储新增跨查询读取，地理历史不因加入新目标而消失；负面观察仍带目标、时间和半径。
旧 FakePlayer 探索路径保留。旧提示中“不能自己选择侦察坐标”的限制已移除。

独立角色 `ExploreProbe`、IPC 8773、虚拟显示 :105，使用原本的 disposable server。
没有启动模型、重启建房角色、给建房角色补给或替其施工。受控试验在远处的平台进行，
平台、铁矿、静止羊由开发者提供；不能称为真实模型在未知世界自主找羊成功。

| 真实定向调用 | 结果 |
|---|---|
| 向东推进 96 格 | 18.7 秒，净位移 95.05 格，14 次采样，路线完成且未发现目标 |
| 前往远坐标，沿途找铁矿 | 10.6 秒，在约 54.38 格处发现并停止，未走到原终点 |
| 前往远坐标，沿途找羊 | 13.3 秒，净位移 69.02 格后发现并停止 |
| 调查选定矩形 | 14.8 秒，13 次采样，条带路线完成 |
| 行走中取消 | 停止等待约 317 ms；后续坐标静止、无 pending 动作 |
| 自然起伏地形上的已观察目的地 | 从 Y=90 到 Y=87、水平约 31.94 格，真实路线完成 |

本轮定位并修复两处实际状态问题：客户端 `hasChunk` 会给尚未收到的区块提供空占位，
导致高度图出现假的 -64。环境与方块搜索改用不创建占位的实际 FULL chunk 查询，未知位置
返回 loaded=false；部分方块扫描返回 unloaded_boundary。另一次已抵达目标格但尚差一 tick
落地时被判为 `baritone_stopped_without_result`。对相同 `near:4403:101:4054:1` 的直接 Body
复现，修复前在 Y=101.024、on_ground=false 时失败；修复后等待真实落地，Y=101、on_ground=true
并返回 goal_reached。沿用动作期限和取消，没有增加导航算法或重试层。

证据：`logs/exploration-20261008/result.json`、`natural.json`、`landing-before.json`、
`landing-after.json`、`final-moving-block.json`。首次夹具未加载的失败和自然地形失败记录保留。
125 项相关 Python 回归通过；技能格式与依赖另作定向检查。Java 编译与真实 Fabric/Mixin 加载通过。
未运行模型搜索策略验收，也没有恢复用户停止的建房实验。

Body 中间执行状态的逐工具设计见 [BODY_EXECUTION_STATE.md](BODY_EXECUTION_STATE.md)
（2026-10-08，设计稿，尚未实现；不改变现有模型调用或反射策略）。

## 2026-10-08：合成数量自动向上取整

用户要求 `craft_item.count` 表示至少新制作多少个物品，由公共配方规划器
按单次产量向上取整：门请求 1 个制作 3 个，请求 4 个制作 6 个。
材料分配、输出空间和分批按实际产量计算，结果分别返回 `requested_count`
与 `crafted_count`；Agent 工具说明已同步。跨堆叠边界的门请求 64 个会分批
制作 63+3 个。内部精确合成原语仍接收规划器算好的真实数量。

库存事务定向回归 40 项、35 个 subtest 通过，客户端合成回归 2 项通过。
覆盖取整、已整除数量、跨堆叠分批、材料不足和取整后的输出空间；没有跑全量。
当前第七次建房会话没有重启或热更新，这一补丁尚未加载到该运行进程。

## 2026-10-08 21:02：medium 建房续跑

用户要求将 MineBot 的 reasoning effort 从 `xhigh` 改为 `medium` 并重启续跑。
第六次会话经 `/quit` 正常结束（`session_terminal=quit`、身体 idle）；
第七次会话使用相同官方 Auth、`gpt-6.1-sol` 和 Responses HTTP，私有配置及
本次启动覆盖值均为 `medium`。原客户端、服务器、世界、背包和建筑保留，
持久化 SQLite 在退出后复制；启动输入交接已有施工事实并要求读取当前现场。
没有补给或代建，仍无总时间/步骤上限。新会话已收到真实模型响应并成功读取
状态、背包和现场，继续行动；这次用户授权的重启也计入实验介入。

旧会话屋顶六段在各自完成时合计验证 100/100 格，已放置工作台和熔炉；
其后导航、设施、照明、门窗及整栋结构和楼梯走访仍需最终验收。此处没有
建房成功结论。实际状态以 `run.json.current_attempt` 指向的
`logs/house-20261008-145412/attempt-7-medium/` 为准，监控已同步。

## 2026-10-08 19:48：第六次建房续跑

用户授权修复后重启，现已启动第六次会话：官方 ChatGPT Auth、
`gpt-6.1-sol`、Responses HTTP，应用执行生命周期修复 `36160b1`
和窗口修复 `237cbc2`。沿用原世界与背包，目标仍为 10×10 双层可用小屋，
无总时限、无总步骤上限；没有补给或代建。本次是有历史人工介入的续跑，
不能计为从零无干预的一次成功。此前第五次失败记录保留。
实际状态以 `logs/house-20261008-145412/run.json` 和其 `current_attempt`
目录为准；没有建房完成结论。观察提醒恢复，正常等待与普通推进保持安静。

## 2026-10-08：Agent 客户端自由鼠标

真实 WSLg 客户端默认保留桌面鼠标，不再在进入世界、关闭菜单时锁住或
居中移动系统指针。游戏区域按住鼠标右键可转动视角，松开恢复桌面指针；
闲置鼠标移动、点击和滚轮不触发挖掘、使用物品或切换槽位，菜单仍可操作。
打开菜单或切出窗口也会释放右键转向。保留 Minecraft 内部的鼠标逻辑状态，
避免关闭其原生挖掘循环。Baritone 与 Body 继续直接控制客户端。
手动游玩可在启动前设置 `MINEBOT_CLIENT_FREE_CURSOR=false`。

晚间用户要求停止建房 Agent 后手动查看。第七次会话已停止，自动监控关闭；
客户端单独重启加载右键转向补丁，服务器、世界和角色背包保留，未续跑 Agent。
`/quit` 写入 FIFO 后未出现退出回执，随后 SIGINT 退出；这一停止方式保存在
原实验 `run.json`，没有将本次建房标为完成。

右键转向沿用原版鼠标增量和灵敏度，始终保持桌面指针自由；最初尝试切换
GLFW 捕获模式导致用户报告低头转圈，已撤掉。独立 `MouseLookProbe` 实测
两次拖动各转约 7.5°，按住不动、松开后的移动和菜单开关均保持视角稳定，
背包未变化且没有使用物品。Java 编译和实际 Mixin 加载通过；记录在
`logs/mouse-look-20261008/mouse-look-proof.json`，主窗口已加载相同补丁。

真实窗口验证最大化、还原、标题栏拖动、最小化、恢复和关闭；操作期间角色
位置、朝向和背包没有变化。直接 Body 转向和 Baritone 步行到目标通过。
Java 编译和实际 Fabric/Mixin 加载通过。窗口验证与客户端重启属于开发者
介入，保存在第六次续跑目录；未补给或代建。

## 2026-10-08：建房故障的四处执行修复

第五次建房会话在 18:11:25 退出，房屋未完成；复核地板 99/100 格，
上层、墙体和屋顶尚未建成。根目录 `run.json` 已记录失败，观察提醒已停用。
本轮没有重启建房 Agent、给其补料或代其施工；世界和背包保留。

针对 `attempt-5-native-window/` 的真实调用与客户端日志，修复公共路径：

- 事务派发显式传递执行预算，终态等待使用同一预算加 5 秒收尾余量；
  子动作受当前工具剩余预算约束。覆盖库存、菜单、交互、导航、战斗、
  方块、采集和客户端原生任务的派发点，不靠各处独立短等待截断工作。
  合成按实际配方重复次数分配批次预算，移除整个批量任务固定 45 秒的上限，
  保留每批有限期限和显式取消；失败回执保留当前批次已产生的数量。
- Builder/clear 停止后逐格核对实际方块，不以 `seenActive` 判定是否成功。
  已经满足目标的重复请求能够成功；停止但方块不匹配仍返回失败。
- 公共工具入口和导航/战斗入口不再由全局停滞计数否决动作或覆写回执。
  只读观察不累计物理停滞；保留单写者、取消和过期 generation 的控制。
- HTTP 事件等待超时、无终态断流和连接中断进入原有会话恢复。
  清理旧请求与旧事件，保留历史、目标、原始 `call_id`、执行中的工具和结果。
  连续短重试用尽后每 60 秒再试，等待期间取消、退出和消息入口仍可响应；
  不自动重放身体动作。明确的认证、配置或不支持能力错误仍按原规则报告。

独立角色 `LifecycleProbe` / IPC 8770 在同一 disposable 服务端的
`x/z=1300` 自备测试夹具，实际结果如下：

| 任务 | 结果 |
| --- | --- |
| 64 原木合成 256 木板 | 17.983 秒成功；服务端确认四组 64 木板 |
| 建造三格石条，再原样重复 | 两次成功，分别 1.311 / 1.300 秒；服务端逐格确认 |
| 清理该石条，再重复清理 | 两次成功，分别 2.474 / 1.299 秒 |
| 无金块时请求金块建筑，期限 2 秒 | 2.266 秒返回 `deadline_exceeded`，后续状态读取正常 |
| 行走中显式取消 | 111 毫秒返回取消终态；`pathing=false`、无活动过程 |

官方 ChatGPT Auth / `gpt-6.1-sol` / 现有 HTTP 内核的真实模型探针也通过：
在原生异步工具启动后故意挂起事件流，触发 `ResponsesTransportError`；
此时一个工具仍在运行。恢复后原 `call_id` 只执行一次，迟到回执正常交付，
模型返回 `PROBE_OK`。该工具是合成计数探针，没有驱动 Minecraft。

证据：`logs/execution-lifecycle-fix-20261008/body-result.json`、
`model-recovery-result.json` 和同目录短复现脚本。探针准备错误单独记录，
不算产品缺陷或无干预建房成功。独立测试客户端结束后关闭。
默认回归 87 项通过（28.34 秒）；受影响事务、会话、取消和回执的定向回归通过；
客户端 Java 编译通过。没有运行历史全量，也没有新增 Gate 或测试平台。
流式消费失败不能靠 SDK 自动重放，依据
[官方 Python SDK 错误说明](https://developers.openai.com/api/reference/python#handling-errors)。

## 2026-10-08：直接显示真实客户端

默认在 WSLg `DISPLAY=:0` 启动真实 Minecraft 客户端，窗口直接出现在 Windows
桌面，不经过抓帧、编码或播放器。入口为 `tools/open-client.ps1`；检测到 IPC
端口已有客户端时不会再启动第二个。世界、角色和 Body IPC 路径不变。

此前实验强制使用 Xvfb `:99`，才需要额外推流。原 `MineBot LIVE` 播放器在
重连后确实出现解码停滞；仅看到发送端帧号增长不能证明用户画面正常。
在模型和 Body 均空闲时，10 秒原始客户端帧的像素校验均不同，客户端本身仍在渲染。
独立 JPEG/WPF 窗口对照虽能恢复，但用户选择直接显示客户端，未将该替代播放器纳入主线。
旧抓帧入口仅作为无桌面环境的备用，停止继续扩建。

本次已从 Windows 确认 `msrdc` 原生窗口标题为
`Minecraft* 26.1.2 - Multiplayer (3rd-party Server) (Ubuntu-24.04)`，
且客户端连接原测试世界。旧播放器、抓帧进程和 Xvfb 已停止。
证据：`logs/house-20261008-145412/attempt-4-mining-fix/client-wslg.log`，
以及第三次实验的 `clear-area-control-fault/`。建房续跑保留世界和背包，
开发者补清地基及切换显示造成的中断如实记在实验根目录 `run.json`。

## 2026-10-08：官方 ChatGPT 登录与原生异步实测通过

现有 Python Responses HTTP 内核已接通官方 ChatGPT 套餐接口，直接请求
`https://api.openai.com/v1/responses`，模型为 `gpt-6.1-sol`。真实登录、
模型发现、普通推理和现有内核的原生异步往返均已通过。
原 API key 和显式 legacy SDK 路径保留。

```bash
python -m pip install -e '.[test]'
minebot-auth login
minebot-auth status
minebot-auth models
export MINEBOT_LLM_AUTH=chatgpt
export MINEBOT_LLM_MODEL=gpt-6.1-sol
unset MINEBOT_LLM_BASE_URL OPENAI_BASE_URL
```

也可用 `python -m minebot.app.chatgpt_auth` 代替 `minebot-auth`。
`login --no-browser --port 14650` 打印授权链接并等待本人浏览器授权。
本机外部浏览器此前持续显示 `Operation timed out`；同一机器改用 Codex
内置浏览器后，新接口和 Codex 备选都登录成功。Windows 本机回调监听
此前也已确认可达，因此没有证据认定是沙盒隔离。具体浏览器会话或网络
差异尚未定位；移除 host ID 单独在外部浏览器重试仍失败。

首次成功登录使用原样官方 [DevKit](https://developers.openai.com/cookbook/articles/sign-in-with-chatgpt)
`@siwc/local`，源码固定为 `0a36fefeb913055c8c7a1a29b63d82b96b2e841a`，
在 Windows / Node 24.19.0 运行，使用默认 `sendHostId=false` 与 DPAPI
CurrentUser 存储。SDK 完成授权、身份验证、模型发现和普通推理后，
凭证通过进程 stdin 转入 MineBot 私有存储，删除 SDK 对照的凭证副本；
现由 Python 单独负责续期。Python 授权链接也与该 SDK 默认保持一致，
保留本地安装 ID，返回登录使用已签发 client ID。

默认凭证文件为 `~/.config/minebot/chatgpt.json`，可用
`MINEBOT_CHATGPT_AUTH_FILE` 指定仓库外路径；Unix 原子写入且权限为 0600。
请求前自动续期，跨进程串行处理 refresh token 轮换。`logout` 尝试远程
撤销并清理本地凭证，远程失败明确报告未确认撤销。模型和工具看不到凭证。
续期已有行为回归，本次短实验尚未跨过真实令牌到期。

生产适配继续使用原 ToolRegistry/Body：工具放进 `minebot` namespace，
保留原生 `async: true` 和原始 `call_id`。HTTP 发送完整 input、
`store=false`、`stream=true` 和加密 reasoning；不发送套餐通道不支持的
采样参数/输出上限，不允许向自定义地址发送 OAuth 凭证。

真实异步实验首次失败：服务端通过 `response.output_item.done` 发出完整
工具调用，但 `response.completed.output` 为空。内核用末尾空列表覆盖了
已接收历史，迟到结果被拒绝为 `No tool call found for function call output`。
修复为保留流式完成项、按 ID 合并终态版本；已有回归增加该真实事件形状。
修复后官方 API 的完整实验约 18 秒通过：工具只启动一次、原生异步为真、
等待时回答聊天、真实迟到 receipt 在原 `call_id` 上交付一次。相关
17 项测试通过（15.83 秒），没有重跑全仓库。

复现实验：

```bash
python -m tests.probes.official_chatgpt_async --output logs/chatgpt-native-async.jsonl
```

本轮证据在 `logs/chatgpt-login-20261008/`：`native-async-first-failed.jsonl`
保留首次失败，`stream-shapes.jsonl` 记录实际事件形状，
`native-async-fixed.jsonl` 记录修复后成功；之前的授权失败另保留在
`authorization-failure.json`。Codex OAuth 备选仅完成独立依赖实验，
生产使用上述新官方接口。

官方模式默认请求 64,000 token 阈值的服务端内联 compaction；通过
`MINEBOT_RESPONSES_COMPACT_THRESHOLD=0` 关闭或设另一阈值。只按内联
压缩规则保留最新加密 compaction 项及之后历史，未返回结果的 native call
保留，上一动作运行时不请求压缩。它与 64,000 **字符**观察详情软预算不同。
使用官方自动 prompt caching，终态记录实际 usage、模型与请求时间。
本次短实验未触发 compaction，cached tokens 为 0；实现和接口接通不能
当作这两项的真实效果验证。建房状态单独记录在下一节。

## 2026-10-08：不限时双层小屋第三次启动记录（历史）

第三次会话 `attempt-3-official/` 已于 16:39 启动，使用新官方 ChatGPT
Auth、`gpt-6.1-sol` 和相同生产 Responses HTTP 内核。开跑前再次确认
原出生点、库存为空；前两次失败没有发生物理动作，世界和旧记录保留。
真实异步 `collect_resource` 已执行，客户端状态已观测到 `acacia_log: 10`，
角色离开出生点。桌面观察窗口 `MineBot - house LIVE` 已恢复，观察提醒启用。
没有总时限或总步骤上限，当时尚无房屋完成结果。随后续跑到第五次并失败，
当前结果和本轮修复见本文开头；以下保留最初启动与失败。
Auth/API key 切换兼容检查按用户要求留待本轮测试之后。

用户已明确启动 [TESTING.md](TESTING.md) 的空库存建房任务：外轮廓 10×10、
双层实际可用、内部设施合理；材料与物品数量自主决定，无总时限。
沿用生产 Responses HTTP 内核和 `gpt-6.1-sol`，交互会话 `max_steps=None`。
独立新世界种子 `20261008`，生存、和平难度、正常昼夜和随机刻；
出生点 `(-6.5,66,6.5)`，服务端确认库存为空、经验为零，客户端生命和饱食均满。
出生附近有天然橡树和露石，没有赠送物品、预建场地或矿脉坐标。
旧实验世界及旧 Baritone 地形缓存均保留，客户端为本轮重新建立缓存。

尚无建成结果。记录根目录为 `logs/house-20261008-145412/`，`run.json`
中的 `current_attempt` 指向当前会话目录；目标、开局状态及每次失败均保留。
桌面 `MineBot - house LIVE` 已实际核对有真实客户端画面。
最初启动命令错误解析了虚拟环境解释器的符号链接，报缺少 `nbtlib`；
当时没有 Agent/Body 调用，错误保存在 `startup-interpreter-error.log`。
纠正命令后，第一会话成功读取状态、库存、近处方块与实体，但异步结果
提交遭端点 HTTP 400：`Async tools are not supported with gpt-6.1-sol.`。
该会话退出，未发生物理动作。随后原生短探针直连/代理分别连接/读取超时。
第二会话 `attempt-2/` 使用原端点、模型、内核重新开始；开跑前再次确认
空库存和身体空闲，随后收到 `response.failed`：
`Upstream service temporarily unavailable`，会话也已退出。服务端复核
角色仍在出生点、库存为空，两次都未发生物理动作。这轮是启动失败，
当时没有正在施工的 Agent，观察提醒停用，客户端和新世界保留。
第一会话的失败不覆盖、不并入成功，期间没有补物资或修改执行代码。
用户随后提出接入官方 ChatGPT OAuth；接通后已按上述第三次会话独立恢复，
最终仍按实物验收。


## 2026-10-08：客户端动作朝向已实现

行走/探索使用 Baritone 路径朝向；可见镜头在原生移动与发包朝向处理后
按最短角度转动，跨 ±180 不绕远路。挖掘、放置和近战/弓弩的精确目标
绕过行走平滑。地面动作临时应用朝向设置，完成、失败、取消或断线恢复；
Elytra 保留原生设置。近处跟随看已选对象，路径和视线绑定同一实体；
菜单看实际方块；显式看向和空闲视线保持。实现见 [ATTENTION.md](ATTENTION.md)。

直接调用 Body 的朝向场景 25.491 秒通过：±180 行走、绕墙转弯、采集高处
原木、指定对象移动后的跟随/注视、箱子转移、显式看向与移动中取消。
54 个有效移动样本均处于行进方向 45° 内；每个采样动作结束后四个原
朝向设置恢复。这个样本支持当前可见行走效果，不代表所有地形都只向前走。
另跑现有近战/弓/弩、菜单合成/转移/熔炼和真实双客户端交接，物理结果通过。

交接首次失败：两颗钻石丢出后被发送者重新捡回，接收者库存为空。
身体在完成投掷后增加原生 GoalRunAway 的短距离让位，离开重叠拾取区，
然后看回接收者。相同工具复验，接收者实际得到两颗钻石；满背包时返回
receiver_pickup_unconfirmed，未报成功。原失败 `handoff_tools.json` 保留，
修复后是 `handoff-retry.json`，不覆盖失败报告。另一次跨堆叠交接
80 颗钻石，2.281 秒完成：服务端接收者 80、发送者 16，记录在
`handoff-80.json`。

证据根目录：`logs/attention-20261008-141829/`；`result.json` 为朝向场景，
`combat_tools.json`、`menus.json`、`handoff-retry.json` 为对应物理回归。
Java 编译、实际 Mixin 加载和 72 项客户端接口/协议回归通过。
没有重跑无关全量或模型长任务。本轮未重飞 Elytra。

启动前，[TESTING.md](TESTING.md) 的任务已按用户要求改为：空库存开始，
建成外轮廓 10×10 的双层小屋，内部设施合理、两层实际可用。无物品数量
配额，无总时限，资源和施工由 Agent 自行安排；限定和平难度与自然开局，
无赠送物品。实际启动状态见本文件上节。


## 2026-10-08：连续采集与大资源体量

晚间建房观察暴露了遗漏：`collect_resource(raw_iron, count=2)` 实际只走
MineProcess 的数量终态，没有矿簇收尾。现记录原生采矿最后正在挖的资源
坐标，达到数量后探查该位置相连的余矿；能在剩余收尾余量内完成的继续
采集，超出则按需求返回。矿石收尾复用上游 GoalComposite/GoalTwoBlocks
寻路挖掘和原有拾取，不重新使用曾原地挥镐的 Builder 清矿路径。树木仍
使用已有 Builder 路径；收尾余量按剩余时间计算，不再固定为最多十块。

独立、供给铁镐的测试角色在远处测试台实测：同一簇 16 铁矿请求 2，旧逻辑
入包 2、剩 14；修后生产 `collect_resource` 入包 16、余矿为零，另一簇不动。
256 格石体请求 2，只入包 2，远端石头保留；矿簇动作取消至终态 164 毫秒，
寻路与所有过程均已停止。首次石体测试把石制支撑台也作为矿体，台下为空，
MineProcess 连续报告无安全路径；操作员停止独立测试，改用黑曜石支撑后通过，
失败记录保留。接口回归 66 项及 5 subtests 通过，客户端编译通过。证据：
`logs/ore-cluster-finish-20261008-2140/result.json`。当前建房角色、客户端和
会话未重启，也没有应用这次补丁；测试角色和测试供给均与建房角色分开。

`collect_resource` 在 disposable 真实客户端上复用原生 Baritone 数量采矿，
不再逐块搜索、接近、挖掘和拾取。小树先读取相连树干/枝干，用上游
Builder 清理选中的原木，再拾取；下一棵在本棵完成后才开始。收尾是偏好，
不是“连通就必须清空”：只探查本次所需数量加少量收尾余量，余量随剩余
时间缩小；超出时使用原生 MineProcess 按库存目标执行，不报树体过大。
没有遍历整片森林或整座山。未授权 disposable mutation 的路径沿用原有事务。

同一生产 `collect_resource` 入口的定向实验 87.589 秒通过，无模型参与：
两棵各 8 根的小树先后完成、实际净增 16（请求净增 9），第三棵未动；
72 格石体净增 40 圆石后停止且仍留石头；12 铁矿实际入包 12 粗铁；
80 根、2×2×20 的雨林木场景仅净增所需 8 根，上端仍保留；采集取消至
终态 103 毫秒，最终 pathing/any_process_active 均为 false。场地明确加载，
提供铁斧、铁镐和垫脚泥土；库存、目标方块由服务端独立核对。这验证了
上述场景，不将其写成自然森林/所有巨树或完整长任务均已通过。

本轮失败保留：早期测试台未实际完整建成，原木掉到台下；第一次还暴露
“库存达到最低值掩盖本棵拾取失败”，已改为采集终态也必须成功。随后在
正确加载的石体上，masked Builder 挖掉约 27 块后原地挥镐、保持 active，
没有继续移动；操作员停止该失败。石头/矿石改走现有上游 MineProcess，
修后同场景 29.373 秒净增 40 且真实移动。没有声称修复上游 Builder 的
所有清除行为，也没有改 Agent 规划或加入另一个无进展 Gate。

最终报告：`logs/collection-native-demand-20261008-135519/result.json`；
失败现场：`logs/collection-continuity-loaded-20261008-134756/`，更早场景
失败分别在 `collection-continuity-20261008-133839/` 和
`collection-continuity-retry-20261008-134504/`。相关行为回归 182 项、
14 个 subtests 通过，Java 编译及实际加载通过；未跑无关全量。
朝向见 [ATTENTION.md](ATTENTION.md)，仅设计，尚未应用。


## 2026-10-08 可见长任务：三项均未完整通过

用户授权后，使用生产 Responses HTTP 内核、gpt-6.1-sol/xhigh 和同一真实
Fabric/Baritone 客户端，实际执行三项任务。实时观察窗口显示该客户端的
画面；任务成功由服务端库存、方块和位置核对，不以模型接受命令为完成。

| 任务 | 上限／实际运行 | 实际结果 | 未完成项或具体故障 |
|---|---|---|---|
| 自然森林资源生产 | 30 分钟／约 30 分钟 | 32 原木、164 圆石、铁镐、32 火把、工作台、1 熔炉、16 粗铁 | 未炼成 16 铁锭，未补齐追加的 4 熔炉，离起点约 48 格 |
| 供料营地建造 | 45 分钟／约 45 分钟 | 大部分木屋和设施、两个水源、24/32 株小麦 | 房屋 8 处不符目标，余下 8 株未种；建造超时、站位失败和支撑块清理拒绝均保留 |
| 三点跨区运输 | 60 分钟／约 11 分钟提前失败 | 到达煤炭点，取走 128 煤炭，立好第一根四格路标 | 第二段导航发生 Body IPC 超时，Agent 退出码 8；基地箱仍为空 |

资源任务遇到升级工具缺木后，自行回地面采木再继续；营地任务自行拆分
施工、离开占据的屋顶缺口后补板、调整水坑附近的耕作站位。普通聊天与
追加要求实际送达，未取消原工作；短暂模型连接错误后也出现了自动恢复。
这些局部行为不等于完整长任务成功。

营地场景遗漏了旧实验留下的 `random_tick_speed=0`，运行中恢复为 3，
属于有环境干预的失败实验。脚本以后明确设置正常随机刻；湿润耕地按
`moisture>0` 判断，修正了首次验证器额外要求最大值 7 的问题。原失败
报告不改写，未种满和房屋缺失独立足以判定失败。施工支撑被旧合法性
检查以 `break_denied:player_structure_risk` 拒绝，未扩建世界保护工程。

运输首次准备场景时，新区块生成导致服务器落后约 21 秒、客户端超时
掉线，尚未启动 Agent；该失败单独保留。预生成目的地附近区块并重连后
才进行上表的实际运输。运输中还暴露了请求 300 秒却被外层工具的
120 秒执行预算截断的问题；本轮未更改该预算。

运输退出的直接原因是 Body IPC，不是模型服务拒绝。相同起点直接调用
`near:2600:94:2240:720`、60 秒期限，状态读取再次超过 10 秒。线程快照
定位到 `MineBotClientBody.state → estimatedTicksToGoal → GoalNear.heuristic`
占据客户端主线程。仅删除状态采样中的非必要耗时估算，没有改导航算法。

重新编译并加载后，同一导航约 15 秒到达，状态读取 24–40 毫秒。该次
取消发在完成之后，取消分项未通过，原报告保留。另一次在实际移动中
取消，状态读取 5 毫秒、取消至终态 144 毫秒，客户端报告进程已停止，
随后一秒位置变化约 0.05 格。此为直接 Body 复验；没有重跑完整运输，
运输后的模型取消用例也未执行，不能据此把原长任务改成通过。

证据根目录：`logs/visible-agent-20261008-111455/`。`resources/`、`camp/`
保存原任务结果；`logistics/` 保存场景启动失败；`transport-retry/logistics/`
保存实际运输、线程快照、修复前后直接复现和移动中取消结果。
`summary.json` 汇总，`post-run-body-state.json` 确认最终客户端空闲。
本轮只运行这些真实任务、定向复现、Java 编译和脚本检查，未运行无关全量。

## 2026-10-08 第二轮：小幅上下文优化与异步复验通过

用户授权小幅优化上下文，并继续复测原生异步。本轮复习 Issue #1 P01–P40；只改善已有任务上下文和 HTTP 请求，不新增长期记忆平台、可执行计划或 30 分钟模型运行。

上下文沿用现有目标、短计划、每轮 Body 状态和工具观测归档。原生 input 已包含聊天，因此 instructions 不再重复最近八条消息；短计划提示保留玩家追加要求、坐标、具体障碍和改策略理由。明确区分新读取的身体状态与历史观察。

HTTP input 超过已有 64,000 字符软阈值时，仅缩减最近 12 个结果之前、已有可查询完整归档的成功大观察：保留真实 success/reason、traceRef、observationHandle 和原始 call_id，详细 summary 按需通过观测工具读取。玩家原文、reasoning、调用、失败、近期结果不裁剪；原始历史不修改。没有归档或可缩减内容时允许超过软阈值，不为凑预算破坏调用/结果关系。此处不是完整上下文窗口压缩，原生 compaction 尚未通过当前端点实测，未启用。

真实 API 验证：gpt-6.1-sol 使用生产 provider 配置的 xhigh 和生产 HTTP 连接，载入合成的归档扫描历史；请求视图从 113,033 字符缩减至 3,073 字符，模型仍准确返回玩家指定的 (10,70,20) 与最新失败 target_unreachable。这里只验证上下文处理和 API 续接，不是 Minecraft 行动或长任务成绩。

原生异步已重新完整跑通两次：均通过生产 ResponsesKernel 调用真实 API，实际运行一个等待聊天结束才返回计数的异步 Python 工具；实际调用为 async=true，工具未完成时回复 ACK，随后在原始 call_id 上提交结果并回复 COUNT=3，工具各只执行一次。一次 low/512/parallel_tool_calls=false 耗时 9.413 秒；另一次使用现有生产配置 xhigh、不手工关闭并行调用，耗时 11.318 秒。没有修改生产模型、增加模拟异步或自动降级。

本轮较早的 HTTP 400、连接失败和超时仍保留。请求差异对照出现有成功、拒绝和无原生异步标记的结果，不能据此归因于单一 strict/tool_choice/parallel 设置。Astra 对照出现返回模型标签与原生错误中的 Luna 名称不同，用户已说明存在降级映射并指示不继续该项；后续只验证 gpt-6.1-sol。两次成功证明当前链路可用，不抹除端点失败。

新增两个上下文边界回归；直接相关集合 90 项 / 19.06 秒通过，覆盖原文、失败、调用与结果保留、归档查回引用及原始历史不改。没有运行无关全量。报告：`logs/context-async-review-20261008/sol-production.json`、`sol-configured.json`、`context-configured.json`；同目录保留失败与请求差异对照。Body 未修改，长任务尚未测试。

## 2026-10-08：Body 连接和原生异步复查

按 Issue #1 P01–P40 检查现有运行链。本次只补 Agent 入口和异步续接的具体缺口；上下文管理留待调研讨论，不启动 30 分钟模型运行。

原有 44 个基础工具继续复用 ToolRegistry。真实客户端增加 `mine_resource`、`build_structure`、`clear_area`、`farm_area`、`elytra_travel`、`read_body_processes`、`build_schematic` 七个薄入口，不暴露任意命令。批量变更复用客户端已有 disposable-world 标志；Java 后端不注册这些客户端入口。文件蓝图限定本次验证过的 Sponge v2、零 Offset；使用 nbtlib 读取，并在施工结束后通过 Body 批量核对每个实心/空气格。区域建造和清除也核对目标格。

工具分派按声明的 JSON Schema 校验参数；错误字段、类型、数量和缺参在执行前返回具体 `invalid_tool_input`，模型可修正。`finish_task` 等到动作结果实际提交给模型后才可关闭任务。HTTP 流中断保留完整调用项、原始 call_id、正在执行的工具和未提交用户消息；恢复不重放物理动作，也不重复添加已提交的工具结果。

生产分派实验使用 `--through-agent`，经过与 Responses 相同的调用/投影入口，`model_calls=0`；它证明连接，不冒充模型任务。批量采矿净增 24 根原木；72 格区域建造/清除；80 株农事；Sponge 房屋 100 格核对；250 格飞行均有真实结果。飞行首次进入飞行后摔死、最终超时；相同参数直接调用和再次 Agent 分派均成功，首次摔死原因未定位，失败报告保留。

2026-10-08 原生协议复验 **未通过**：gpt-6.1-sol / SDK 2.36.0 / 当前私有端点 / HTTP SSE，先收到 async=true 调用、待完成期间收到 ACK，但迟到结果请求返回 HTTP 400：`Async tools are not supported with gpt-6.1-sol.`。此前 2026-10-07 的成功记录仍作为历史事实；不能用它覆盖本次拒绝，也不自动换模型、换内核或启动 provider 平台。直连与代理请求的连接失败同样保留。报告在 `logs/agent-body-review-20261008/protocol*.json`。

相关接口回归 180 项和 116 个 subtests 通过；包含流中断后原始结果续接、用户消息保留、错误参数不执行和完成前等待结果。真实实验见 `logs/body-complex-20261007/` 的 2026-10-08 记录，可复现：

```bash
python -m tests.e2e_real_client_body_complex --disposable-local-server --case <任务> --through-agent
```

## 用户行为与目标

用户已授权替换普通 Agents SDK Runner 的模型—工具循环，采用基础 OpenAI SDK 的 Responses 原生异步工具；WebSocket steering 是次要能力。机器人执行长动作时可以聊天、接收追加要求；真实动作结果自动进入后续模型请求；明确取消真正停止身体。每次模型请求使用最新目标、任务状态、动作结果和身体状态。

理念来自 [Issue #1](https://github.com/qWaitCrypto/MineBot/issues/1) 的完整 P01–P40。本计划不把已有契约、治理或进度机制当作必须保留的产品理念。

2026-10-07 用户进一步明确：原生异步工具是主线，原生 response.steer 是次要能力。随后用户明确不继续 WebSocket 工作：当前目标只要求已实测的 HTTP 原生异步路径，WebSocket 代码保留，未经真实验证，不再作为当前待办。

## 范围与接口

- 复用 ToolRegistry、真实客户端 Body、工具结果投影和物理取消。
- 长动作独立于模型协程存活；按原始 call_id 保留 pending 调用，终态以真实 function_call_output 返回。模型轮次结束和普通聊天不取消身体。
- 同一身体只有一个写动作；读取和取消不能排在长写动作后面。
- HTTP 路径在当前模型响应结束后提交排队的新消息，身体工具独立运行。WebSocket 路径在活跃响应期间使用 response.steer；accepted、pending、steered 和自动后继响应遵循原生协议，避免重复消息。
- 工具结果在合法续接点按原始 call_id 提交。HTTP 使用完整 input/output 历史、store=false 与 reasoning.encrypted_content；WebSocket 引用最新 response_id。每次请求重新提供工具定义和最新 instructions。
- 只固定一个真实可用的模型、SDK、端点组合。先核对现有端点；不自动退回同步 Runner 并声称异步已完成。
- 不移植 Pi，不建立 provider 平台、Gate、世界保护工程或长期记忆平台；不重写整个 Brain/Body。

## 实施切片与提交

1. 核实模型与协议组合，记录实际支持或具体拒绝；调整最小依赖。
2. 实现薄 Responses 循环：完整调用分派、pending 结果、单写约束、继续响应与实时上下文；接口定向回归。
3. 接通生产入口、聊天 steering、追加要求和明确取消；移除这条路径对旧模型轮次/进度终止的依赖。
4. 运行下面的真实实验，记录实际结果和具体失败原因，更新运行命令与状态。

每个有意义的切片单独提交。只跑对应接口回归与真实实验；不跑无关全量，不用源码形状测试锁实现。

## 验收与停止条件

在现有 disposable 本地世界与固定客户端组合运行真实模型任务：“采集原木并交给玩家”。采集期间发送普通聊天和追加的“交接后回到出发点”；观察原动作继续、追加要求进入后续计划、真实库存/交接/位置确认结果。另开一次长动作，明确取消后独立读取身体确实空闲，旧动作停止。

必要定向回归覆盖原始 call_id 的迟到结果、重复事件、聊天不取消动作、写动作互斥与取消。真实实验失败则写明任务、初始条件、实际结果及具体原因；接口回归不能替代真实任务成功。

达到这些行为后停止扩张。若模型端点不支持原生协议，保留已实现的切片并明确缺少的配置，不将兼容模拟包装成原生支持。

## 当前进度（用户移除 WebSocket 后，本次目标已完成）

- [x] 目标已创建，当前执行计划已切换。
- [x] 固定 gpt-6.1-sol / OpenAI SDK 2.36.0 / 现有端点的 HTTP 原生异步组合，普通请求和原生 SSE 均已实测。
- WebSocket steering：用户已明确移出当前工作范围；保留已有代码，不继续端点验证。
- [x] Responses 内核与定向接口验证。
- [x] 生产入口、消息 steering、明确取消、短计划和最新上下文接通；会话回归覆盖 SQLite 消息队列与同步执行线程。
- [x] 真实模型 + Minecraft 连续任务和物理取消验收，83.779 秒通过。

## 启用与验证

配置为 `openai_responses` 时默认使用已通过真实任务的新内核；`openai_chat` 和 `litellm` 继续使用 SDK 路径。可显式指定 `MINEBOT_AGENT_KERNEL=sdk` 使用旧循环，不在 API 报错后自动切换。

已验证的配置是：

```dotenv
MINEBOT_AGENT_KERNEL=responses
MINEBOT_LLM_KIND=openai_responses
MINEBOT_LLM_MODEL=gpt-6.1-sol
MINEBOT_RESPONSES_TRANSPORT=http
```

沿用现有端点和密钥配置；需要指定网络代理时使用 `MINEBOT_LLM_PROXY`。新内核默认选 HTTP/SSE，可显式设置 MINEBOT_RESPONSES_TRANSPORT=websocket；使用基础 SDK 的两种原生接口，不升级整套 Agents SDK，也不在失败后自动切换传输。模型实际返回的调用 `async` 标志决定是否允许在该结果未返回时继续响应，不能把工具声明支持异步当作调用已经异步。

```bash
python -m pytest -q
python tools/probe_responses_protocol.py --env-file /private/provider.env --model gpt-6.1-sol
python -m tests.e2e_real_client_agent_collect --disposable-local-server --continuity --env-file /private/provider.env --model gpt-6.1-sol
```

协议实验只验证真实 API，不启动 Minecraft。默认 HTTP 验证原生异步调用、未完成调用期间的新输入和原始 call_id 的迟到结果；--transport websocket 另验 steering 自动后继响应。没有成功就返回具体阶段与错误。原生接口定义见 [异步工具](https://developers.openai.com/api/docs/guides/async-tool-calling)、[steering](https://developers.openai.com/api/docs/guides/steering)。

新内核用 `update_plan` 和 `finish_task` 承载短计划与模型判断；计划 revision、记录 ID 由运行时填写。模型完成记录明确标注 `completion_authority=model`，不套用旧物资目标解析器，也不标成服务端已独立验证。真实任务实验仍用库存、交接与位置观测确认结果。

## 当前结果（2026-10-07）

原生异步主线已通过真实验收：gpt-6.1-sol / OpenAI SDK 2.36.0 / HTTP Responses SSE / 当前私有端点。原生异步工具不要求 WebSocket；模型调用实际带 async=true，工具未完成时可以继续聊天，再按原始 call_id 接回迟到结果。[官方异步工具文档](https://developers.openai.com/api/docs/guides/async-tool-calling)。

真实模型通过生产交互入口完成以下任务，耗时 83.779 秒：

- 空库存出发，采集石墙后的三根橡木原木；采集期间发送普通聊天和“交接后返回出发点”。模型实际回复了两条消息，原采集调用继续运行，结果按原始 call_id 续接。
- 交接初次距离不足，模型自行靠近并重试；服务器独立确认 Receiver 库存为三根原木。
- 完成交接后返回出发点附近（33.710, 150, 28.255），距指定点约 1.24 格。
- 单独开始行走，观察到实际移动后明确取消。身体终态已收尾，客户端 pathing=false、any_process_active=false；服务器一秒间隔的两次位置均为（35.079, 150, 28.488）。

结果、用户消息、原生异步调用/结果确认、取消回执和服务器观测在 [result.json](logs/agent-collect-1791368744299662501/result.json)；对应 trace.jsonl 和 agent.log 位于同目录。实验仅使用已选择的 disposable 本地世界；Agent 不持有 RCON。

对应接口回归 70 项 / 15.75 秒通过；配置默认值与旧路径选择回归 15 项、3 个 subtests / 3.01 秒通过；真实客户端 Java 编译及加载通过。未运行历史全量，也未增加 Gate 或另一套测试平台。移除了会阻塞 HTTP 聊天的多余 wait_for_tasks 工具，结果就绪时由内核自动续接。请求打开事件流前的连接错误交给基础 SDK 最多重试两次；收到事件后的物理工具不会被 SDK 重放。

失败实验保留在 logs/：交互入口误依赖 Java event_head；blockAt 缺少 interaction_ready；没有专用采集后端时 lambda 返回 None；即时挖掘只有客户端预测；拾取没有匹配 UUID 和距离。这些实际阻断已分别修正，挖掘复用上游 Baritone 的单方块 clear 过程，未扩建自研导航。一次客户端启动 SIGSEGV 和一次 HTTP 连接失败也记录为失败；最终实验通过并不覆盖或删除它们。

## WebSocket steering：已移出当前工作范围

代码保留原生 response.steer 路径及定向协议回归，尚未通过当前 provider 的真实 steering 验收。此前同一模型握手可达，但首次 response.create 在任何响应事件前被关闭：1013 / no available account。HTTP previous_response_id 也被该端点拒绝，当前 HTTP 路径因此使用完整历史、store=false 与 reasoning.encrypted_content。

Sub2API 源码核对固定为 0.2.14 / 3f1a2ea0a760730e3bc528105c00b4ee4f23e469，未确认 provider 部署版本。ctx_pool 与 http_bridge 只接受 response.create；passthrough 能透传其他事件，但本端点尚未实测。需要 provider 的可用 WebSocket 账户与相应配置才能继续这一项。[入口](https://github.com/Wei-Shaw/sub2api/blob/3f1a2ea0a760730e3bc528105c00b4ee4f23e469/backend/internal/service/openai_ws_forwarder_ingress.go#L131-L264)、[透传](https://github.com/Wei-Shaw/sub2api/blob/3f1a2ea0a760730e3bc528105c00b4ee4f23e469/backend/internal/service/openai_ws_v2_passthrough_adapter.go#L1150-L1162)、[账户选择](https://github.com/Wei-Shaw/sub2api/blob/3f1a2ea0a760730e3bc528105c00b4ee4f23e469/backend/internal/handler/openai_gateway_handler.go#L2700-L2729)。

HTTP 在当前模型响应结束后处理排队消息；WebSocket 原生 steering 可以在模型响应进行中注入要求，两者不混称。[官方 steering 文档](https://developers.openai.com/api/docs/guides/steering)。用户已明确不继续这项工作。以下源码与失败记录保留为历史证据，不触发后续开发或验证。


## 上下文管理调研（2026-10-08，待讨论）

这一节记录首次调研的分析和候选方案；后来用户授权的小幅优化与验证见本文开头，没有新增可执行计划。检索窗口为 2026-07-08 至 2026-10-08；有明确发布日期的研究与当前官方能力分开描述，不把旧功能写成最近三个月的新发布。

现状：旧 SDK 会话已有 12 轮 / 96 items / 64,000 字符窗口；Responses HTTP 的 `_HttpResponsesConnection.history` 仍完整累积并在每轮重发。`AgentContext` / `ContextCompiler` 已能组织目标、任务、身体状态、近期结果，但这不限制原始 Responses 历史。现有 TaskWorkspace 和完整工具观测归档可以复用，无需另建存储平台。

近期值得借鉴的研究：

| 日期与来源 | 具体进展 | 对 MineBot 的判断 |
| --- | --- | --- |
| 2026-07-11，[Compaction as Epistemic Failure](https://arxiv.org/abs/2607.13071) | 记录压缩将超时/终止过程的部分输出写成已确认结果的案例 | 摘要不能把 pending、失败或客户端预测改写成成功；动作事实保留真实终态与观测引用。该研究是案例分析，不证明所有压缩都会这样。 |
| 2026-08-24，[The Compaction Cliff](https://arxiv.org/html/2608.22752v1) | 分类型保留约束、程序、事实、偏好和事件，研究多轮均匀压缩丢失约束的问题 | 玩家明确要求与项目适用约束原样注入；旧工具输出允许缩短。借鉴保留策略，不引入论文的分类器/验证平台。 |
| 2026-09-29，[PAIR](https://arxiv.org/html/2609.36526v1) | 从同一个环境状态比较压缩前后的后续执行，定位有害压缩并修改对应摘要段落 | 用真实续行动作判断压缩是否有效，失败时检查丢了哪条事实。论文实验在 AppWorld、OfficeBench 和零售任务，不是 Minecraft；不搬入离线训练/反复 rollout 管线。 |
| 2026-09-30，[ReCAP](https://arxiv.org/html/2609.40118v1) | 根据当前请求、过去 attention 重要度与消息依赖，挑选历史及其支持证据 | 借鉴按当前任务找回相关事实、调用与结果一起保留。完整方法需要 attention；闭源 API 要额外代理模型估算，当前不划算。论文性能不代表本项目性能。 |

当前成熟接口也可用作候选，但以下文档没有足够发布日期证据，不能当作近三个月发布：

- [OpenAI Responses compaction](https://developers.openai.com/api/docs/guides/compaction)：支持 `context_management` 阈值触发的服务端压缩和独立 `/responses/compact`。支持 `store=false` 的 input-array 续接，不依赖 WebSocket 或 `previous_response_id`；压缩状态是不可读的加密 item。自动压缩后可按文档删去最新压缩项之前的历史；独立 compact 返回的窗口则必须整体保留，不能套用前一种裁剪方式。当前 provider 的 compaction 尚未实测，不能宣称支持；异步 pending 与压缩的组合也需验证。
- [Pi compaction 文档](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/compaction.md)：结构化目标/约束/进度/决策/下一步摘要，加最近完整消息，调用和结果不得被裁开。这个模式可以借鉴，不需要为它更换内核。

建议首先做任务工作上下文，而不是先做跨会话长期记忆：

| 内容 | 来源与更新方式 |
| --- | --- |
| 玩家当前目标、追加要求、适用约束 | 持续保留有效原文；取消、完成或用户修改后更新，不让摘要自行改写要求。 |
| 当前阶段、缺少材料、障碍、改变策略的理由 | 复用现有任务/短计划；由模型根据实际行动更新，运行时不写游戏策略。 |
| 位置、库存、健康、目标是否仍存在 | Body 新读取的状态；旧观测写明时间，不把旧库存当作现在的库存。 |
| 最近完整行动、失败、未完成原始 call_id | 保留调用与结果关系；pending 和未交付结果不剪掉，不因压缩重执行身体动作。 |
| 更早的动作细节、地点、资源和蓝图证据 | 完整记录复用现有归档，工作上下文只保留任务相关结论和可查回引用。 |

历史压缩优先考虑已验证 provider 下的原生 compaction；明确任务状态仍单独注入。若该接口实际不可用，采用小型结构化滚动摘要加完整近期行动窗口，不扩成兼容平台。最初只在已完成行动的边界压缩，避免先引入 pending 压缩竞态；这是实施建议，不是官方对异步组合的保证。

例子：采木交接后玩家追加“回出发点”。上下文需要保留追加要求、出发点坐标、原采集调用、交接结果及距离不足后靠近的理由；当前库存重新读取，不反复携带完整方块扫描。验证看压缩后是否继续正确行动、是否遗忘要求或重复动作，以及实际输入量/延迟；不以摘要长度或模块数量验收。
