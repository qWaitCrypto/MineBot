# Body 执行状态暴露设计

2026-10-08。**设计稿，尚未实现。** 本文服务于一个具体行为：一次 Body 工具正在执行时，调用方能知道它正在做什么、最近真实推进了什么、正在等什么，以及它结束后是否还占着角色。范围为当前 44 个基础工具、7 个客户端工具，并补充两个组合工具和直接 Body 入口。

依据为完整 [Issue #1](docs/references/issue-1-route-correction-20261006.md)、当前代码和建房实验。本文不设计模型在线读取机制、反射决策、自动补料、任务规划或新调度器；不改变正在运行的实验。完成这份逐项设计即为本轮停止条件。

## 现状与需要补上的连接

- 客户端 `MineBotClientBody.tick` 已约每 20 tick 发一次 `progress`，包含动作 ID 和客户端状态。它有 structured action 的内部阶段、原生进程标志、位置、库存和菜单确认信息，但没有统一的语义状态摘要。
- `RealClientIpc._wait_terminal` 已支持 `on_progress`；`RealClientBody._run_action` 调用 `action/command` 时没有传该回调，中间状态没有进入上层工具视图。
- `craft_item`、`smelt_item`、`give_player` 等 Python 事务会连续调用多个 Body 动作。只暴露某个 Java 动作会遗漏批次之间的准备、等待、核验和清理。
- 建房实验的 `build_structure(anchor=[-15,66,7], size=[10,1,10], block=cobblestone, timeout_s=300)` 消耗快捷栏的一组材料后长时间没有推进，最终 `deadline_exceeded`；随后 Agent 自己装备材料并继续施工。状态应分别呈现快捷栏数量、背包数量、已核验格子和最近推进时间。快捷栏为空不能直接推断背包缺料，进程 active 也不能代表仍在施工。

## 一个简短对象，覆盖整次工具调用

长工具保留一个最新快照。`operation_id` 复用原工具 `call_id`；开发者直接调用 Body 时复用 `Action.id`。工具内部多个动作共用外层 operation，只更新当前 `action_id` 和阶段；不建立任务树、独立任务数据库或另一套句柄。

下面是**拟议格式示例，并非新的实验结果**：

```json
{
  "operation_id": "call_example",
  "tool": "build_structure",
  "action_id": "action_example",
  "state": "running",
  "phase": "placing",
  "reason": null,
  "observed_at": "2026-10-08T09:40:00Z",
  "last_progress_at": "2026-10-08T09:39:15Z",
  "control": "held",
  "facts": {
    "source": "client",
    "anchor": [-15, 66, 7],
    "size": [10, 1, 10],
    "block": "cobblestone",
    "checked_cells": 100,
    "matching_cells": 64,
    "hotbar_count": 0,
    "inventory_count": 64,
    "builder_active": true
  }
}
```

| 字段 | 含义 |
| --- | --- |
| `operation_id`, `tool` | 整次工具身份。直接 Body 调用的 tool 使用动作名。 |
| `action_id` | 当前底层动作；纯 Python 阶段或没有动作时为 null。不得继续挂着已结束的子动作 ID。 |
| `state` | `accepted / running / waiting / stopping / succeeded / failed / cancelled / unknown`。这是新增摘要，不重命名既有 Result/Event 终态。 |
| `phase` | 工具语义阶段，如 `approaching / breaking / awaiting_menu / smelting / verifying`。不暴露 Java 的 phase=11，也不要求所有工具经过同一组阶段。 |
| `reason` | 已知等待条件、具体失败或结束原因；沿用既有 reason，必要的新 reason 在实现时明确加入。没有证据时为 null，不猜“缺料”“无路”。 |
| `observed_at` | 最新实际观测时间。收到旧缓存、重连或查询请求不能刷新成一次新观测；UTC 时间用于展示，本机耗时使用单调时钟。 |
| `last_progress_at` | 最近一次与本动作相关的真实推进；从未观测到则 null。心跳、重复日志、挥手动画和模型消息不刷新它。 |
| `control` | **本操作**的 `held / released / unknown`，见下文。它不表示 Brain/reflex 的决策权。 |
| `facts` | 小型 MC 事实对象。只带目标、当前必要读数、实际完成量和等待依赖；原始大列表仍走已有观察详情。来源放在 `source`；不同时间采样的读数保留其采样时间。 |

不强制百分比、ETA、置信度或统一“健康分”。有限工作有实际分子、分母时才给数量，并说明口径；发现候选不等于采到物品、方块消失不等于拾取、匹配格子不等于本次新放置格子、发出右键不等于交互成功。未扫描的区域不能计作完成或缺失。每次观察值允许随世界变化而回退，不伪造单调进度。

`facts` 使用表中稳定含义的字段，不把全文日志塞进去。通常只有几个标量、一个目标和少量示例；例如批量核验只带 checked/matching/total 和至多现有的 8 个不匹配示例。状态是一个原子快照，不能拼成“新阶段 + 旧动作读数”；累计已确认结果可保留，当前观测与历史累计要区分。

## 生命周期、等待与控制

1. **接收与执行。** 完成入参校验并接纳后为 accepted，真正进入事务为 running。`body_busy`、不支持、非法参数等拒绝立即返回具体失败，不伪造一个运行中的过程。瞬时动作允许直接进入终态。
2. **合法等待。** waiting 必须带已知依赖及继续条件，例如服务端菜单 revision、熔炉 cook progress、弓蓄力、收件人拾取或显式 builder pause。已知为路径计算时可标记 calculating；只有 pathing=false 不能证明在算路。单纯不知道原因的静止不能命名为正常等待。
3. **推进。** 导航记录沿有效路径的推进或新可达区域，位置/距离本身只是事实，不把来回抖动算推进；挖掘记录同一目标的挖掘进度和方块确认；合成/转移记录已确认批次和槽位 revision；施工记录实际匹配/放置证据；熔炉记录 cook progress/产出。瞄准、冷却和保持距离可以合法不动，不能统一用“几秒没走路”判断失败。反复换目标、挖掘进度重置等也不能冒充累计推进。
4. **无法继续。** 已确认当前执行器无法自行满足的前置条件，返回具体失败及部分成果，正常停止并释放本操作。是否重新装备、补料、换地点由调用方决定。是否仍有可用材料、内部是否正在换槽必须按事实表达。仅有“很久没变化”时保留时间和未知原因，不编造材料缺失，也不在此设计新增一个全局超时策略；已有动作预算仍生效。
5. **核验与清理。** 底层动作完成后，外层可能仍在 verifying、collecting_output、recovering_workstation 或 cleaning_grid。外层结果只在自身实际结束后终结；不能让动作之间的空隙看起来像整个工具空闲。
6. **取消。** 发起取消后为 stopping；直到本操作的客户端过程、按键/挖掘/菜单操作以及 Python 后续动作都停止，才标 cancelled、control=released。父操作取消不能在下一批子动作上重新启动。已经发生的世界变化和物品转移保留在结果里。
7. **故障。** 超时或 Python worker 抛异常不等于客户端已停。能确认停止才 released；断线而无法确认时为 unknown。工具可以返回明确的 transport 失败，同时保留 control=unknown；既有占用判断不能因此放行下一次冲突写入。重连只读取、核对原动作与原生过程，不自动重放施工。
8. **结束。** 成功、普通失败或取消后保留该 operation 的最后快照，不清空成“从未做过”。新操作用新 ID；迟到的旧动作事件不能覆盖它。重新启动后没有恢复证据的状态为未知，不能凭内存为空宣称角色空闲。

`held` 表示本操作仍有权发出后续物理动作，或仍有未确认停止的物理动作，包括事务的子动作间隙；它不是“每一 tick 都在按键”。`released` 表示本操作不会再发后续动作且其已发动作已结清；不保证整个世界没有其他活动。炉火继续燃烧、物品实体继续下落不属于角色仍被本操作控制。现有 execution lane/Body busy 仍负责实际准入，状态对象不成为第二套锁或调度器。

## 44 个基础工具：逐项设计

表中阶段与字段是**实现目标**，不是声称当前代码已全部提供。阶段按真实分支选择，已经满足目标可直接核验结束；读取不到的字段缺省，不用推测填满表格。

### 读取与搜索（9 个）

短读取通常只有一次采样和结果，不制造一秒钟的进度过程。分页/菜单读取才需要中间阶段。它们不覆盖正在工作的写操作状态；`read_state` 返回的 execution 指向被观察的操作，不指向这次 read_state 自己。

| 工具 | 阶段 | 必要事实、等待与结束依据 |
| --- | --- | --- |
| `read_state` | sampling → terminal | 原 BodyState + 当前/最近写操作 execution；来源、采样时间、连接状态。读取失败不清除原操作，也不把旧状态更新为当前。 |
| `read_inventory` | sampling → terminal | 实际库存、槽位/装备、采样时间；保留空槽与真实空背包语义，不把未读取当 0。 |
| `find_hostiles` | scanning → terminal | 查询半径、实际采样实体数、命中数与截断标志；范围内未发现不等于世界没有敌人。 |
| `search_for_block` | scanning/paging → terminal | block_types、实际搜索范围、页数、命中及截断/未加载信息。**只找候选，不移动**，命中不代表可站立、可见或可交互。 |
| `read_container` | opening → awaiting_menu → reading_pages → terminal | 容器坐标、菜单 ID/revision、已读/总槽位、页数；等待实际服务端菜单确认。翻页间 menu 改变则明确失败，不拼出混合快照。读菜单可能打开界面，不能因 sidecar 是 read 就与另一菜单写入互抢。 |
| `read_block` | sampling → terminal | 坐标、方块类型/properties、是否已加载、采样时间；未加载不是 air。 |
| `read_nearby_blocks` | scanning/paging → terminal | anchor、范围、已读页/单元、返回数量、是否截断；不把分页进度转成资源采集进度。 |
| `read_nearby_entities` | sampling → terminal | 范围、实体 UUID/必要属性、返回数量、采样时间与截断；同名实体保持身份区分。 |
| `read_recipe` | reading_recipe → terminal | 配方 ID、工作站需求、实际配方匹配及读取来源；配方存在不等于背包原料满足或已经合成。 |

### 导航、接近与探索（9 个）

| 工具 | 阶段 | 必要事实、等待与结束依据 |
| --- | --- | --- |
| `move_to` | preparing → navigating → verifying | 目标/到达半径、当前位置、距离、pathing/goal_active；到达几何条件才成功。进程停止但未到达返回具体实际原因。 |
| `move_away` | choosing_destination → navigating → verifying | 避开对象/起点、请求最小距离、选定落点、实际距离；验证远离条件，不只验证一个中间落点动作结束。 |
| `get_to_block` | locating → approaching → verifying_reach | 目标方块身份/坐标、站位、距离、可用的可达/交互范围证据；未找到与找到但不可接近分开。 |
| `go_to_player` | locating_player → approaching → verifying_range | 名称及解析出的 UUID、目标最新位置、距离与距离带；目标丢失或离开范围明确记录。 |
| `follow_player` | locating_player → approaching → verifying_range | 玩家 UUID、距离带、实时距离；**当前事务是进入距离带后返回**，不能把状态写成永久跟随服务。 |
| `search_for_entity` | locating_entity → approaching → verifying_range | 类型/名称筛选、选中 UUID、实时距离、交互范围；**当前工具包含接近**，不能列为纯读。 |
| `follow_entity` | acquiring_target → following/holding_distance → terminal | 目标身份/位置、双方距离、原生 follow 状态、当前动作的结束条件和期限。保持距离合法静止；区分达到本次距离目标、取消、失去目标、期限结束，不把一次结果宣称为永久陪伴。 |
| `explore_for` | scanning → selecting_frontier → navigating → scanning | 所找资源/类型、当前 frontier、实际到达区域/已搜范围、候选数及预算；找到可用结果或耗尽本次搜索预算才结束，不以 explore 进程曾启动为成功。 |
| `go_to_surface` | inspecting_column → clearing/ascending → settling → verifying_surface | 当前 Y、目标/最高已检查位置、已挖格/移动步、液体/顶部障碍；实际满足现有地表条件才成功。站稳等待与无路分开。 |

### 挖掘、拾取与放置（7 个）

| 工具 | 阶段 | 必要事实、等待与结束依据 |
| --- | --- | --- |
| `mine_block_collect` | approaching → equipping → breaking → awaiting_drop → picking_up → verifying | 精确坐标/预期方块、当前挖掘目标和同目标进度、reach/可见性（可读取时）、掉落身份、实际库存增量。方块清除和掉落入包分别记录；目标改变/无掉落/未拾取具体区分。 |
| `collect_block_domain` | discovering → approaching_cluster → breaking → picking_up → verifying | 本域/簇、当前目标、已确认清除数/库存增量、剩余需求及既有预算；单簇结束不重置整次累计。大簇按现有预算分段，状态设计不引入“必须整簇采完”。 |
| `dig_down` | inspecting → clearing → descending → settling → verifying_depth | 起始/当前/目标 Y、已确认挖掘数、步数/预算、脚下液体/危险事实；达到深度和站位核验或明确危险/无路失败。 |
| `dig_up` | inspecting → clearing → ascending → settling → verifying_height | 起始/当前/目标 Y、头顶目标、脚下支撑、已挖格/移动步、剩余预算；不能把抬头挥镐算成上升。 |
| `pickup_items` | locating_drops → approaching → awaiting_pickup → verifying | 目标 item/UUID、掉落数量、距离、拾取前后库存增量/空间；实体消失但未入包不能默认拾取成功。 |
| `place_block` | preparing_item → approaching → aiming → placing → awaiting_block → verifying | 目标坐标、预期方块、支撑面、选中物品/数量、实际方块类型/properties；等待服务端方块确认，不能以右键或库存减少替代落点核验。 |
| `place_here` | selecting_local_target → preparing_item → placing → verifying | 实际选定的邻近落点/支撑、选中物品、世界落点结果；落点不可用和材料不可用分别报告，不用玩家当前位置冒充放置坐标。 |

### 库存、合成、熔炼与交接（8 个）

| 工具 | 阶段 | 必要事实、等待与结束依据 |
| --- | --- | --- |
| `craft_item` | checking_recipe → preparing_station/grid → staging_inputs → awaiting_recipe → taking_output → verifying_batch → cleaning_grid/recovering_station → verifying | 产物/配方、请求与实际产出、当前批次、剩余原料、工作台位置、菜单 ID/revision、已确认输出/残留。等待新菜单/配方输出有具体依赖；分批和临时工作台回收都属于本次事务，子 craftItem 成功不是外层结束。 |
| `equip_item` | locating_item → staging_slot → selecting/equipping → verifying | item、源槽、目标手/装备位、实际持有/装备；已装备可立即成功，无物品/无交换空间/核验失败具体结束。 |
| `smelt_item` | locating/preparing_furnace → loading_input → loading_fuel → smelting → collecting_output → recovering_station → verifying | 炉坐标、输入/燃料/输出槽、实际可读 cook/burn progress、已产出/已取出/目标数、剩余输入/燃料；炉子正常加工是合法 waiting，不靠角色移动判卡死。配方、燃料、菜单或库存空间阻断按事实结束，保留尚在炉内的物品位置。 |
| `give_player` | locating_receiver → approaching → staging_item → dropping → awaiting_receiver_pickup → verifying | 收件人 UUID、距离、物品/请求数、各批次已丢/已由目标拾取数、掉落 UUID；只有该收件人拾取证据才算交付，发送方减库存不能替代。 |
| `consume_item` | checking_need → selecting_item → consuming → verifying_effect | 物品/使用 tick、消耗数、饱食度/生命/可读效果变化；满饱食按现有分支直接报告，不能伪造一次消耗。 |
| `discard_item` | locating_stacks → staging_slot → dropping → verifying | item、请求/实际丢出数、当前槽、库存差值；多组累计不重置，部分完成与全部完成分开，不能写成给玩家的 pickup 成功。 |
| `transfer_container_item` | locating/approaching_container → opening → reading_slots → transferring → awaiting_revision → verifying | 坐标、方向、物品、当前源/目标槽、菜单 ID/revision、请求/已确认转移数、剩余容量；只统计经两端槽位或库存验证的转移，满容/源变化/菜单变化保留部分成果。 |
| `clear_furnace` | locating/approaching → reading_slots → withdrawing → verifying_empty | 炉坐标、input/fuel/output 各槽前后数量、已取出数、背包空间；已空可立即成功，未取出的槽位及原因必须留在失败结果里。 |

### 世界交互与使用物品（9 个）

| 工具 | 阶段 | 必要事实、等待与结束依据 |
| --- | --- | --- |
| `go_to_bed` | locating_bed → approaching → interacting → verifying_sleep | 床坐标、当前时间、占用/合法性、实际 sleeping；已睡可立即成功，非夜晚/占用/未入睡分别报告。本工具不承诺一直睡到天亮。 |
| `set_openable_state` | reading_target → approaching → interacting → verifying_property | 门/活板门/栅栏门坐标、目标 open 值、实际前后值；已满足无需虚构交互。 |
| `till_farmland` | checking_soil → selecting_hoe → approaching → tilling → verifying_block | 坐标、原方块、锄具、上方空间/可读条件、实际 farmland；挥动完成不是耕地完成。 |
| `sow_crop` | checking_farmland → selecting_seed → approaching → planting → verifying_crop | 耕地/作物落点、种子种类/余量、实际作物类型；缺种、落点占用、土壤不适合分别报告。 |
| `harvest_and_resow` | inspecting_crop → harvesting → collecting → selecting_seed → replanting → verifying | 成熟度、原/新作物、已收获/拾取、补种是否完成、种子余量；收获成功但补种失败保留两段事实，不能合并成全部成功。 |
| `set_switch_state` | reading_target → approaching → interacting → verifying_property | 开关坐标、目标/实际 powered 或工具指定状态；按钮瞬时变化按真实采样时点表达，不承诺永久 powered。 |
| `use_item` | staging_item → aiming → using → verifying | 物品（含空手）、目标/视角、使用 tick、选中物品变化、可读库存/效果差值；持续使用结束只证明使用时长，结果依据由当前工具实际核验项决定。 |
| `use_on_entity` | locating_entity → approaching → selecting_item → using → verifying_entity | 选定 UUID、范围、使用物品、前后可读属性/库存差值；不能仅凭收到交互包宣称剪羊毛等效果已经发生。 |
| `use_on_block` | reading_target → approaching → selecting_item → using → verifying_block | 坐标、预期/实际方块及 properties、物品差值；保留工具既有 allow_unchanged 语义，明确 unchanged 是被允许还是验证失败。 |

### 战斗（2 个）

| 工具 | 阶段 | 必要事实、等待与结束依据 |
| --- | --- | --- |
| `engage_entity` | acquiring_target → approaching → aiming/attacking → cooldown → verifying_kill/disengaging | 目标 UUID/生命、距离、可读视线、自己生命、攻击冷却/实际伤害；低血撤退是明确结束分支。目标离开采样范围不等于击杀，反复挥手不等于命中。 |
| `ranged_attack` | acquiring_target → positioning → aiming → charging/loading → releasing → awaiting_damage → verifying | 目标 UUID/生命/距离、弓或弩、弹药数、使用/装填状态、已发射/实际伤害（有证据时）；蓄力/装填合法等待，无弹、目标丢失和未观察到伤害具体区分。 |

## 7 个客户端工具：逐项设计

原生过程的 active、paused、pathing 等是执行事实，不直接充当成功条件。没有上游可靠 API 的细阶段保持未知，只暴露确实可读的过程和世界事实；不为凑表格复制一套 Baritone 内部调度。

| 工具 | 阶段 | 必要事实、等待与结束依据 |
| --- | --- | --- |
| `mine_resource` | locating → pathing/mining → picking_up → verifying_quantity | 精确资源、起始/当前库存、**额外**采集目标及实际增量、可读挖掘目标/进程。与 collect_resource 的库存总量口径分开；采完附近资源却未满足数量不能按进程停下算完成。 |
| `build_structure` | preparing → building → paused/awaiting_block → verifying_cells | anchor/size/block、builder_active/paused、相关库存/快捷栏/可放置材料、已检查/匹配/总单元、当前层（可读时）。只有部分检查就注明部分；暂停合法 waiting。材料/放置准备问题不能靠 active 掩盖，完整匹配才 structure_verified。 |
| `clear_area` | preparing → clearing → verifying_cells | inclusive start/end、当前可读目标、已检查/air/总单元、目标块挖掘证据、清除范围；重启挖掘/换块不算累计成果。完整区域核验才 area_cleared。 |
| `farm_area` | scanning → approaching → harvesting/replanting → terminal | anchor/range、farm_active、成熟作物/种子/库存差值以及实际可读收获/补种证据；未全域扫描就不宣称“全部农田已完成”。当前原生停止证据与整片补种验证明确区分，状态接线不会自动增加全田验收能力。 |
| `elytra_travel` | preparing → taking_off → flying → approaching_destination → landing/verifying | 目标、实际位置/高度/速度（可读时）、on_ground/飞行状态、火箭余量、elytra_active/safe_to_cancel、到达距离；启动/加载/取消不等于真实抵达。现有真实飞行结果尚未验证；只有距离及落地条件实测满足才能报告到达。 |
| `read_body_processes` | sampling → terminal | 保留原生各进程、paused、pathing 等字段，增加同一 execution 摘要及采样时间；读的是当前/最近操作，不覆盖它，不把 all flags=false 单独当作 Python 事务结束。 |
| `build_schematic` | reading_blueprint → starting_builder → building → paused → verifying_cells | 文件/格式/原点、总单元、已检查/匹配数、少量不匹配、所需材料/当前可放置材料（可读时）；当前支持的 Sponge v2、零 offset 限制保留。builderBuildFile 只是启动控制回执，外层继续跟踪 native builder 及包含 air 的整图核验。 |

## 两个现有组合工具的边界

它们是已有的确定性组合，不把资源规划塞进 Body。外层复用同一快照，当前子工具和底层 action 只做关联，不让子工具抢掉父操作身份。

| 工具 | 阶段 | 暴露内容与结束依据 |
| --- | --- | --- |
| `collect_resource` | inspecting_inventory → discovering/exploring → collecting_cluster → verifying_total | item/材料族、**库存总量**目标、起始/当前数量、本次采得量、剩余数、当前子工具/簇、已有预算；保留短树连续采集等现有行为。子动作结束后继续保留整次累计，外层实际达到数量或返回具体阻断才结束。 |
| `ensure_tool_for` | inspecting_inventory/recipe → collecting_prerequisite → crafting → equipping → verifying | 请求资源、所需工具、当前前置物品/缺口、当前子工具、实际已获得/装备情况；第一项具体失败原样带出。最后核验工具取得与装备，不把已找到配方当作可开采。 |

## 直接 Body 动作和控制入口

这些不全是模型独立工具，但不能遗漏它们发起的过程。下表中的别名共用一个适配，不重复计算进度。

| 动作/别名 | 状态映射 |
| --- | --- |
| `navigate`, `moveTo`, `navigateTo` | 对应 move_to 的目标、路径和到达核验。 |
| `mineResource`, `baritoneMine` | 对应 mine_resource 的额外数量口径。 |
| `mineBlock`, `collectBlock` | 当前客户端映射为精确单格 clear；只承诺方块清除，掉落入包由外层 mine_block_collect 核验。 |
| `getToBlock`, `baritoneGetToBlock` | 对应接近指定方块的原生过程和范围结果。 |
| `explore`, `baritoneExplore` | 原生探索锚点、已读过程；找到指定资源由外层 explore_for 判断。 |
| `followEntity`, `baritoneFollow` | 对应目标身份、距离及原生跟随过程，不承诺无限服务。 |
| `pickupItems`, `baritonePickup` | 对应原生拾取过程、物品与实际库存增量。 |
| `farmArea`, `baritoneFarm` | 对应农作阶段与原生过程；未观测的整田结果不补写。 |
| `buildStructure`, `baritoneBuild` | 对应建造过程，完整单元验证属于外层。 |
| `clearArea`, `baritoneClear` | 对应清除过程，完整区域验证属于外层。 |
| `elytraTravel`, `baritoneElytra` | 对应飞行、取消安全状态和实际到达/落地事实。 |
| `engageEntity` | 近战阶段、目标伤害/击杀或撤退。 |
| `rangedAttack` | 远程阶段、弹药、装填/蓄力与伤害。 |
| `craftItem` | 单次/本动作批次的菜单阶段与产出；不声称临时工作站回收已完成。 |
| `containerTransfer` | 单次槽位转移、revision 和两端确认；外层累计总量。 |
| `furnaceTransfer` | 炉内对应槽位转移与确认；不冒充整个熔炼过程。 |
| `handoffItem` | 本批丢出、目标收件人拾取和身份确认。 |
| `ascend` | 地表检查、向上挖掘/移动、站稳及地表结果。 |
| `digDown` | 检查、清除、向下移动、深度/危险/预算。 |
| `digUp` | 检查、清除、向上移动、高度/支撑/预算。 |
| `useItem` | 原生使用阶段、选中物品、持续 tick 和可读结果。 |
| `placeBlock` | 准备、瞄准、放置、服务端方块确认。 |
| `collectBlocks` | 当前簇的 break/pickup、已确认清除/入包；不覆盖外层采集总目标。 |

控制回执只说明该控制实际应用；若它改变已有长操作，长操作的 execution 要随之更新，而控制调用不能提前终结或取代它。

启动控制还有一个特殊边界：直接调用 builderBuildFile 的既有 `control_applied` 回执仍表示“启动已应用”，不改成建筑验收终态。若有外层 build_schematic，execution 归外层；没有外层时，继续以启动 Action.id 关联原生 builder 的运行快照，直到该过程真实停止。控制回执和长过程快照在各自语义下表达，不能拿短回执清掉长过程或释放其物理占用。其他直接控制启动的原生长过程同理。

| 控制入口（含别名） | 状态与效果确认 |
| --- | --- |
| `baritoneSettings`, `setBaritoneSettings` | 短控制；回读实际应用的白名单 setting 值，不能用 settings 请求成功宣称在途任务完成。 |
| `builderPause`, `baritoneBuilderPause` | 原操作转 waiting，reason=显式暂停；确认 builder_paused。保持原 operation ID 和未完成格子。 |
| `builderResume`, `baritoneBuilderResume` | 确认恢复后原操作回 running；恢复回执不是建造结果。 |
| `builderOpenSchematic`, `baritoneBuilderOpenSchematic` | 打开选择界面/加载请求及实际加载结果；等待用户文件选择如实标记，不能声称建筑已建好。 |
| `builderOpenLitematic`, `baritoneBuilderOpenLitematic` | 指定 index、实际加载/拒绝结果；区分选图与施工。 |
| `builderBuildFile`, `baritoneBuilderBuildFile` | 文件/原点、启动回执和实际 builder 关联。控制调用可结束，关联的 builder 状态仍需暴露，不能短控制结束就抹去仍在施工的进程。 |
| `exploreFilter`, `baritoneExploreFilter` | 短控制；文件/invert 与实际加载结果，不伪造探索进度。 |
| `elytraRepack`, `baritoneElytraRepack` | repacking/实际 loaded、path size 等已读效果；打包完成不是飞行到达。 |
| `elytraReset`, `baritoneElytraReset` | reset 请求及实际 elytra 状态；reset 回执不能冒充安全取消确认。 |
| `getToBlockBlacklistClosest`, `baritoneGetToBlockBlacklistClosest` | 回读 blacklist 变化；原 get 过程保留身份，若实际重选目标则更新目标，不重置外层已确认成果。 |
| `builderSelectionAdd`, `baritoneBuilderSelectionAdd` | pos1/pos2 和回读 selection；只表示选区改变。 |
| `builderSelectionClear`, `baritoneBuilderSelectionClear` | 回读空 selection；不表示原世界区域已清空。 |
| `builderSelectionExpand`, `baritoneBuilderSelectionExpand` | direction/amount 和实际选区边界。 |
| `builderSelectionContract`, `baritoneBuilderSelectionContract` | direction/amount 和实际选区边界。 |
| `builderSelectionShift`, `baritoneBuilderSelectionShift` | direction/amount 和实际选区边界。 |
| `jump` | 原生起跳输入及可读 on_ground/位置变化；一次输入不是到达高处。 |
| `stop` | stopping；仅确认本操作相关过程/按键停止后 released，不能以命令接收代替停止。 |
| `selectItem` | item、实际选中槽/手持物；只确认选择。 |
| `selectSlot` | 槽号、实际选中槽/物品。 |
| `lookAt` | 目标、实际 yaw/pitch；不宣称目标已经可交互或攻击命中。 |
| `attackEntity` | 目标 UUID、实际攻击输入/可读伤害；单次攻击不等于完整 engage/击杀。 |
| `dropItem` | 源槽、请求/实际库存减少量及可读掉落；不声称收件人已拾取。 |
| `moveItem` | 源/目标槽与实际变化，等待必要槽位确认；不以点击回执替代。 |
| `igniteBlock` | 坐标/工具、实际点燃方块/状态；具体效果核验。 |
| `sowCrop` | 耕地/作物坐标、种子和实际作物状态。 |
| `interrupt`（Body 方法） | 关联被取消 operation，stopping → 已停止/cancelled；返回是否确认释放，不启动新目标。 |

`spawn/despawn` 在真实客户端后端未实现，生命周期归现有启动程序；`navigationMutationDecision` 是旧服务端入口；泛化 `baritoneCommand` 未作为当前 typed Body 能力开放。这些请求即时返回既有 unavailable/unsupported 事实，不能创建假的 running。IPC 调试命令若启动了未关联的原生进程，查询必须显示该进程事实及 operation 未知，不能显示为空闲。

### 直接 perception scopes

| scope | 观测要求 |
| --- | --- |
| `inventory` | 槽位/装备、分页口径及采样时间。 |
| `container` | 菜单身份/revision、槽位范围和采样时间；共享菜单互斥同上。 |
| `recipeData` | 配方和工作站信息，不代表库存满足。 |
| `findBlocks` | 候选、查询边界/分页/截断/已加载限制。 |
| `blockAt` | 精确方块/properties，未知不能写 air。 |
| `blockCells` | 实际读取单元数、范围/失败单元和采样时间；大区域分批，不重复全图逐秒扫描。 |
| `nearbyBlocks` | 区域、实际读数和截断信息。 |
| `nearbyEntities` | UUID、距离/必要属性、范围及采样时间。 |
| `nearbyHostiles` | 实际敌对过滤结果与采样范围。 |
| `baritoneState`, `processState` | 保留当前原生字段并暴露同一 execution；读取失败保留最后真实采样时间。 |
| `capabilities` | 本后端支持/不支持的真实能力；字段缺失不是运行中，也不为旧后端捏造细阶段。 |

## 接入位置与更新方式

按现有链路补连接，不新建状态服务：

1. **客户端事实。** `minecraft/client/body/.../MineBotClientBody.java` 在已有 Run/progress/state 中提供语义 phase、等待依赖及当前动作的少量事实。读取现有 tick、菜单 revision、挖掘目标、库存/世界结果；原生 Baritone 使用公开可读状态，未知细节不补写。阶段变化与终态立即更新；持续执行沿用约每 20 tick 的更新，不每 tick 推完整世界。
2. **传输和动作关联。** `minebot/game/real_client_ipc.py` 继续用现有 on_progress；`real_client_body.py` 接入回调，按 Action.id 更新最新快照，再保留已有终态。关联断线、迟到事件及 stopping。控制产生的长原生过程也要有关联，不能只观察 `_PendingAction` 是否存在。
3. **外层事务。** `minebot/body/` 的各事务在现有动作之间更新阶段、批次/数量和确认事实；合成/熔炼/菜单等待等 Python 过程不能由 Java 猜。对所有工具复用一个薄调用上下文；`runner.py` 的现有工具调用边界已有 call_id，执行 lane 把上下文带入 worker，组合工具传给子调用，结束/异常时清理绑定。不新增单独工作流引擎。
4. **读取出口。** `read_state` 和 `read_body_processes` 增加相同的 execution 摘要，保留旧字段；写工具最终 `ToolResult.metrics.execution` 带自己的最后状态和已确认部分成果。普通短读仍返回自己的观察来源/时间，不抢占主写操作快照。没有新 operation 时 execution 可为 null，这只说明无关联记录，实际空闲还需既有物理状态证据。
5. **复用已有记录。** 只保存最新活动/最近终态，完整过程沿用当前事件/trace；需要通知时沿用已有观察路径。不增加竞争消费 `poll_events` 的线程，不把每秒 progress 灌入模型历史，不为每次心跳发起模型请求。本轮不规定模型何时消费它。

Java/FakePlayer 和显式 legacy SDK 路径保持可用：摘要按已有事件、事务和结果补充，缺失细阶段明确省略/未知；不要求为字段对齐重写旧执行器。API key/Auth 的区别不进入 Body 状态字段，状态不依赖某个 provider 的异步或 steering 特性。

## 实现后的精简验证与停止条件

本轮只做设计和清单核对，不启动新实验。后续实现复用现有真实任务入口，围绕几个执行行为验收：

- 一次批量施工：能看到开始、真实格子变化、材料/槽位事实和最后核验；实际不能继续时明确原因、部分成果与控制状态，停止后可接下一动作。
- 一次跨子动作的菜单事务：合成或熔炼覆盖菜单确认、正常等待、产出、清理与外层终态；子动作结束不冒充整个工具结束。
- 一次进行中的取消，以及一次已知传输故障：取消确实停止；无法确认停止的故障如实 unknown。只读查询和迟到旧事件都不能改写当前操作身份。

其他工具在原有单能力任务执行时检查其对应字段，不再为 51 个工具各造一套 mock/全量 live 验收。只对新增公共生命周期/关联逻辑保留必要回归。停止条件为：上述真实行为通过、清单每项有实际实现映射、无已知的假空闲/假进度/假终态；记录没通过的具体调用和事实，不用文档完整度代替可用结果。

代码核对入口：[基础清单](minebot/app/body_tool_catalog.py)、[客户端工具](minebot/app/client_body_tools.py)、[基础工具绑定](minebot/app/phase1_runtime.py)、[扩展事务工具](minebot/app/body_capability_tools.py)、[客户端适配](minebot/game/real_client_body.py)、[IPC](minebot/game/real_client_ipc.py)、[Python 事务](minebot/body/)、[组合工具](minebot/brain/composition.py)。已有能力证据见 [BODY_VALIDATION.md](BODY_VALIDATION.md)，当前内核见 [RUNTIME.md](RUNTIME.md)。
