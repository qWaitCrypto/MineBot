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
    path = Path(path)
    if path.is_dir():
        path = path / 'trace.jsonl'
    rows = []
    for line in path.read_text().splitlines():
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
    # Some native-backed tools (notably look-at) return no native elapsed_ms.
    # Their admitted call duration is observable, but is not movement time.
    call_intervals = merge([
        (max(first, row['ts'] - row['elapsed_s']), min(last, row['ts']))
        for row in rows if row['event'] == 'call_supervision_finished'
        and isinstance(row.get('elapsed_s'), (int, float)) and row['elapsed_s'] >= 0
    ])
    supervised_s = sum(end - start for start, end in call_intervals)
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
    during_work = Counter(r.get('purpose') for r in requests
                          if any(start <= r['ts'] <= end for start, end in busy))
    by_state_purpose = Counter((r.get('loop_state'), r.get('purpose')) for r in requests)
    minute_counts = {}
    for row in requests:
        minute = int((row['ts'] - first) // 60)
        key = f"{row.get('loop_state')} / {row.get('purpose')}"
        minute_counts.setdefault(minute, Counter())[key] += 1
    choices = Counter(r.get('choice') for r in answers)
    milliseconds = [r['decision_ms'] for r in answers if isinstance(r.get('decision_ms'), (int, float))]
    deliveries = [r for r in rows if r['event'] in ('reflex_fact_wake', 'body_event_wake')]
    intents = [r for r in rows if r['event'] == 'runtime_intent_wake']
    wakes = [{'seq': r['seq'], 'event': r['event'], 'reason': r.get('reason'),
              'status': r.get('status'), 'kind': r.get('kind'), 'activity_id': r.get('activity_id'),
              'body_event': r.get('body_event', r.get('name'))} for r in deliveries]
    changes = [r for r in rows if r['event'] == 'reflex_state_changed']
    state_intervals = []
    current_state, state_started = 'idle', first
    for row in changes:
        state_intervals.append((current_state, state_started, row['ts']))
        current_state, state_started = row.get('to'), row['ts']
    state_intervals.append((current_state, state_started, last))
    state_seconds = Counter()
    longest_idle_state_gap = 0
    for state, start, end in state_intervals:
        state_seconds[state] += max(0, end - start)
        if state == 'idle':
            points = [start] + [t for t in question_times if start < t < end] + [end]
            longest_idle_state_gap = max(longest_idle_state_gap,
                max((b - a for a, b in zip(points, points[1:])), default=0))
    gaps = [{'before_seq': a['seq'], 'after_seq': b['seq'], 'seconds': b['ts'] - a['ts']}
            for a, b in zip(requests, requests[1:])]
    default_requested = [r for r in rows if r['event'] == 'call_supervision_default_requested']
    default_finished = [r for r in rows if r['event'] == 'call_supervision_default_finished']
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
            'requests_by_state_and_purpose': {f'{state} / {purpose}': n
                for (state, purpose), n in by_state_purpose.items()},
            'requests_per_session_minute_by_loop_state': {k: n * 60 / duration
                for k, n in by_state.items()} if duration else {},
            'requests_per_minute_buckets': [{'minute_from_trace_start': minute,
                'observed_seconds': min(60, duration - minute * 60),
                'counts_by_state_and_purpose': dict(counts)}
                for minute, counts in sorted(minute_counts.items())],
            'loop_state_seconds': dict(state_seconds),
            'requests_per_session_minute_by_purpose': {k: n * 60 / duration for k, n in by_purpose.items()} if duration else {},
            'requests_per_observed_body_work_minute_by_purpose': {k: n * 60 / work for k, n in by_purpose.items()} if work else None,
            'requests_during_observed_body_work_by_purpose': dict(during_work),
            'requests_during_observed_body_work_per_minute_by_purpose':
                {k: n * 60 / work for k, n in during_work.items()} if work else None,
            'rate_accounting': 'Session-normalized rates include all requests. Total requests divided by observed work time measures overhead, not work cadence. The during-work rate restricts the numerator to requests inside observed native work intervals.',
            'native_action_count': len(actions), 'observed_native_work_s': work,
            'idle_remainder_estimate_s': duration - work if actions else None,
            'supervised_body_call_s': supervised_s,
            'outside_supervised_body_calls_s': duration - supervised_s,
            'supervised_call_timing_method': 'Union of completed supervised-call intervals; includes tool execution and its settlement, not proof of physical movement. Native terminals missing elapsed_ms are excluded from native work time, never treated as proof that the Body was idle.',
            'timing_method': 'Union of native terminal elapsed_ms intervals, deduplicated by action_id; IPC receipt is the end timestamp. Idle remainder and no-question gap are estimates; missing native terminals are not counted as work.',
            'longest_estimated_idle_without_any_question_s': longest if actions else None,
            'longest_idle_state_without_any_question_s': longest_idle_state_gap,
            'idle_state_gap_method': 'idle is the loop state, and may contain life movement. This measurement excludes dead/work intervals but includes session startup.',
            'largest_request_gaps': sorted(gaps, key=lambda r: r['seconds'], reverse=True)[:10],
            'default_actions_requested': dict(Counter(r.get('choice') for r in default_requested)),
            'default_actions_finished': dict(Counter(r.get('choice') for r in default_finished)),
            'action_accounting': 'Answers are semantic selections, default_requested is an attempted action dispatch, and native_action_count is observed execution. They are not interchangeable.',
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
