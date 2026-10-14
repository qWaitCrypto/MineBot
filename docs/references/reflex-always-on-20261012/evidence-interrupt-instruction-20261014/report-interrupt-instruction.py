import importlib.util
import json
from pathlib import Path
import shutil

root = Path('/home/qwait/MineBot-always-on')
docs = Path('/home/qwait/MineBot/docs/references/reflex-always-on-20261012')
run = root / 'logs/reflex-always-on-20261014-interrupt-instruction-fixed'
rows = [json.loads(line) for line in (run / 'trace.jsonl').read_text().splitlines()]
spec = importlib.util.spec_from_file_location('metrics', docs / 'metrics.py')
metrics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metrics)
summary = metrics.summarize(run)
(run / 'metrics.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
evidence = docs / 'evidence-interrupt-instruction-20261014'
evidence.mkdir(exist_ok=True)
for name in ('interrupt-condition-trial.py', 'interrupt-condition-trial-648e073.jsonl',
             'interrupt-condition-trial-648e073.summary.json', 'interrupt-online-trial.py',
             'interrupt-online-trial.jsonl', 'interrupt-online-trial.summary.json',
             'interrupt-online-trial-v2.jsonl', 'interrupt-online-trial-v2.summary.json',
             'interrupt-instruction-tests.log', 'start-interrupt-instruction-fixed.py',
             'control-interrupt-instruction-fixed.py', 'inspect-handler-result.py',
             'inspect-interrupt-live.py', 'report-interrupt-instruction.py'):
    shutil.copy2(root / 'logs' / name, evidence / name)
for name in ('trace.jsonl', 'metrics.json', 'fixture-events.jsonl', 'acceptance-run.json', 'launcher-returncode.json'):
    shutil.copy2(run / name, evidence / name)
request = next(r for r in rows if r['event'] == 'call_supervision_request'
               and r.get('purpose') == 'plan_result_frame' and r.get('tool') == 'equip_item')
(evidence / 'handler-result-frame.json').write_text(json.dumps(request, ensure_ascii=False, indent=2))
counts = {key: summary[key] for key in ('elapsed_s', 'requests_by_purpose', 'requests_per_minute_session_by_purpose',
    'answers', 'decision_ms_median', 'agent_delivery_counts', 'agent_deliveries', 'delegate_work', 'read_reflex',
    'supervised_body_call_s', 'observed_native_work_s', 'requests_during_observed_body_work_per_minute_by_purpose') if key in summary}
counts['session_requests_per_minute_by_purpose'] = {
    key: count * 60 / summary['elapsed_s'] for key, count in summary['requests_by_purpose'].items()}
report = '''# interrupt_when 条件指令与真实handler（2026-10-14）

已读核心理念、常在线计划第6步与工作语言中断执行路径。用户授权继续推进、后面统一处理遗留问题。仍在codex/reflex-always-on，不合并、不推送。

## 修改与离线

48fa6f8只补问句中的Agent条件指令：The Agent also says: when “<条件原话>”, interrupt the current work and do “<handler意图>”. 只在运行题真的含interrupt选项时出现。事实、选项原文与顺序、危险题分流、结果题、无中断计划全部不变。程序没有判断条件真假、没有硬触发、次数规则或新概率门。

原step6-next三个真实帧，恢复原始事实/时钟/ambient history，经线上编译得到原题逐字相等，按线上键顺序原回答3/3复现continue。298帧仍持木剑、条件不成立；475/563帧空手且背包有石剑、条件成立。

| 写法 | seq298（继续） | seq475（中断） | seq563（中断） |
|---|---|---|---|
| 旧题基线 | continue | continue（漏中断） | continue（漏中断） |
| 离线增加条件指令 | continue11、unknown1 | interrupt12 | interrupt12 |
| 离线另问条件成立否（未上线） | continue12 | interrupt12 | interrupt12 |
| 当前线上编译函数 | continue12 | interrupt12 | interrupt12 |

unknown保留原响应None；这一版脚本没有存客户端异常原因，不能断言是服务超时，也不把它合进其他选择。离线两种候选都有原始请求和回答，不只报最好的一种。上线选最小改法：现有问句明确Agent条件，不新增问题和路由。最后36/36不是整计划验收；只覆盖这三个真实帧。

线上回放第一次旧模块接口不兼容（frame_questions不接受新interrupt关键字），0请求；原空输出与错误状态保留。v2包装旧签名只丢弃新关键字，旧题仍逐字相等后才问服务；结果36/36。

定向pytest test_body_loop_step6、test_reflex_question、test_direct_call_supervision、test_body_loop、test_work_runtime、test_body_loop_connection：233 passed in 17.36s。新增测试只验证Agent原话进入问句、原选项不变、无选项时不加指令。没有改旧断言。最新源码尚未跑全量；旧7e4c1e6全量结果不冒充本提交。

## 真实运行：未通过完整验收

运行reflex-always-on-20261014-interrupt-instruction-fixed，48fa6f8，deepseek-v4.1-flash SDK与真实Decision，25569/25579/8771。沿用人工高台世界；夹具传送起点、清包、给木剑/备用石剑，运行中移除主手木剑。没有生成怪物或直接调用delegate/装备；计划、条件、handler由真实Agent写，夹具只改变身体事实。

- seq479：Agent计划的首个移动节点正在运行，interrupt已在选项中。
- seq488/496/504/597：已空手、有石剑，仍continue；不是缺事实。首节点结束选next_step。
- seq694/697：第二节点第一题选interrupt。距夹具移除主手约6秒，之前有4道含正确事实的continue，不能称立即发现。
- seq710 handler_started；seq712 equip_item真实成功，seq714主手已是stone_sword。
- **seq718装备结果题、seq722 call_agent，seq723原计划blocked（interrupt_handler_blocked）。没有handler_finished、没有resume、没有完成三站。**
- delegate_work两次：seq222 invalid_plan，seq399 accepted。seq728实际Agent投递blocked；之后Agent调用read_reflex一次。最终坐标[248.56,121,220.50]、主手石剑槽0、health20，无死亡。

这次仅验证中断真的被选、handler写动作真的执行；恢复原任务失败，不能报第6步通过。

## 新结果题缺口（先记录）

完整seq718题面保存在handler-result-frame.json。handler的意图是“装备背包石剑到主手，之后继续巡视”；事实明确主手石剑。可是next_step文字是“Do not repeat it and report to the Agent that this work is finished.”，没有给出它位于中断handler、之后还有原巡视这一结构上下文。_handle_interrupt只有子树先被判done才会到resume结果题，所以这次还没走到那题。

位置：work_runtime._call_plan仅取node.next_call；_handle_interrupt复制并执行handler时没有传挂起工作的后续上下文；reflex_question.frame_options无next_call时写最终报告Agent。这里只确定上下文缺口，不把一次call_agent直接归因为模型或声称已修。保留原题，留作下一轮离线与结构验证。

## 参与度与交付

详细按用途/分钟、Agent投递逐条如下；metrics.json保留原生可观测工作区间和局限。不同流程变化是“中断巡视→装备”与“handler结果交Agent”两件；同类重复不增加次数。Agent调用read_reflex一次，不报零；它在blocked通知之后，不是工作的持续进度轮询。机器人存活，巡视未完成。

owned会话/client已退出、服务器RCON stop。trace完整保留在上述运行目录和提交证据中。下一项仍在授权范围内；本报告不标整体目标完成。
'''
report += '\n```json\n' + json.dumps(counts, ensure_ascii=False, indent=2) + '\n```\n'
(docs / 'interrupt-instruction-live-20261014.md').write_text(report)
summary_path = docs / 'summary.md'
text = summary_path.read_text().replace('产品最新 `648e073`', '产品最新 `48fa6f8`')
text = text.replace('| 未过 | interrupt handler真实选择、',
    '| 中断 | 三帧线上离线36/36；真实选中interrupt并装备，但handler结果call_agent，未恢复巡视；233定向通过 |\n| 未过 | interrupt handler恢复、')
text = text.replace('断线报告：', '中断报告：[条件指令与真实handler](interrupt-instruction-live-20261014.md)。断线报告：')
summary_path.write_text(text)
step = docs / 'step-8.md'
text = step.read_text().replace('当前源码648e073', '当前源码48fa6f8')
text = text.replace('| 未完成 | 工作中断线/IPC硬断、中断handler',
    '| 中断 | 选中并装备、随后blocked，无resume；233定向通过 |\n| 未完成 | 工作中断线/IPC硬断、中断handler恢复')
step.write_text(text)
target = root / 'docs/references/reflex-always-on-20261012'
for name in ('interrupt-instruction-live-20261014.md', 'summary.md', 'step-8.md'):
    shutil.copy2(docs / name, target / name)
shutil.copytree(evidence, target / evidence.name, dirs_exist_ok=True)
print(json.dumps(counts, ensure_ascii=False, indent=2))
