import json, sys
R = sys.argv[1]
rows = [json.loads(l) for l in open(R + '/trace.jsonl') if l.startswith('{')]
t0 = rows[0]['ts']
rel = lambda r: round(r['ts'] - t0, 1)

print('== protection facts and what the Agent did next')
for i, r in enumerate(rows):
    if r.get('event') == 'reflex_fact_queued' and str(r.get('activity_id', '')).startswith('protection-'):
        print(rel(r), r['seq'], 'queued', r.get('activity_id'), r.get('status'))
        shown = 0
        for s in rows[i + 1:]:
            if s['ts'] - r['ts'] > 90:
                break
            e = s.get('event')
            if e == 'reflex_fact_wake' and str(s.get('activity_id', '')).startswith('protection-'):
                print('   ', rel(s), s['seq'], 'wake', s.get('activity_id'))
            elif e in ('assistant_message', 'model_tool_call') and shown < 4:
                shown += 1
                print('   ', rel(s), s['seq'], e, s.get('tool') or '', (s.get('content') or s.get('arguments_summary') or '')[:200])

print('== craft running frames: inventory as shown')
for r in rows:
    if (r.get('event') == 'call_supervision_request' and r.get('purpose') == 'running_frame'
            and r.get('tool') == 'craft_item'):
        st = r['request']['state']
        print(rel(r), r['seq'], st.get('current_work', '')[:60], '|', st.get('inventory', '')[:160])
for r in rows:
    if r.get('event') == 'tool_raw_result' and r.get('tool') == 'craft_item':
        res = r.get('raw_result') or {}
        print(rel(r), r['seq'], 'craft result', res.get('success'), res.get('reason'), json.dumps(r.get('arguments'))[:80])

stops = [r for r in rows if r.get('event') == 'call_supervision_result' and r.get('purpose') == 'running_frame'
         and r.get('choice') in ('next_step', 'call_agent')]
print('== running stops', [(r['seq'], r.get('tool'), r.get('choice')) for r in stops])
print('deaths', sum(r.get('event') == 'reflex_death' for r in rows),
      '| completed', any(r.get('event') == 'session_goal_completed' for r in rows))
