"""Online compiler, native idle frames, real Decision; no gameplay writes."""
import asyncio, copy, itertools, json, random, sys
from collections import Counter
from pathlib import Path
import argparse
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--workspace', type=Path, default=Path.home() / 'MineBot-always-on')
parser.add_argument('--run-dir', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
settings = parser.parse_args()
sys.path.insert(0, str(settings.workspace))
from minebot.app import reflex_question as rq
from minebot.app.decision_client import DecisionClient
root=settings.workspace
run=settings.run_dir
output=settings.output

def rows():
    result=[]
    for line in (run/'trace.jsonl').read_text().splitlines():
        try: result.append(json.loads(line))
        except ValueError: pass
    return result

async def main():
    data=rows()
    frames=[r for r in data if r['event']=='call_supervision_request' and r.get('purpose')=='idle_frame']
    continuous=[]
    for frame in frames:
        facts=frame['compiler_input']['facts']
        suitable=(facts.get('time',24000)%24000<12000 and not rq.nearby_players(facts)
                  and not rq.protective_options(facts) and not facts.get('agent_thinking'))
        if suitable and (not continuous or frame['ts']-continuous[-1]['ts']<3): continuous.append(frame)
        else: continuous=[]
        if len(continuous)>=60: break
    if len(continuous)<60: raise RuntimeError(f'Only {len(continuous)} consecutive calm daytime idle frames')
    visitor=next((r for r in frames if rq.nearby_players(r['compiler_input']['facts'])),None)
    summaries=[]
    client=DecisionClient(timeout_s=2)
    async def ask(sink,identity,source,facts,options):
        request=rq.idle_question(facts,options)
        answer=await client.ask(request)
        choice=rq.frame_choice(answer.raw,request['questions'])
        sink.write(json.dumps({'id':identity,'source_seq':source['seq'],'facts':facts,'request':request,
                               'choice':choice,'response':answer.raw,'elapsed_s':answer.elapsed_s},ensure_ascii=False)+'\n');sink.flush()
        return choice
    try:
        with output.open('x') as sink:
            # Reproduce the original online request before applying sequential
            # choices. Online trace stores keys sorted; rebuild with compiler.
            first=continuous[0]
            facts=copy.deepcopy(first['compiler_input']['facts'])
            baseline_request=rq.idle_question(facts,first['compiler_input']['options'])
            assert baseline_request==first['request']
            baseline=await ask(sink,'baseline',first,facts,first['compiler_input']['options'])
            next_request=next((r['seq'] for r in data if r['event']=='call_supervision_request'
                               and r.get('tool_call_id')==first.get('tool_call_id')
                               and r['seq']>first['seq']), float('inf'))
            paired=[r for r in data if r['event']=='call_supervision_result'
                    and r.get('purpose')=='idle_frame'
                    and r.get('tool_call_id')==first.get('tool_call_id')
                    and first['seq']<r['seq']<next_request]
            assert len(paired)==1, 'Missing or ambiguous recorded idle answer'
            recorded=paired[0]['choice']
            summaries.append({'id':'baseline','choice':baseline,'recorded':recorded,'matched':baseline==recorded})
            if baseline!=recorded: raise RuntimeError('Original baseline answer changed; stop comparison')
            previous='stay';switches=0;counts=Counter()
            for index,frame in enumerate(continuous):
                facts=copy.deepcopy(frame['compiler_input']['facts'])
                prior_action=frame['compiler_input']['options'].get(previous)
                # Previous answer supplies the following frame's continue
                # option, even though this is an offline selection experiment.
                options=rq.idle_options(facts,current=prior_action if previous!='stay' else '')
                choice=await ask(sink,f'sequential_{index}',frame,facts,options)
                if choice=='keep_going': choice=previous
                if choice!='unknown':
                    if choice!=previous:switches+=1
                    previous=choice
                counts[choice]+=1
            summaries.append({'id':'sixty_daytime_frames','first_seq':continuous[0]['seq'],
                              'last_seq':continuous[-1]['seq'],'switches':switches,'choices':dict(counts),
                              'passed':switches<=6})
            # Time/distance comparisons explicitly modify ONE recorded fact,
            # as requested by Step7; these are not native scenario captures.
            control=continuous[-1]
            for label,clock in [('day',6000),('dusk',12500),('night',18000)]:
                facts=copy.deepcopy(control['compiler_input']['facts']);facts['time']=clock
                options=rq.idle_options(facts);counts=Counter()
                orders=list(itertools.permutations(options))
                random.Random(7).shuffle(orders)
                for index,order in enumerate((orders*12)[:12]):
                    counts[await ask(sink,f'time_{label}_{index}',control,facts,{k:options[k] for k in order})]+=1
                summaries.append({'id':'time_'+label,'choices':dict(counts),'unique_orders':len(orders)})
            if visitor:
                for distance in (2,6,20):
                    facts=copy.deepcopy(visitor['compiler_input']['facts'])
                    player=rq.nearby_players(facts)[0];player.pop('dist2',None);player['dist']=distance
                    options=rq.idle_options(facts);counts=Counter();orders=list(itertools.permutations(options))
                    random.Random(7).shuffle(orders)
                    for index,order in enumerate((orders*12)[:12]):
                        counts[await ask(sink,f'player_{distance}_{index}',visitor,facts,{k:options[k] for k in order})]+=1
                    summaries.append({'id':'player_'+str(distance),'source_seq':visitor['seq'],'choices':dict(counts)})
            else:summaries.append({'id':'player_distance','status':'no native player frame yet'})
    finally:
        await client.close()
        output.with_suffix('.summary.json').write_text(json.dumps(summaries,ensure_ascii=False,indent=2))
    print(json.dumps(summaries,ensure_ascii=False,indent=2),flush=True)

asyncio.run(main())
