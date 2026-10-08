# Body 逐能力真实任务基线（2026-10-07）

本轮由开发者直接调用生产 ToolRegistry callable 和抽象 Body，不使用模型 Agent；所有报告的 `model_calls=0`。44 个基础工具都有实际成功调用，目录之外的 Baritone 采矿、农场、建造/清除、文件蓝图、飞行和控制也完成了下列真实任务。**这份记录确认物理执行基线，不宣称完整 Agent 任务已经验收。**

## 实验条件

仅使用明确选择的 disposable 本地世界 `127.0.0.1:25566`，组合为 MC 26.1.2 / Fabric Loader 0.19.3 / Fabric API 0.152.1 / Java 25 / upstream Baritone 1.18.0。交接使用第二个真实客户端 Receiver。共享服务器未参与。

RCON 仅用于准备地形、物品、目标实体和独立观察；正式执行由生产工具/Body 完成。移动目标实验在同一次跟随调用中改变 Receiver 位置两次，用作外部刺激，没有替机器人发新的导航指令。成功根据真实库存、位置、方块、实体、菜单或同步状态核对。场地提供必要材料、工具和加载的目标区域；每轮清理上一轮的非玩家实验实体。

## 真实结果

表中部分早期脚本在后面的能力失败；仅引用已成功并有实际结果的步骤，后续失败由独立任务修复复测，不能把整个早期脚本标为通过。

| 能力/任务 | 真实结果 | 记录 |
|---|---|---|
| 精确导航 | 不挖不放，穿过四道交错的五格高墙，抵达指定目标格；墙保持 | [navigation](logs/body-complex-20261007/navigation-1791373171006875838/result.json) |
| 上游数量采矿 | 初始 5 根橡木，三个分散矿源净增 24，最终 29 | [mine_resource](logs/body-complex-20261007/mine_resource-1791371985735426941/result.json) |
| 资源事务 | 接近工作目标；指定矿石采集且邻矿不动；分散原木净增 18 | [collection 的成功步骤](logs/body-complex-20261007/collection-1791373219928361938/result.json) |
| 掉落物拾取 | 6 堆各 12 颗绿宝石，较近的泥土作为非目标；收齐 72 | [pickup](logs/body-complex-20261007/pickup-1791373866802662721/result.json) |
| 感知与分页 | 64 个矿石分多页无遗漏/重复；稀疏库存 7 钻石与 13 铁锭；实体半径/类型、附近方块、配方读取 | [perception](logs/body-complex-20261007/perception-1791372050650091343/result.json) |
| 探索 | 找到初始扫描半径外的指定资源；实际返回矿石坐标；上游长期探索另验取消 | [exploration](logs/body-complex-20261007/exploration-1791374129769619916/result.json) |
| 合成 | 64 木棍、8 工作台、2 蛋糕；6 个空桶余物；真实物品容量拆批 | [menus](logs/body-complex-20261007/menus-1791372469591529540/result.json) |
| 容器/熔炉 | 从箱子跨槽取出 96 钻石，再存回 80；高炉完成 4 铁锭，余料/燃料清理 | [menus](logs/body-complex-20261007/menus-1791372469591529540/result.json) |
| 装备与战斗 | 20 格外的活动目标；近战、弓、弩分别击杀；近处非目标维持 20 HP | [combat](logs/body-complex-20261007/combat-1791372235631535199/result.json) |
| 垂直作业 | 下挖 12 格、指定泥土上升 12 格、穿透顶层回到 171；逐格核对支柱 | [vertical](logs/body-complex-20261007/vertical-1791372341282753469/result.json) |
| 多层建造/清除 | 6×3×4 共 72 格建成，再清除；每格核对，底面保持 | [building 的成功步骤](logs/body-complex-20261007/building-1791372601282010238/result.json) |
| 文件蓝图 | Sponge v2 空心门洞房屋 80 格；100 个实心/空气格全部符合；新场地和原场地暂停/恢复均通过 | [file_house](logs/body-complex-20261007/file_house-1791376449082910261/result.json)、[native_controls](logs/body-complex-20261007/native_controls-1791376586262421219/result.json) |
| 农场与单株农事 | 80 株小麦全部收获/重种；另验远距离耕地、播种、收获重种，逐格确认 | [farm 成功步骤](logs/body-complex-20261007/farming-1791372719773344218/result.json)、[crop_interactions](logs/body-complex-20261007/crop_interactions-1791373197696360980/result.json) |
| 精确放置 | 三处远距离/侧面放置与就近放置，服务端方块符合目标 | [placement](logs/body-complex-20261007/placement-1791373095689338229/result.json) |
| 实体与方块使用 | 给指定牛挤奶、给指定羊剪毛；门闸打开/关闭、开关上电、指定地面点火、饮用牛奶 | [interactions 的成功步骤](logs/body-complex-20261007/interactions-1791375366708939886/result.json) |
| 食物/药水/丢弃 | 饥饿下吃面包；背包槽位的 19 钻石丢弃到地面；喝药水得到速度效果，再用牛奶清除 | [interactions 成功步骤](logs/body-complex-20261007/interactions-1791375366708939886/result.json)、[effects](logs/body-complex-20261007/effects-1791376442398662689/result.json) |
| 睡床 | 接近 24 格外的床并进入睡眠；Body sleeping=true、服务端 SleepTimer>0 | [sleep](logs/body-complex-20261007/sleep-1791375628899926135/result.json) |
| 玩家交接 | 从 96 钻石中跨堆叠交接 80；服务端接收方 80、发送方 16 | [players 的成功步骤](logs/body-complex-20261007/players-1791375143760306438/result.json) |
| 跟随/远离 | 接近指定玩家；一次 18 秒跟随内响应两次目标移动；另验 16 格安全距离与复查 | [players 成功步骤](logs/body-complex-20261007/players-1791375143760306438/result.json)、[spacing](logs/body-complex-20261007/spacing-1791375637371965950/result.json) |
| 鞘翅飞行 | 掉落起飞、250 格飞行、落在升高的平台；飞行与落地状态及服务端位置确认 | [flight](logs/body-complex-20261007/flight-1791373586790481935/result.json) |
| 取消 | 导航/采矿/建造/上挖取消延迟 191/200/124/148 ms；停止后无 pathing/process；探索取消 128 ms | [controls](logs/body-complex-20261007/controls-1791373546200237561/result.json)、[exploration](logs/body-complex-20261007/exploration-1791374129769619916/result.json) |
| 原生控制 | 选择区添加/扩展/收缩/平移/清空；文件建造暂停/恢复、状态读取；跳跃服务端实际离地、视角、槽位、停止、探索过滤和 Elytra repack/reset 调用 | [native_controls](logs/body-complex-20261007/native_controls-1791376586262421219/result.json)、[native_misc](logs/body-complex-20261007/native_misc-1791376748067508927/result.json) |
| 独立文件过程取消 | 没有 pending Python 写动作时 interrupt 仍能停止原生文件 builder；299 ms，process/pathing 停止且服务端库存不再变化 | [file_cancel](logs/body-complex-20261007/file_cancel-1791376999545442581/result.json) |
| 原生黑名单 | 一个 getToBlock 调用中排除最近箱子，自动到达另一个箱子；空闲时明确拒绝 | [blacklist](logs/body-complex-20261007/blacklist-1791376066239252136/result.json) |

## 修复与失败记录

本轮复现并修复了：数量采矿取到一件就结束；感知截断后过滤/空附近方块/实体半径；半径向上取整导致邻格提前完成；放置没有接近、支撑面和服务端确认；批量配方没有按实际容量拆批；地面/实体交互站位、触达距离和视角；拾取完成第一堆后反复回访空位置；交接换槽同 tick 丢弃导致服务器只收到 32 件；跟随窗口到期一律超时；方块属性值包含字段名；Body 时间写死/睡眠与效果漏读；原生跳跃输入被覆盖；独立原生过程无法通过 interrupt 停止；空闲黑名单缺少明确拒绝。

文件建造的早期失败不是完整建成：不暂停对照只放出 51/80 格，缺失地板为 (710,150,8)。后续服务器观察发现上一轮静止羊仍站在 (710.5,150,8.5)，恰好占着该格，见 [占位观察](logs/body-complex-20261007/house-obstruction.json)。新场地完成 80/80；清掉旧实验实体后原场地也完成 80/80，且暂停/恢复通过。原始失败 [file_house](logs/body-complex-20261007/file_house-1791376231635524844/result.json) 和三个早期 native_controls 报告保留。施工位被实体持续占用时不能算成功，Body 不应替 Agent 决定击杀或迁走动物。

另外保留了准备地面未加载/死亡、探索只达到预算却被旧断言误判、错误实验参数、以及丢弃后过早进行服务端查询的失败。探索只接受实际找到资源的 [最终记录](logs/body-complex-20261007/exploration-1791374129769619916/result.json)；丢弃的最终记录等到了真实服务器库存更新。这些失败不计作能力成功。

选定组合未安装 Schematica/Litematica 编辑器插件，打开编辑器蓝图桥接分别返回 `schematica_not_installed` / `litematica_not_installed`，明确属于缺依赖；文件方式的 Sponge v2 建造已通过。未试的蓝图格式、完整外部编辑器、地面自动起飞不能用本轮结果代替。本轮飞行条件明确为已装备鞘翅/烟花并从高处掉落起飞。

## 44 个基础工具的调用证据

以下逐项对应当前 CanonicalBodyToolCatalog 的 base 工具；没有把 `collect_resource` / `ensure_tool_for` 的 Agent 组合规划算入 Body 基础验收。

| 基础工具 | 成功调用所在记录 |
|---|---|
| `read_state` | [perception-1791372050650091343](logs/body-complex-20261007/perception-1791372050650091343/result.json) |
| `read_inventory` | [perception-1791372050650091343](logs/body-complex-20261007/perception-1791372050650091343/result.json) |
| `move_to` | [navigation-1791373171006875838](logs/body-complex-20261007/navigation-1791373171006875838/result.json) |
| `explore_for` | [exploration-1791374129769619916](logs/body-complex-20261007/exploration-1791374129769619916/result.json) |
| `go_to_surface` | [vertical-1791372341282753469](logs/body-complex-20261007/vertical-1791372341282753469/result.json) |
| `follow_entity` | [players-1791375143760306438](logs/body-complex-20261007/players-1791375143760306438/result.json) |
| `engage_entity` | [combat-1791372235631535199](logs/body-complex-20261007/combat-1791372235631535199/result.json) |
| `ranged_attack` | [combat-1791372235631535199](logs/body-complex-20261007/combat-1791372235631535199/result.json) |
| `find_hostiles` | [combat-1791372235631535199](logs/body-complex-20261007/combat-1791372235631535199/result.json) |
| `search_for_block` | [perception-1791372050650091343](logs/body-complex-20261007/perception-1791372050650091343/result.json) |
| `mine_block_collect` | [collection-1791373219928361938](logs/body-complex-20261007/collection-1791373219928361938/result.json) |
| `craft_item` | [menus-1791372469591529540](logs/body-complex-20261007/menus-1791372469591529540/result.json) |
| `equip_item` | [combat-1791372235631535199](logs/body-complex-20261007/combat-1791372235631535199/result.json) |
| `smelt_item` | [menus-1791372469591529540](logs/body-complex-20261007/menus-1791372469591529540/result.json) |
| `move_away` | [spacing-1791375637371965950](logs/body-complex-20261007/spacing-1791375637371965950/result.json) |
| `get_to_block` | [collection-1791373219928361938](logs/body-complex-20261007/collection-1791373219928361938/result.json) |
| `go_to_player` | [players-1791375143760306438](logs/body-complex-20261007/players-1791375143760306438/result.json) |
| `follow_player` | [players-1791375143760306438](logs/body-complex-20261007/players-1791375143760306438/result.json) |
| `search_for_entity` | [players-1791375143760306438](logs/body-complex-20261007/players-1791375143760306438/result.json) |
| `give_player` | [players-1791375143760306438](logs/body-complex-20261007/players-1791375143760306438/result.json) |
| `consume_item` | [effects-1791376442398662689](logs/body-complex-20261007/effects-1791376442398662689/result.json) |
| `discard_item` | [effects-1791376442398662689](logs/body-complex-20261007/effects-1791376442398662689/result.json) |
| `transfer_container_item` | [menus-1791372469591529540](logs/body-complex-20261007/menus-1791372469591529540/result.json) |
| `read_container` | [menus-1791372469591529540](logs/body-complex-20261007/menus-1791372469591529540/result.json) |
| `clear_furnace` | [menus-1791372469591529540](logs/body-complex-20261007/menus-1791372469591529540/result.json) |
| `go_to_bed` | [sleep-1791375628899926135](logs/body-complex-20261007/sleep-1791375628899926135/result.json) |
| `set_openable_state` | [interactions-1791375366708939886](logs/body-complex-20261007/interactions-1791375366708939886/result.json) |
| `till_farmland` | [crop_interactions-1791373197696360980](logs/body-complex-20261007/crop_interactions-1791373197696360980/result.json) |
| `sow_crop` | [crop_interactions-1791373197696360980](logs/body-complex-20261007/crop_interactions-1791373197696360980/result.json) |
| `harvest_and_resow` | [crop_interactions-1791373197696360980](logs/body-complex-20261007/crop_interactions-1791373197696360980/result.json) |
| `set_switch_state` | [interactions-1791375366708939886](logs/body-complex-20261007/interactions-1791375366708939886/result.json) |
| `use_item` | [interactions-1791375366708939886](logs/body-complex-20261007/interactions-1791375366708939886/result.json) |
| `use_on_entity` | [interactions-1791375366708939886](logs/body-complex-20261007/interactions-1791375366708939886/result.json) |
| `use_on_block` | [interactions-1791375366708939886](logs/body-complex-20261007/interactions-1791375366708939886/result.json) |
| `place_block` | [placement-1791373095689338229](logs/body-complex-20261007/placement-1791373095689338229/result.json) |
| `place_here` | [placement-1791373095689338229](logs/body-complex-20261007/placement-1791373095689338229/result.json) |
| `dig_down` | [vertical-1791372341282753469](logs/body-complex-20261007/vertical-1791372341282753469/result.json) |
| `dig_up` | [vertical-1791372341282753469](logs/body-complex-20261007/vertical-1791372341282753469/result.json) |
| `pickup_items` | [pickup-1791373866802662721](logs/body-complex-20261007/pickup-1791373866802662721/result.json) |
| `collect_block_domain` | [collection-1791373219928361938](logs/body-complex-20261007/collection-1791373219928361938/result.json) |
| `read_block` | [perception-1791372050650091343](logs/body-complex-20261007/perception-1791372050650091343/result.json) |
| `read_nearby_blocks` | [perception-1791372050650091343](logs/body-complex-20261007/perception-1791372050650091343/result.json) |
| `read_nearby_entities` | [perception-1791372050650091343](logs/body-complex-20261007/perception-1791372050650091343/result.json) |
| `read_recipe` | [perception-1791372050650091343](logs/body-complex-20261007/perception-1791372050650091343/result.json) |

## 固定 Body / Agent 边界

Body 接收明确动作、目标、资源和预算，负责物理追踪/路径执行、菜单/槽位操作、实际状态读取、动作结果与取消。上面的复杂任务证明这些动作可由现有调用入口使用；不会因为模型轮次结束或聊天而重新实现另一套身体。

Agent 负责理解玩家目标、拆短计划、连续选择动作、任务上下文、普通聊天/追加要求、明确取消意图，以及失败后改变任务策略。比如目标被动物占位时选择迁走动物、换施工位置或终止建造，属于 Agent 的策略；Body 不能把未建成报告成完成。

后续 Agent 实验出问题，先留下实际 Body 调用参数和当时状态，再直接复现同一个调用。只有复现出物理执行、状态、数量、终态或取消错误才修改 Body；如果 Body 已按明确输入执行，问题在计划/上下文/工具选择，就修改 Agent。这里采用本轮已验证的条件和失败事实，不把历史 Gate 或旧计划当成新的 Body 待办。

## 复现与检查

已有客户端与本地 disposable 服务器启动后：

```bash
PYTHONPATH=. .venv/bin/python tests/e2e_real_client_body_complex.py --disposable-local-server --case <任务>
```

脚本只提供具名任务，不默认串行跑全套。原始参数、终态、游戏结果与观察存放在 `logs/body-complex-20261007/`。相同上游能力的别名共享同一行为证据。

最终相关快速回归：237 项测试和 30 个 subtests，2.10 秒通过；导航事务另有对应的 112 项定向回归通过。Java 编译与真实 Fabric/Mixin 加载通过。本轮新增两个短状态/取消适配回归和两个半径参数用例，主要证据来自真实任务，未运行历史全量、未调用模型、未新增 Gate/测试平台。

提交：`264fd9e`（第一批数量/感知/放置/菜单修复与实验入口）、`6928e18`（交接/跟随/使用/状态/取消修复与完整任务验证）。这两次提交只处理本轮 Body 实验切片。

## 2026-10-08：建房实验中的边缘挖掘卡顿

实际调用 `clear_area(start=[-15,66,7], end=[-6,74,16], timeout_s=300)` 超时。
保留调用、终态和客户端日志后，停止模型会话，用独立角色直接复现。
同一区域后来直接清完，不能以背包增量推算挖掉的全部方块。

最小复现为两个泥土块 `[1006,101,1003]`、`[1006,100,1006]`，
角色起点 `[1001.8907689,100,1004.5029796]`，生存模式、空手。
默认随机瞄准抖动下，30 秒后两个方块都还在，角色没有移动；逐次挖掘记录显示目标切换和进度重置。
仅将 reach 从 4.5 改为 3.5 仍超时。关闭 `randomLooking`、`randomLooking113` 后，
生产 Body 编译版本在 2.15 秒清完，实际方块读回为 air；动作期间两项为零，终态后恢复原值。
原生 Baritone 对照同样会切换，未观察到两个 Body 动作并行抢占。

修复仅在现有 Body 动作期间禁用这两项抖动，完成、失败、取消和断开时恢复。
156 个混合石土方块的相同区域：修复前 85.66 秒，修复后 77.93 秒，均逐格确认全部为空；
修复后向前移动约 11.4 格。取消后 13 ms 收到终态并恢复原设置。
这是一组场景对照，不是通用速度提升承诺。未运行历史全量。

证据：`logs/house-20261008-145412/attempt-3-official/clear-area-control-fault/`。
探针物资只给独立角色 `MineBotDigProbe`；建房角色的背包未补给。
原建房区域由开发者直接补清过一次，后续续跑需记录这次介入，不能称为无干预单次成功。
