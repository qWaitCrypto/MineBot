"""The online compiler and unmodified new premature-stop frame."""
from pathlib import Path
root = Path('/home/qwait/MineBot-always-on')
prefix = (root / 'logs/interrupt-condition-trial.py').read_text().split('async def main():')[0]
exec(compile(prefix, __file__, 'exec'))

async def main():
    rows = [json.loads(line) for line in (root / 'logs/reflex-always-on-20261014-player-handler-fixed/trace.jsonl').read_text().splitlines()]
    output = root / 'logs/patrol-early-stop-replay-5703f3f.jsonl'
    client = DecisionClient(url='http://117.176.14.165:10004/v1/systemone')
    report = []
    try:
        with output.open('x') as sink:
            for seq in (862,):
                row = next(r for r in rows if r['seq'] == seq)
                request = compile_online(row)
                previous = next(r['choice'] for r in rows if r['seq'] > seq
                    and r['event'] == 'call_supervision_result' and r.get('tool_call_id') == row['tool_call_id']
                    and r.get('purpose') == row['purpose'])
                answer = await client.ask(request)
                choice = rq.frame_choice(answer.raw, request['questions'])
                item = {'seq': seq, 'recorded': previous, 'baseline': choice, 'expected': 'keep_going',
                        'compiled_from_raw_frame': True, 'request_equal': True}
                report.append(item)
                sink.write(json.dumps({'seq': seq, 'variant': 'baseline', 'request': request,
                    'response': answer.raw, 'choice': choice}, ensure_ascii=False) + '\n')
                sink.flush()
                if choice != previous:
                    item['status'] = 'baseline_not_reproduced; no further comparison'
                    break
                opts = request['questions']['q']['criteria']
                counts = Counter()
                for order in list(itertools.permutations(opts))[:12]:
                    candidate = copy.deepcopy(request)
                    candidate['questions']['q']['criteria'] = {key: opts[key] for key in order}
                    answer = await client.ask(candidate)
                    choice = rq.frame_choice(answer.raw, candidate['questions'])
                    counts[choice] += 1
                    sink.write(json.dumps({'seq': seq, 'variant': 'permutation', 'request': candidate,
                        'response': answer.raw, 'choice': choice}, ensure_ascii=False) + '\n')
                    sink.flush()
                item['distribution'] = dict(counts)
    finally:
        await client.close()
        output.with_suffix('.summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False), flush=True)

asyncio.run(main())
