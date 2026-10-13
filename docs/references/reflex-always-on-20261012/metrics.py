"""Measured participation. Delivery events are not inferred model turns."""
from collections import Counter
import json
from pathlib import Path
import statistics
import sys


def merge(intervals):
    merged = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def summarize(path):
    rows = []
    for line in Path(path).read_text().splitlines():
        rows.append(json.loads(line))
    if not rows:
        raise ValueError('Empty trace')
    first, last = rows[0]['ts'], rows[-1]['ts']
    duration = last - first
    requests = [r for r in rows if r['event'] == 'call_supervision_request']
    answers = [r for r in rows if r['event'] == 'call_supervision_result']
    actions = {}

    def visit(value, timestamp):
        if isinstance(value, dict):
            action_id, elapsed = value.get('action_id'), value.get('elapsed_ms')
            if action_id and isinstance(elapsed, (float, int)) and elapsed >= 0:
                candidate = {'action_id': action_id, 'elapsed_s': elapsed / 1000,
                             'end': timestamp, 'kind': value.get('action_kind'), 'reason': value.get('reason')}
                if action_id not in actions or candidate['elapsed_s'] > actions[action_id]['elapsed_s']:
                    actions[action_id] = candidate
            for child in value.values():
                visit(child, timestamp)
        elif isinstance(value, list):
            for child in value:
                visit(child, timestamp)

    for row in rows:
        if row['event'] == 'tool_raw_result':
            visit(row.get('raw_result'), row['ts'])
    busy = merge([(max(first, a['end'] - a['elapsed_s']), min(last, a['end'])) for a in actions.values()])
    work = sum(end - start for start, end in busy)
    idle = []
    cursor = first
    for start, end in busy:
        if start > cursor:
            idle.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < last:
        idle.append((cursor, last))
    question_times = [r['ts'] for r in requests]
    longest = 0
    for start, end in idle:
        points = [start] + [t for t in question_times if start < t < end] + [end]
        longest = max(longest, max((b - a for a, b in zip(points, points[1:])), default=0))

    by_purpose = Counter(r.get('purpose') for r in requests)
    by_state = Counter(r.get('loop_state') for r in requests)
    choices = Counter(r.get('choice') for r in answers)
    milliseconds = [r['decision_ms'] for r in answers if isinstance(r.get('decision_ms'), (int, float))]
    deliveries = [r for r in rows if r['event'] in ('reflex_fact_wake', 'body_event_wake')]
    intents = [r for r in rows if r['event'] == 'runtime_intent_wake']
    wakes = [{'seq': r['seq'], 'event': r['event'], 'reason': r.get('reason'),
              'status': r.get('status'), 'kind': r.get('kind'), 'activity_id': r.get('activity_id'),
              'body_event': r.get('body_event', r.get('name'))} for r in deliveries]
    changes = [r for r in rows if r['event'] == 'reflex_state_changed']
    chains = [r for r in rows if r['event'] == 'reflex_reaction_chain']
    last_body = next((r for r in reversed(rows) if r['event'] == 'body_progress' and 'health' in r), {})
    death_events = [r for r in rows if r['event'] == 'reflex_death']
    respawns = [r for r in rows if r['event'] == 'reflex_respawned']
    death_records = []
    for row in death_events:
        facts = row.get('facts') or {}
        following = next((r for r in respawns if r['seq'] > row['seq']), None)
        native_start = facts.get('sampled_at_ms')
        native_end = (following.get('facts') or {}).get('sampled_at_ms') if following else None
        death_records.append({'seq': row['seq'], 'position': facts.get('position', facts.get('pos')),
                              'health': facts.get('health'), 'oxygen': facts.get('oxygen'),
                              'respawn_seq': following['seq'] if following else None,
                              'respawn_delay_s': following['ts'] - row['ts'] if following else None,
                              'native_respawn_delay_s': (native_end - native_start) / 1000
                              if isinstance(native_start, (int, float)) and isinstance(native_end, (int, float)) else None})
    tool_counts = Counter(r.get('tool') for r in rows if r['event'] == 'tool_invoke')
    return {'source': str(path), 'seq_end': rows[-1]['seq'], 'elapsed_s': duration,
            'requests_by_purpose': dict(by_purpose), 'requests_by_loop_state': dict(by_state),
            'requests_per_session_minute_by_purpose': {k: n * 60 / duration for k, n in by_purpose.items()} if duration else {},
            'requests_per_observed_body_work_minute_by_purpose': {k: n * 60 / work for k, n in by_purpose.items()} if work else None,
            'native_action_count': len(actions), 'observed_native_work_s': work, 'idle_remainder_estimate_s': duration - work,
            'timing_method': 'Union of native terminal elapsed_ms intervals, deduplicated by action_id; IPC receipt is the end timestamp. Idle remainder and no-question gap are estimates; missing native terminals are not counted as work.',
            'longest_estimated_idle_without_any_question_s': longest if actions else None,
            'answers': dict(choices), 'unknown_ratio': choices.get('unknown', 0) / len(answers) if answers else None,
            'decision_ms_median': statistics.median(milliseconds) if milliseconds else None,
            'agent_delivery_counts': dict(Counter(r['event'] for r in deliveries)), 'agent_deliveries': wakes,
            'runtime_intent_wakes': [{'seq': r['seq'], 'reason': r.get('reason'), 'kind': r.get('kind')} for r in intents],
            'wake_accounting': 'reflex_death is a death observation, not a wake. agent_start counts launches/turn entries, not delivery events. Distinct deliveries can be coalesced into a model turn.',
            'agent_start_count': sum(r['event'] == 'agent_start' for r in rows),
            'delegate_work': tool_counts.get('delegate_work', 0), 'read_reflex': tool_counts.get('read_reflex', 0),
            'state_switches': len(changes), 'reaction_chains': chains, 'deaths': death_records,
            'last_body': {k: last_body[k] for k in ('seq', 'pos', 'position', 'health', 'food', 'oxygen') if k in last_body}}


if __name__ == '__main__':
    print(json.dumps(summarize(sys.argv[1]), ensure_ascii=False, indent=2))
