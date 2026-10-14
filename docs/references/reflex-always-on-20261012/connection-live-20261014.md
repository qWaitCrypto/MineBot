# 原生断线停止与重新进服验证（2026-10-14）

已读核心理念、常在线计划5.1与第2/7/8步、题目约定15–17节。用户授权继续推进，留在codex/reflex-always-on，不合并、不推送。

## 根因与修改

旧353cb9d在服务器踢出角色后12.03秒仍发10道空闲题，没有loop_stopped。缺失状态假死亡已修，所以这次没有死亡通知；但是循环生命期没接断线事件。Java的DISCONNECT只停止原生动作、清ready，Python生命周期仅收death/respawned。

产品648e073接入既有原生body_event：world_disconnected/world_ready。循环断线时停止原消费任务、取消在途答案并清身体历史；保留同一生命周期监听。进服就绪时启动一个消费任务；快速重连等旧消费者结束再启动。Agent回合不能在断线时重开循环，身体调用返回server_disconnected，终态题返回unknown。仍走既有cancel_active_execution_with_facts，无新锁、持有者、停止入口或场景判断。题目措辞没有改。

## 测试

- 新增test_body_loop_connection.py三个测试：真实适配器事件去重；断线消费任务结束、期间不写、重连只有一个消费者；紧邻断线/重连及session close不误重开。
- 提取353cb9d的旧BodyLoop喂相同回归：2 failed、1 deselected。失败在断线后原消费者仍未结束。未改旧断言。
- 当前定向：pytest tests/unit/test_body_loop_connection.py tests/unit/test_body_loop.py tests/unit/test_body_loop_step6.py tests/unit/test_body_loop_terminal_frames.py tests/unit/test_direct_call_supervision.py tests/unit/test_real_client_body.py tests/unit/test_real_client_ipc.py -q；157 passed in 11.91s。
- Java :client:body:test BUILD SUCCESSFUL；JUnit XML实际15 tests、0 failures、0 errors、0 skipped。
- 本提交未跑全量；最近完整结果仍是旧7e4c1e6的2494 passed、1 skipped、291 subtests passed。不当648e073结果。

## 三次真实运行

仅25569/25579/8771、deepseek-v4.1-flash SDK、真实Decision与用户启动器。沿用人工高台世界与Step5NextBot；没有清包、供给、传送、生成危险。两个断线测试只用kick；重进服使用新客户端、新SDK会话、同一世界角色。不是自主生存或30分钟生活验收。

| 运行后缀 | 事实 |
|---|---|
| disconnect-check / 353cb9d | 踢出后12.03秒10题，无loop_stopped、无死亡；原循环生命期失败 |
| disconnect-fixed / 648e073 | 原生断线到Python停止2.55ms；seq9 loop_stopped，reason server_disconnected；观察12.03秒零请求、零死亡 |
| reconnect-check / 648e073 | seq6重新有idle题；Agent下发移动树；seq482计划completed，实际位置从[224.42,121,220.5]到[220.5,121,220.50]，health20，无死亡 |

上条进度中的“约8ms”是从发送命令前到停止的粗值；最终采用原生sampled_at_ms→Python trace ts为2.55ms，命令返回→停止2.13ms。两个时钟取自同一WSL实例，但仍含记录时延，不当精确网络延迟。

## Decision参与度

| 场景 | 请求（idle/running/plan_result） | 回答 | 实际Agent投递 |
|---|---|---|---|
| 旧断线 | 12/0/0 | stay12，中位127.44ms | 0 |
| 修复断线 | 1/0/0 | unknown1：断线作废在途答案，16.85ms为取消耗时 | 0 |
| 重进服 | 10/2/1 | stay8、keep_going1、next_step1、unknown2；中位128.70ms包含取消项 | 0 |

重进服delegate_work2次：第一次拒绝、第二次接受，逐条见下方真实结果。read_reflex0。seq483完成事实入队，夹具看到计划completed即quit，未等到投递，所以不能称Agent已接收到完成通知。原生移动回执elapsed890ms，监督调用区间1.07s；区间内题数及各用途每分钟数见metrics.json。断线场没有Body工作，不能算工作每分钟率。重进服短场不作长期节奏结论。

## 未验证与交付

实际验证了服务器踢出时停止、新客户端重进同世界继续完成移动。**同一客户端同一SDK会话内重连仅有定向测试，还没有游戏实测**；IPC进程硬崩溃、工作/保命动作中断线的真实结算、30分钟生活仍未验证。没有扩大本次证据范围。

完整trace路径为 /home/qwait/MineBot-always-on/logs/reflex-always-on-20261014-{disconnect-check,disconnect-fixed,reconnect-check}/trace.jsonl。三场launcher exit0，owned客户端/Agent已退出、owned服务器RCON stop，测试端口释放。证据、脚本、测试日志在evidence-connection-20261014。原启动脚本沿用装备实验scope描述，分析文件summary.json和本报告明确纠正本次实际场景；不把模板scope当发生过的干预。

真实delegate结果：

```json
[
  {
    "seq": 203,
    "success": false,
    "reason": "missing_intent"
  },
  {
    "seq": 379,
    "success": true,
    "reason": "accepted"
  }
]
```
