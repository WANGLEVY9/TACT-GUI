"""Register a within-checkpoint two/three-call crossover and compare effects."""
import argparse
import json
from pathlib import Path
import random

from .mechanism import GUARDED_DIRECT,MAX_CHECKPOINTS,registered_arms
from .pilot import digest,write


def crossover(protocol, seed):
    base = protocol['checkpoints']
    if protocol['schema']!=1 or not 1<=len(base)<=MAX_CHECKPOINTS//2:
        raise ValueError('crossover exceeds runner limits')
    if len({case['id'] for case in base})!=len(base) or any('base_checkpoint' in case for case in base):
        raise ValueError('expected unique uncrossed checkpoints')
    cases = [{**case,'id':f"{case['id']}-h{slots}",'base_checkpoint':case['id'],'slots':slots}
             for case in base for slots in (2,3)]
    random.Random(seed).shuffle(cases)
    return {'schema':1,'seed':seed,'source_protocol_sha256':digest(protocol),
            'scope':'development horizon crossover','arms':list(registered_arms(protocol)),
            'checkpoints':cases}


def interactions(protocol, report):
    if digest(protocol)!=report['protocol_sha256']:
        raise ValueError('protocol digest mismatch')
    roster = registered_arms(protocol)
    cases = {case['id']:case for case in protocol['checkpoints']}
    ids = [block['checkpoint'] for block in report['blocks']]
    if len(ids)!=len(set(ids)) or set(ids)!=set(cases):
        raise ValueError('report must cover all registered blocks')
    by_checkpoint = {}
    for block in report['blocks']:
        case=cases[block['checkpoint']]
        horizons = by_checkpoint.setdefault(case['base_checkpoint'],{})
        if case['slots'] in horizons:
            raise ValueError('duplicate crossover horizon')
        horizons[case['slots']] = (case,block)
    results=[]
    for base,entries in by_checkpoint.items():
        if set(entries)!={2,3}:
            raise ValueError('missing crossover horizon')
        case2,short=entries[2]
        case3,long=entries[3]
        ignored={'id','slots'}
        if ({k:v for k,v in case2.items() if k not in ignored} !=
                {k:v for k,v in case3.items() if k not in ignored}):
            raise ValueError('horizons use different checkpoint conditions')
        row={'base_checkpoint':base,'group':case2['app_group'],'comparable':False}
        if short['valid_pair'] and long['valid_pair'] and short.get('device')==long.get('device'):
            arms=short['arms']+long['arms']
            for block in (short,long):
                if len(block['arms'])!=len(roster) or {a['arm'] for a in block['arms']}!=set(roster):
                    raise ValueError('incomplete crossover arms')
            if len({a['initial_hash'] for a in arms})==1:
                short_y={a['arm']:a['success'] for a in short['arms']}
                long_y={a['arm']:a['success'] for a in long['arms']}
                if any(y not in (0,1) for y in (*short_y.values(),*long_y.values())):
                    raise ValueError('invalid crossover outcome')
                row.update(comparable=True,observed={'2':short_y,'3':long_y},
                           review_by_horizon=(long_y['self_review']-long_y['direct'])-
                                             (short_y['self_review']-short_y['direct']),
                           independent_by_horizon=(long_y['independent_review']-long_y['self_review'])-
                                                  (short_y['independent_review']-short_y['self_review']))
                if GUARDED_DIRECT in roster:
                    row['guard_by_horizon']=(long_y[GUARDED_DIRECT]-long_y['direct'])-(short_y[GUARDED_DIRECT]-short_y['direct'])
                    row['independent_vs_guard_by_horizon'] = (
                        (long_y['independent_review']-long_y[GUARDED_DIRECT])-
                        (short_y['independent_review']-short_y[GUARDED_DIRECT]))
        results.append(row)
    matched = {row['base_checkpoint'] for row in results if row['comparable']}
    summaries = {}
    for block in report['blocks']:
        case = cases[block['checkpoint']]
        if case['base_checkpoint'] not in matched:
            continue
        current = summaries.setdefault(str(case['slots']), {})
        for arm in block['arms']:
            metrics = current.setdefault(arm['arm'], dict(terminal_success=0, trials=0,
                                                           stable_progress=0, first_hit=0, lost_goal=0))
            metrics['trials'] += 1
            metrics['terminal_success'] += arm['success']
            progress = arm.get('progress', {})
            if progress.get('stable'):
                metrics['stable_progress'] += 1
                metrics['first_hit'] += progress['first_confirmed_action'] is not None
                metrics['lost_goal'] += progress['lost_goal']
    return {'registered':len(results),'comparable':len(matched),
            'outcomes_by_horizon':summaries,'checkpoint_interactions':results}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest='stage',required=True)
    register=sub.add_parser('register')
    register.add_argument('protocol',type=Path)
    register.add_argument('output',type=Path)
    register.add_argument('--seed',type=int,required=True)
    measure=sub.add_parser('analyze')
    measure.add_argument('protocol',type=Path)
    measure.add_argument('report',type=Path)
    measure.add_argument('output',type=Path)
    args=p.parse_args()
    protocol=json.loads(args.protocol.read_text())
    result=(crossover(protocol,args.seed) if args.stage=='register' else
            interactions(protocol,json.loads(args.report.read_text())))
    write(args.output,result)


if __name__=='__main__':
    main()
