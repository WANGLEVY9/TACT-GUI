"""Audit frozen routing runs and summarize matched functional outcomes and list costs."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from .mechanism import caps, registered_arms, call_schedule
from .pilot import digest, write
from .providers import list_cost


CONTRASTS = {
    'fixed_direct_change_model':('gui_direct','flash_direct'),
    'fixed_gui_model_add_reviewer':('gui_direct','gui_review'),
    'fixed_gui_model_reduce_quota':('gui_direct','gui_direct_128'),
    'fixed_flash_executor_change_visual_reviewer':('flash_review','gui_review_flash'),
    'fixed_gui_reviewer_change_executor':('gui_review','gui_review_flash'),
    'fixed_text_observation_change_reviewer':('text_flash_review','text_plus_review'),
}


def metrics(arm):
    usage = [c['usage'] for c in arm['calls'] if c.get('usage_complete')]
    costs = [c.get('estimated_list_cny') for c in arm['calls']]
    return {'verified':arm['success'], 'initial_hash':arm.get('initial_hash'),
            'calls':len(arm['calls']), 'taps':len(arm['actions']),
            'input_tokens':sum(u['prompt_tokens'] for u in usage),
            'output_tokens':sum(u['completion_tokens'] for u in usage),
            'truncated':sum(c.get('finish_reason')=='length' for c in arm['calls']),
            'summed_request_seconds':round(sum(c['request_seconds'] for c in arm['calls']),6),
            'estimated_list_cny':round(sum(costs),9) if len(costs)==len(usage) and None not in costs
            and arm.get('actual') and arm['actual']['model_calls']==len(costs) else None}


def summarize(root):
    def read(name):
        return json.loads((root/name).read_text())

    config, report = read('protocol.json'), read('report.json')
    if digest(config) != report['protocol_sha256']:
        raise ValueError('invalid protocol checksum')
    roster = registered_arms(config)
    variants = config['variants']
    index = {c['id']:c for c in config['checkpoints']}
    blocks = report['blocks']
    if len(blocks)!=len(index) or set(index)!={b['checkpoint'] for b in blocks}:
        raise ValueError('report is missing a registered checkpoint')
    verified_pngs = 0
    arms_by_case = {}
    for block in blocks:
        case_id = block['checkpoint']
        case = index[case_id]
        if len(block['arms'])!=len(roster) or {a['arm'] for a in block['arms']}!=set(roster):
            raise ValueError('missing or duplicate routing arm')
        arms = {}
        for arm in block['arms']:
            name = arm['arm']
            variant = variants[name]
            if arm['variant'] != variant or arm['group'] != case['app_group'] or arm['checkpoint'] != case_id:
                raise ValueError('arm identity mismatch')
            schedule = call_schedule(name,case['slots'],variants)
            if len(arm['calls'])>len(schedule):
                raise ValueError('arm exceeded schedule')
            for i, call in enumerate(arm['calls']):
                review = schedule[i]=='review'
                expected = variant['review_model'] if review else variant['execute_model']
                view = variant['review_view'] if review else 'screen'
                if ((call['requested_model'],call['returned_model'],call['input_view'],call['max_tokens'],
                     call['role'],call['estimated_list_cny'])
                    != (expected,expected,view,variant['max_tokens'],schedule[i],list_cost(expected,call['usage']))):
                    raise ValueError('provider receipt differs from registered assignment')
                path = root/case_id/name/f'screen-{i}.png'
                if hashlib.sha256(path.read_bytes()).hexdigest() != call['screen_sha256']:
                    raise ValueError('frame hash mismatch')
                verified_pngs += 1
            if arm['success'] in (0,1):
                verification = arm['verification']
                if (arm['success'] != verification['observed'] or arm['success'] != verification['replay']
                        or arm['initial_hash'] != verification['replay_initial_hash']):
                    raise ValueError('invalid independent replay')
            if arm.get('actual') and any(not 0 <= n <= caps(case)[resource]
                                         for resource,n in arm['actual'].items()):
                raise ValueError('arm exceeds registered budget')
            arms[name] = metrics(arm)
        arms_by_case[case_id] = arms

    results = {}
    for name, (baseline, candidate) in CONTRASTS.items():
        if baseline not in variants or candidate not in variants:
            continue
        paired, excluded = [], []
        wins = Counter()
        for case, measures in arms_by_case.items():
            x, y = measures[baseline], measures[candidate]
            if (x['initial_hash']!=y['initial_hash'] or any(
                    a['verified'] not in (0,1) or a['estimated_list_cny'] is None for a in (x,y))):
                excluded.append(case)
                continue
            wins[y['verified']-x['verified']] += 1
            paired.append({'case':case,'baseline':x,'candidate':y})
        results[name] = {'baseline_arm':baseline,'candidate_arm':candidate,'pairs':len(paired),
                         'excluded':excluded,'candidate_wins':wins[1],'baseline_wins':wins[-1],
                         'ties':wins[0],
                         'estimated_list_cny':{k:round(sum(p[k]['estimated_list_cny'] for p in paired),9)
                                               for k in ('baseline','candidate')},
                         'results':paired}
    common = {key:arms for key,arms in arms_by_case.items()
              if len({a['initial_hash'] for a in arms.values()})==1 and all(
                  a['verified'] in (0,1) and a['estimated_list_cny'] is not None for a in arms.values())}
    totals = {}
    for role in roster:
        collected = [arms[role] for arms in common.values()]
        totals[role] = {key:sum(a[key] for a in collected)
                        for key in ('verified','calls','taps','input_tokens','output_tokens','truncated')}
        totals[role]['summed_request_seconds']=round(sum(a['summed_request_seconds'] for a in collected),6)
        totals[role]['estimated_list_cny']=round(sum(a['estimated_list_cny'] for a in collected),9)
    pareto = [role for role,row in totals.items() if common and not any(
        other['verified']>=row['verified'] and other['estimated_list_cny']<=row['estimated_list_cny']
        and (other['verified']>row['verified'] or other['estimated_list_cny']<row['estimated_list_cny'])
        for other in totals.values())]
    return {'scope':'development contrasts on observed matched and replay-checked trajectories',
            'platform':report['platform'],'protocol_sha256':report['protocol_sha256'],
            'audited_screens':verified_pngs,'budget':report['budget'],'registered_cases':list(index),
            'common_cases':list(common),'totals_on_common_cases':totals,'observed_pareto':pareto,
            'contrasts':results,'cases':arms_by_case}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run',type=Path)
    args = parser.parse_args()
    report = summarize(args.run)
    write(args.run/'routing-summary.json',report)
    print(json.dumps({k:report[k] for k in ('platform','audited_screens','common_cases',
                                           'totals_on_common_cases','observed_pareto')},indent=2))


if __name__=='__main__':
    main()
