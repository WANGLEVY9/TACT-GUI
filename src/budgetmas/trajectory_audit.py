"""Replay terminal failures to check for an earlier, subsequently lost goal state."""
import argparse
import json
from pathlib import Path

from .android import Android, png_dimensions
from .gui_team import device_call
from .ledger import Ledger
from .mechanism import checkpoint
from .pilot import digest, read_frozen, write
from .pilot_device import Device, matches
from .scopes import Scope


def branch_traces(root, protocol, report):
    if digest(protocol)!=report['protocol_sha256']:
        raise ValueError('protocol digest mismatch')
    cases = {case['id']:case for case in protocol['checkpoints']}
    for block in report['blocks']:
        if not block['valid_pair']:
            continue
        case = cases[block['checkpoint']]
        roles={arm['arm']:arm for arm in block['arms']}
        review_disagreement=roles['independent_review']['success']!=roles['self_review']['success']
        for arm in block['arms']:
            if arm['success']!=0 or len(arm['actions'])<2:
                continue
            if arm['arm']!='direct' and not review_disagreement:
                continue
            screenshots = sorted((root/block['checkpoint']/arm['arm']).glob('screen-*.png'))
            sizes = {png_dimensions(path.read_bytes()) for path in screenshots}
            if len(sizes)!=1:
                raise ValueError('missing screens or geometry changes')
            yield case,arm,next(iter(sizes))


def audit(device, app, case, arm, size, ledger):
    results = []
    for _ in range(2):
        initial,_ = checkpoint(device,ledger,app,case)
        steps = []
        for action in arm['actions']:
            if action['action']!='left_click':
                raise ValueError('unexpected recorded action type')
            raw = '<tool_call>'+json.dumps({'name':'computer_use','arguments':action})+'</tool_call>'
            device_call(ledger,lambda:device.android.execute(raw,*size))
            nodes = device_call(ledger,device.nodes)
            steps.append(matches([node for node in nodes if node.get('package')==app['package']],case['oracle']))
        results.append({'initial_matches':initial==arm['initial_hash'],'after_each_action':steps})
    verified = (all(r['initial_matches'] and not r['after_each_action'][-1] for r in results)
                and results[0]['after_each_action']==results[1]['after_each_action'])
    return {'id':case['id'],'arm':arm.get('arm','direct'),'replays':results,'verified':verified,
            'early_goal_lost':any(results[0]['after_each_action'][:-1]) if verified else None}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('app_manifest',type=Path)
    parser.add_argument('trial_directories',type=Path,nargs='+')
    parser.add_argument('--serial',required=True)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    android=Android(args.serial)
    manifest=read_frozen(args.app_manifest,android)
    apps={app['group']:app for app in manifest['config']['apps']}
    trials=[]
    for root in args.trial_directories:
        protocol=json.loads((root/'protocol.json').read_text())
        report=json.loads((root/'report.json').read_text())
        trials.extend(branch_traces(root,protocol,report))
    args.out.mkdir(parents=True,exist_ok=False)
    device=Device(android)
    bounds=[{'device_steps':2*(2+2*len(case['prefix'])+2*len(arm['actions']))}
            for case,arm,_ in trials]
    ledger=Ledger(args.out/'budget.sqlite',{'device_steps':sum(row['device_steps'] for row in bounds)})
    results=[]
    for (case,arm,size),bound in zip(trials,bounds):
        scope=Scope(ledger,bound)
        try:
            result=audit(device,apps[case['app_group']],case,arm,size,scope)
            scope.close()
        except Exception as error:
            result={'id':case['id'],'arm':arm['arm'],'verified':False,'early_goal_lost':None,'error_type':type(error).__name__}
        results.append(result)
        print(result,flush=True)
    write(args.out/'report.json',{'scope':'post-hoc audit of multi-tap direct failures and failed arms in review disagreements',
                                 'verified_replays':sum(row['verified'] for row in results),
                                 'early_goals_lost':sum(row['early_goal_lost'] is True for row in results),
                                 'results':results,'budget':ledger.audit()})


if __name__=='__main__':
    main()
