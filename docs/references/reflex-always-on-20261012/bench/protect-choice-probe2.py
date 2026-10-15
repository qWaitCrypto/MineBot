"""Option 2: during a protective action, re-ask the protective choice each frame (no "keep going")."""
import asyncio, itertools, json, re, statistics, sys
from collections import Counter
sys.path.insert(0, '/home/qwait/MineBot-always-on')
from minebot.app.decision_client import DecisionClient
RUN = '/home/qwait/MineBot-always-on/logs/reflex-always-on-20261014-step5-threat-claude2'

def question(state, weapon_in_option, offer_retreat):
    m = re.search(r'Nearest hostile mob: (\w+)', state.get('threats', ''))
    mob = m.group(1) if m else 'hostile mob'
    hand = state.get('main_hand')
    engage = f'Fight back against the {mob} in melee.'
    if weapon_in_option and hand and hand != 'nothing':
        engage = f'Fight back against the {mob} in melee with the {hand} in the main hand.'
    if weapon_in_option == 'health':
        hp = re.search(r'Health is (\d+) of 20', state.get('body', ''))
        engage = engage[:-1] + (f', with health {hp.group(1)} of 20.' if hp else '.')
    crit = {'engage': engage, 'move_away': f'Move away from the {mob}.'}
    if offer_retreat:
        crit['retreat_safe'] = 'Go back to the last place where the Body stood safely.'
    crit['call_agent'] = 'Hand control back to the Agent for a new plan.'
    keep = {k: state[k] for k in ('body', 'threats', 'inventory', 'main_hand', 'position') if state.get(k)}
    return {'state': keep, 'questions': {'p': {'type': 'choice', 'instructions': 'Choose how the Body protects itself right now.', 'criteria': crit}}}

def orders(req):
    keys = list(req['questions']['p']['criteria'])
    perms = list(itertools.permutations(keys))
    return [perms[i] for i in range(0, len(perms), max(1, len(perms) // 6))][:6]

async def main():
    rows = [json.loads(l) for l in open(RUN + '/trace.jsonl') if '"call_supervision_request"' in l]
    pick = [r for r in rows if (r.get('purpose') == 'running_frame' and r.get('tool') in ('engage_entity', 'move_away') and 595 <= r['seq'] <= 945)]
    c = DecisionClient()
    for r in pick:
        st = r['request']['state']
        offer = 'retreat_safe' in json.dumps(r['request']['questions'])
        line = []
        for w in (True, 'health'):
            base = question(st, w, offer)
            cnt = Counter()
            for o in orders(base):
                q = json.loads(json.dumps(base)); q['questions']['p']['criteria'] = {k: base['questions']['p']['criteria'][k] for k in o}
                for _ in range(3):
                    a = await c.ask(q)
                    if a.raw:
                        cnt[a.raw['answers']['p']['choice']] += 1; break
            line.append(('health' if w == 'health' else 'weapon') + ':' + ','.join(f'{k}{v}' for k, v in cnt.most_common()))
        print(r['seq'], r.get('tool') or '-', '|', st.get('body', '')[:22], '|', st.get('threats', '')[19:], '|', ' '.join(line))
    await c.close()
asyncio.run(main())
