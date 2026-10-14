# 第 2 步：运行中「做完没有」单独问（Claude 接手，2026-10-14）

Codex 已停手，用户让 Claude 接手。分支 `codex/reflex-always-on`，提交 `ae4318a`，未合并、未推送。Codex 的历史报告原样保留。约定写在 `~/MineBot/docs/design-docs/reflex-frame-question-20261012.md` §18。

## 问题

`4b83331` 的真实运行（空背包 → 石镐并装备 → 12 块煤）里，运行帧选了 4 次 `next_step`，其中 3 次误停：

| 回答 seq | 调用 | 当时的事实 |
|---|---|---|
| 502 | 采原木 | 原木 3/5 |
| 1983 | 合成木镐 | 背包有木镐，主手还拿着树苗 |
| 3916 | 采煤 | 煤 3/12 |

任务最后完成，是 Agent 自己补救的。另一处要记下：seq 1984/1990/1994 交接记录里的 `phase=breaking`、`no_progress_s=0`，是回答之后才取的事实。Decision 实际看到的题是 seq 1978，题里 phase 为空，「7.9 秒前有进展」。

## 改了什么（用户确认：只改运行帧）

- 有意图的运行帧，q 里不放 `next_step`，同一请求另问 `a`：事实是否已经显示意图的每一部分。读答案时先看危险题；`a` 答 `shown` 就做下一步，否则按 q。
- 主手不再写在身体句子里，单独作为一条 `main_hand` 事实，放在背包后面。
- 结果题的「报告已达成」措辞改回「告诉 Agent 这件事做完了」（约定 §1）。

## 证据

- **离线**：113 个运行帧逐字还原，原答案复现 109 帧，另外 4 帧原记录里没有有效回答。意图未满足时误答「已显示」：原结构 80/1092，新结构 **0/1092**。正确停下的帧仍然停下。线上出题代码和试验请求逐字一致，113/113。
- **回归**：
  - `player-handler-fixed`：x 205.8 那帧 12/12 继续；到点那帧 12/12 下一步。中断条件「主手为空并且背包有石剑」成立的帧，大多数帧从选继续变成 12/12 选中断（原本是已知的漏判），只有 seq 785 一帧两边概率接近。
  - 结果帧：计划结果帧不变。直接调用结果帧有 3 帧偏向叫 Agent，执行路径相同。
  - 空闲帧：6 种形态，多数答案不变；只有一帧原本就在回家和待着之间摇摆，新写法 12/12 回家。
- **单元测试**：受影响的定向测试 111 passed；全量 `tests/unit` **2536 passed, 1 skipped, 291 subtests**，exit 0（`logs/final-suite-intent-question/`）。
- 脚本：`bench/step2-done-wording-trial.py`、`step2-done-question-trial.py`、`step2-done-regression.py`、`step2-idle-mainhand-regression.py`；输出 `logs/step2-done-*-20261014.*`。

## 真实验收（无人值守，进行中）

- 监工脚本 `logs/supervise-step2-intent-question.py`，日志 `logs/supervise-step2-intent-question.log`。
- 17:58 起跑。任务、模型、端口和上次相同，世界是新的，seed 同为 1。没有人工干预。
- 收尾时机：任务完成后再等 2 分钟；最长 40 分钟。只结束它自己起的进程。
- 结果在 `logs/reflex-always-on-20261014-step2-intent-question/analysis.json`。`stops_requested` 里每一次停下都要人工对照意图、背包和主手核对，**零误停才算这一项通过**。
- trace 保留。

## 仍未做（第 2 步其余验收）

- Agent 思考时灌水自救，工作中灌水后回到原工作，上浮受阻。这三项要在 `ae4318a` 上重新做真实验收，旧证据来自更早的提交。
- 危险题和 `a` 同时出现的帧没有真实样本，只有单元测试。
- 移动类意图在上浮途中（y=67），`a` 有 6/12 答「已显示」，未标注，留待观察。
- 第 3–8 步的未过项见 `summary.md`，没有动。
