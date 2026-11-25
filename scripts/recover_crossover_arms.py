"""One bounded retry of technical failures, with a separate sensitivity report."""
import argparse
from copy import deepcopy
import json
from pathlib import Path

from budgetmas.android import Android
from budgetmas.horizon import interactions
from budgetmas.ledger import Ledger
from budgetmas.mechanism import caps, run_arm
from budgetmas.pilot import digest, read_frozen, write
from budgetmas.pilot_device import Device
from budgetmas.providers import GUIPlus


RECOVERABLE = {'TimeoutExpired','RuntimeError'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('original',type=Path)
    parser.add_argument('app_manifest',type=Path)
    parser.add_argument('output',type=Path)
    args = parser.parse_args()
    protocol = json.loads((args.original/'protocol.json').read_text())
    original = json.loads((args.original/'report.json').read_text())
    if digest(protocol)!=original['protocol_sha256']:
        raise ValueError('original protocol mismatch')
    cases = {case['id']:case for case in protocol['checkpoints']}
    sensitivity = deepcopy(original)
    jobs = [(block,index,cases[block['checkpoint']])
            for block in sensitivity['blocks'] for index,arm in enumerate(block['arms'])
            if arm['success'] is None and arm.get('error_type') in RECOVERABLE]
    if not jobs:
        print('No eligible unresolved device arms')
        return
    if any(not block.get('device') for block,_,_ in jobs):
        raise ValueError('recovery requires original per-checkpoint device IDs')
    args.output.mkdir(parents=True,exist_ok=False)
    write(args.output/'registration.json',{'original_report_sha256':digest(original),
                                          'adb_timeout_seconds':40,
                                          'jobs':[{'checkpoint':case['id'],'arm':block['arms'][i]['arm'],
                                                   'device':block['device']} for block,i,case in jobs]})
    ledger = Ledger(args.output/'budget.sqlite',{k:sum(caps(case)[k] for _,_,case in jobs)
                                               for k in caps(jobs[0][2])})
    results = []
    for block,index,case in jobs:
        android = Android(block['device'],timeout=40)
        manifest = read_frozen(args.app_manifest,android)
        apps = {app['group']:app for app in manifest['config']['apps']}
        old = block['arms'][index]
        arm = run_arm(Device(android),GUIPlus,ledger,case,apps[case['app_group']],old['arm'],
                      args.output/case['id']/old['arm'])
        results.append({'checkpoint':case['id'],'role':old['arm'],'original_error':old['error_type'],
                        'retry_success':arm['success'],'retry_error':arm.get('error_type')})
        # This copy is solely for sensitivity analysis. The original is immutable.
        block['arms'][index]=arm
        block['valid_pair']=all(row['success'] is not None for row in block['arms']) and len(
            {row['initial_hash'] for row in block['arms']})==1
        print(results[-1],flush=True)
    write(args.output/'sensitivity.json',{'scope':'single technical retry; original results retained',
                                         'original_report_sha256':digest(original),
                                         'recoveries':results,'retry_budget':ledger.audit(),
                                         'interaction':interactions(protocol,sensitivity)})


if __name__=='__main__':
    main()
