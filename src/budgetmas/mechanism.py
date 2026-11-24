"""Development-only paired trial of equal-quota GUI continuations."""
import argparse
import hashlib
import json
from pathlib import Path
import random
import re
import time

from .android import Android, parse_action, png_dimensions
from .gui_team import device_call
from .ledger import Ledger
from .pilot import digest, read_frozen, write
from .pilot_device import Device, find_target, matches, state_hash
from .providers import GUIPlus
from .runtime import Journal
from .scopes import Scope


ARMS = ('direct', 'self_review', 'independent_review')
CALLS_PER_ARM = 2
MAX_CHECKPOINTS = 24
TOKENS_PER_CALL = 512
REVIEW_INSTRUCTIONS = {
    'self_review': 'Review the prior actions attributed to you and identify the best next action.',
    'independent_review': 'Independently review the prior actions of another operator and identify the best next action.',
}


def call_schedule(arm, slots=CALLS_PER_ARM):
    if type(slots) is not int or slots not in (2,3):
        raise ValueError('supported continuation horizons are two and three calls')
    if arm == 'direct':
        return ('execute',)*slots
    if arm in REVIEW_INSTRUCTIONS:
        return ('review',)+('execute',)*(slots-1)
    raise ValueError('unknown continuation arm')


def caps(case):
    prefix_length = len(case['prefix'])
    slots = case.get('slots',CALLS_PER_ARM)
    call_schedule('direct',slots)
    return {'model_calls':slots,'device_steps':6+3*slots+4*prefix_length}


def checkpoint(device, ledger, app, case):
    package = app['package']
    device_call(ledger,lambda:device.launch(app))
    for label in case['prefix']:
        nodes = device_call(ledger,device.nodes)
        location = find_target([n for n in nodes if n.get('package')==package], (label,))
        if location is None:
            raise RuntimeError('registered prefix control unavailable')
        device_call(ledger,lambda:device.tap(location))
    nodes = [n for n in device_call(ledger,device.nodes) if n.get('package')==package]
    if not matches(nodes,case['start']) or matches(nodes,case['oracle']):
        raise RuntimeError('registered checkpoint assertions failed')
    visible = [(n.get('resource-id',''),n.get('class',''),n.get('text',''),n.get('content-desc','')) for n in nodes]
    # UI structure and registered anchor labels control matching. Dynamic text
    # enters only aggregate pre-decision features, never a task's oracle.
    return state_hash(nodes,package), {
        'nodes':len(visible),
        'labelled_nodes':sum(bool(text or desc) for _,_,text,desc in visible),
        'clickable_nodes':sum(n.get('clickable')=='true' for n in nodes),
        'duplicate_visible_labels':sum(bool(text or desc) for _,_,text,desc in visible) -
            len({(text or desc).casefold() for _,_,text,desc in visible if text or desc}),
    }


def user_context(case, actions):
    return ('Task: '+case['goal']+'\nEarlier completed UI actions: '+json.dumps(case['prefix'])+
            '\nContinuation actions already taken: '+json.dumps(actions))


def run_arm(device, provider_factory, run_ledger, case, app, arm, folder):
    folder.mkdir(parents=True,exist_ok=False)
    journal = Journal(folder/'events.jsonl')
    scope = Scope(run_ledger,caps(case))
    provider = provider_factory(scope)
    started = time.monotonic()
    report = {'checkpoint':case['id'],'group':case['app_group'],'arm':arm,
              'scope_id':scope.id,'calls':[],'actions':[],'success':None}
    actions = []
    try:
        report['initial_hash'], report['state'] = checkpoint(device,scope,app,case)
        report['upper'] = caps(case)
        advice = None
        for index,role in enumerate(call_schedule(arm,case.get('slots',CALLS_PER_ARM))):
            screenshot = device_call(scope,device.android.screenshot)
            (folder/f'screen-{index}.png').write_bytes(screenshot)
            context = user_context(case,report['actions'])
            if role == 'review':
                context += '\n'+REVIEW_INSTRUCTIONS[arm]
            else:
                context += '\nPerform one touchscreen tap as the next step, or terminate if the task is complete.'
                if advice:
                    context += '\nReview recommendation (advisory only): '+json.dumps(advice)
            response = provider.predict(screenshot,context,max_tokens=TOKENS_PER_CALL,advice=role=='review')
            raw = response['text']
            call = {key:value for key,value in response.items() if key!='text'}
            call.update(role=role,screen_sha256=hashlib.sha256(screenshot).hexdigest())
            report['calls'].append(call)
            (folder/f'model-{index}.txt').write_text(raw)
            if role == 'review':
                advice = raw[:1500]
                continue
            try:
                action = parse_action(raw)
            except (TypeError,ValueError,KeyError):
                journal.append('invalid_action',index=index)
                continue
            if action['action']=='terminate':
                journal.append('stop_requested',index=index)
                break
            if action['action']!='left_click':
                journal.append('unsupported_action',index=index)
                continue
            width,height = png_dimensions(screenshot)
            device_call(scope,lambda:device.android.execute(raw,width,height))
            actions.append((raw,width,height))
            report['actions'].append(action)
        def verify():
            nodes = device_call(scope,device.nodes)
            return matches([n for n in nodes if n.get('package')==app['package']],case['oracle'])
        observed = verify()
        replay_initial,_ = checkpoint(device,scope,app,case)
        for raw,width,height in actions:
            device_call(scope,lambda:device.android.execute(raw,width,height))
        replay = verify()
        report['verification'] = {'observed':observed,'replay':replay,'replay_initial_hash':replay_initial}
        if replay_initial==report['initial_hash'] and observed==replay:
            report['success'] = int(observed)
        report['actual'] = scope.close()
    except Exception as error:
        report['error_type'] = type(error).__name__
        journal.append('arm_failed',error_type=type(error).__name__)
        # Known operations can be settled when no request has unknown usage.
        if not scope.audit()['pending_reservations']:
            report['actual'] = scope.close()
    report['elapsed_seconds'] = time.monotonic()-started
    write(folder/'report.json',report)
    return report


def paired_summary(blocks):
    comparisons = {}
    for baseline in ('direct','self_review'):
        counts = dict(independent_only=0, baseline_only=0, both_success=0, both_incomplete=0)
        families = set()
        for block in blocks:
            if not block['valid_pair']:
                continue
            by_arm = {row['arm']:row for row in block['arms']}
            independent = by_arm['independent_review']
            outcomes = (independent['success'],by_arm[baseline]['success'])
            key = {(1,0):'independent_only',(0,1):'baseline_only',
                   (1,1):'both_success',(0,0):'both_incomplete'}[outcomes]
            counts[key] += 1
            if outcomes == (1,0):
                families.add(independent['group'])
        comparisons[baseline] = {**counts,'independent_win_families':sorted(families),
                                 'net_wins':counts['independent_only']-counts['baseline_only']}
    return {'registered':len(blocks),'comparable':sum(block['valid_pair'] for block in blocks),
            'comparisons':comparisons}


def run(config, apps, android, output, provider_factory=GUIPlus):
    cases = config['checkpoints']
    if config['schema']!=1 or not 1<=len(cases)<=MAX_CHECKPOINTS:
        raise ValueError('expected one to twenty-four registered development checkpoints')
    ids = [case['id'] for case in cases]
    if len(set(ids))!=len(ids) or any(not re.fullmatch(r'[a-z][a-z0-9-]*',key) for key in ids):
        raise ValueError('invalid checkpoint identifiers')
    for case in cases:
        if case['app_group'] not in apps or not case['start'] or not case['oracle']:
            raise ValueError('checkpoint missing application or assertions')
        caps(case)
    out = Path(output)
    out.mkdir(parents=True,exist_ok=False)
    write(out/'protocol.json',config)
    ledger = Ledger(out/'budget.sqlite',{k:sum(caps(c)[k] for c in cases)*len(ARMS) for k in caps(cases[0])})
    device = Device(android)
    rng = random.Random(config['seed'])
    blocks = []
    for case in cases:
        order = list(ARMS); rng.shuffle(order)
        arms = [run_arm(device,provider_factory,ledger,case,apps[case['app_group']],arm,out/case['id']/arm)
                for arm in order]
        valid = all(row['success'] is not None for row in arms) and len({row['initial_hash'] for row in arms})==1
        blocks.append({'checkpoint':case['id'],'order':order,'valid_pair':valid,'arms':arms})
        write(out/case['id']/'block.json',blocks[-1])
        print(case['id'],valid,{row['arm']:row['success'] for row in arms},flush=True)
    report = {'scope':'live functional-checkpoint development experiment',
              'protocol_sha256':digest(config),'blocks':blocks,
              'paired_summary':paired_summary(blocks),'budget':ledger.audit()}
    write(out/'report.json',report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config',type=Path)
    parser.add_argument('app_manifest',type=Path)
    parser.add_argument('--serial',required=True)
    parser.add_argument('--out',required=True,type=Path)
    args = parser.parse_args()
    android = Android(args.serial)
    manifest = read_frozen(args.app_manifest,android)
    config = json.loads(args.config.read_text())
    run(config,{app['group']:app for app in manifest['config']['apps']},android,args.out)


if __name__=='__main__':
    main()
