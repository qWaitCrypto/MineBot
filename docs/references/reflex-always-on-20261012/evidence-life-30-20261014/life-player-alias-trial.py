"""Offline alias candidate; never mutates the live process or product source."""
import asyncio
import copy
import itertools
import json
import random
from collections import Counter
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

root = Path('/home/qwait/MineBot-always-on')
sys.path.insert(0, str(root))
from minebot.app.body_loop import BodyLoop
from minebot.app import reflex_question as rq
from minebot.app.decision_client import DecisionClient


def players_from_observation(facts):
    # Native state and the existing perception API name the same entity list
    # differently. An explicit empty list is still an observation.
    for key in ('nearby_entities', 'entities', 'entities_near'):
        entities = facts.get(key)
        if isinstance(entities, (list, tuple)):
            return [item for item in entities if isinstance(item, dict)
                    and str(item.get('entity_type', item.get('type', ''))).removeprefix('minecraft:') == 'player']
    return []


async def main():
    run = root / 'logs/reflex-always-on-20261014-life-30-final-v2'
    rows = [json.loads(line) for line in (run / 'trace.jsonl').read_text().splitlines()]
    output = root / 'logs/life-player-alias-trial-0a2840b.jsonl'
    client = DecisionClient(url='http://117.176.14.165:10004/v1/systemone')
    reports = []
    try:
        with output.open('x') as sink:
            for seq in (490, 915):
                row = next(r for r in rows if r['seq']==seq)
                facts = row['compiler_input']['facts']
                loop = BodyLoop(SimpleNamespace(decision_client=SimpleNamespace()))
                baseline = loop._idle_question(facts, loop._idle_options(facts))
                assert baseline == row['request']
                response = await client.ask(baseline)
                chosen = rq.frame_choice(response.raw, baseline['questions'])
                previous = next(r['choice'] for r in rows if r['seq']>seq and r['event']=='call_supervision_result'
                    and r.get('tool_call_id')==row['tool_call_id'])
                item = {'seq':seq,'recorded':previous,'baseline':chosen,'candidate_only':True}
                reports.append(item)
                sink.write(json.dumps({'seq':seq,'variant':'baseline','request':baseline,
                    'response':response.raw,'choice':chosen},ensure_ascii=False)+'\n')
                sink.flush()
                if chosen != previous:
                    item['status']='baseline_not_reproduced'
                    print(json.dumps(item),flush=True)
                    break
                with patch.object(rq,'nearby_players',players_from_observation):
                    request = loop._idle_question(facts, loop._idle_options(facts))
                criteria = request['questions']['q']['criteria']
                orders = list(itertools.permutations(criteria))
                random.Random(seq).shuffle(orders)
                counts = Counter()
                for index, order in enumerate((orders*12)[:12]):
                    candidate = copy.deepcopy(request)
                    candidate['questions']['q']['criteria']={key:criteria[key] for key in order}
                    response = await client.ask(candidate)
                    chosen = rq.frame_choice(response.raw,candidate['questions'])
                    counts[chosen]+=1
                    sink.write(json.dumps({'seq':seq,'variant':index,'request':candidate,'response':response.raw,
                                           'choice':chosen},ensure_ascii=False)+'\n')
                    sink.flush()
                item['distribution']=dict(counts)
                item['added_facts']=request['state'].get('players')
                item['option_keys']=list(criteria)
                print(json.dumps(item,ensure_ascii=False),flush=True)
    finally:
        await client.close()
        output.with_suffix('.summary.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2))

asyncio.run(main())
