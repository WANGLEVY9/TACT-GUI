"""Development-only paired trial of equal-quota GUI continuations."""
import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
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
GUARDED_DIRECT = 'guarded_direct'
STOP_PROMPT = (
    'Before tapping, decide from the current screenshot and action history whether the visible task goal already holds. '
    'If it holds, terminate now. Preserve all completed steps. Do not repeat an already successful action '
    'unless the task explicitly requires that repetition. If the goal does not hold, perform one necessary tap.'
)


def registered_arms(config):
    order = tuple(config.get('arms', ARMS))
    unique = set(order)
    if len(unique) != len(order) or not set(ARMS) <= unique <= {*ARMS, GUARDED_DIRECT}:
        raise ValueError('invalid registered arms')
    return order
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
    if arm in ('direct',GUARDED_DIRECT):
        return ('execute',)*slots
    if arm in REVIEW_INSTRUCTIONS:
        return ('review',)+('execute',)*(slots-1)
    raise ValueError('unknown continuation arm')


def caps(case):
    prefix_length = len(case['prefix'])
    slots = case.get('slots',CALLS_PER_ARM)
    call_schedule('direct',slots)
    progress_reads = 2*slots if case.get('measure_progress',False) else 0
    return {'model_calls':slots,'device_steps':6+3*slots+4*prefix_length+progress_reads}


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
    measure_progress = case.get('measure_progress',False)
    observed_steps = []
    phase = 'initial_checkpoint'

    def verify():
        nodes = device_call(scope,device.nodes)
        return matches([n for n in nodes if n.get('package')==app['package']],case['oracle'])

    try:
        report['initial_hash'], report['state'] = checkpoint(device,scope,app,case)
        report['upper'] = caps(case)
        advice = None
        for index,role in enumerate(call_schedule(arm,case.get('slots',CALLS_PER_ARM))):
            phase = 'screenshot'
            screenshot = device_call(scope,device.android.screenshot)
            capture = folder/f'screen-{index}.png'
            pending = capture.with_suffix('.tmp')
            pending.write_bytes(screenshot)
            pending.replace(capture)
            context = user_context(case,report['actions'])
            if role == 'review':
                context += '\n'+REVIEW_INSTRUCTIONS[arm]
            else:
                context += '\nPerform one touchscreen tap as the next step, or terminate if the task is complete.'
                if arm == GUARDED_DIRECT:
                    context += '\n'+STOP_PROMPT
                if advice:
                    context += '\nReview recommendation (advisory only): '+json.dumps(advice)
            phase = 'model_request'
            response = provider.predict(screenshot,context,max_tokens=TOKENS_PER_CALL,advice=role=='review')
            raw = response['text']
            call = {key:value for key,value in response.items() if key!='text'}
            call.update(role=role,screen_sha256=hashlib.sha256(screenshot).hexdigest())
            report['calls'].append(call)
            (folder/f'model-{index}.txt').write_text(raw)
            journal.append('model_response',index=index,role=role,model=call.get('returned_model'),
                           tokens=(call.get('usage') or {}).get('total_tokens'))
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
            phase = 'action'
            device_call(scope,lambda:device.android.execute(raw,width,height))
            actions.append((raw,width,height))
            report['actions'].append(action)
            journal.append('action_applied',index=index,action=action,action_index=len(actions))
            if measure_progress:
                # Metric-only oracle. Never enters user_context or action admission.
                phase = 'progress_measurement'
                observed_steps.append(verify())
                journal.append('progress_measured',index=index,goal_after=observed_steps[-1])
        phase = 'terminal_verification'
        observed = verify()
        phase = 'replay'
        replay_initial,_ = checkpoint(device,scope,app,case)
        replay_steps = []
        for raw,width,height in actions:
            device_call(scope,lambda:device.android.execute(raw,width,height))
            if measure_progress:
                replay_steps.append(verify())
        replay = verify()
        report['verification'] = {'observed':observed,'replay':replay,'replay_initial_hash':replay_initial}
        if replay_initial==report['initial_hash'] and observed==replay:
            report['success'] = int(observed)
        if measure_progress:
            stable_steps = observed_steps == replay_steps and report['success'] is not None
            report['progress'] = {
                'observed_after_actions':observed_steps,'replay_after_actions':replay_steps,
                'stable':stable_steps,
                'first_confirmed_action':next((i for i,v in enumerate(observed_steps,1) if v),None) if stable_steps else None,
                'lost_goal':any(observed_steps) and not observed if stable_steps else None,
            }
        report['actual'] = scope.close()
    except Exception as error:
        report['error_type'] = type(error).__name__
        report['error_phase'] = phase
        journal.append('arm_failed',phase=phase,error_type=type(error).__name__)
        # Known operations can be settled when no request has unknown usage.
        if not scope.audit()['pending_reservations']:
            report['actual'] = scope.close()
    report['elapsed_seconds'] = time.monotonic()-started
    write(folder/'report.json',report)
    return report


def paired_summary(blocks, baselines=('direct','self_review')):
    comparisons = {}
    for baseline in baselines:
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
    roster = registered_arms(config)
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
    ledger = Ledger(out/'budget.sqlite',{k:sum(caps(c)[k] for c in cases)*len(roster) for k in caps(cases[0])})
    rng = random.Random(config['seed'])
    jobs = defaultdict(list)
    devices = {}
    for case in cases:
        target = android[case['app_group']] if isinstance(android,dict) else android
        key = target.serial if isinstance(android,dict) else 'single'
        devices[key] = Device(target)
        order = list(roster)
        rng.shuffle(order)
        jobs[key].append((case,order))

    def worker(key):
        completed = []
        for case,order in jobs[key]:
            arms = [run_arm(devices[key],provider_factory,ledger,case,apps[case['app_group']],arm,out/case['id']/arm)
                    for arm in order]
            valid = all(row['success'] is not None for row in arms) and len({row['initial_hash'] for row in arms})==1
            block = {'checkpoint':case['id'],'order':order,'valid_pair':valid,'arms':arms}
            if isinstance(android,dict):
                block['device'] = key
            write(out/case['id']/'block.json',block)
            print(case['id'],valid,{row['arm']:row['success'] for row in arms},flush=True)
            completed.append(block)
        return completed

    if len(jobs)==1:
        blocks = worker(next(iter(jobs)))
    else:
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            by_id = {block['checkpoint']:block for batch in pool.map(worker,jobs) for block in batch}
        blocks = [by_id[case['id']] for case in cases]
    baselines = tuple(arm for arm in roster if arm!='independent_review')
    report = {'scope':'live functional-checkpoint development experiment',
              'protocol_sha256':digest(config),'blocks':blocks,
              'paired_summary':paired_summary(blocks,baselines),'budget':ledger.audit()}
    write(out/'report.json',report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config',type=Path)
    parser.add_argument('app_manifest',type=Path)
    allocator = parser.add_mutually_exclusive_group(required=True)
    allocator.add_argument('--serial')
    allocator.add_argument('--device-map', type=Path)
    parser.add_argument('--out',required=True,type=Path)
    parser.add_argument('--adb-timeout',type=float,default=20)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    if args.device_map:
        mapping = json.loads(args.device_map.read_text())
        groups = {case['app_group'] for case in config['checkpoints']}
        if set(mapping)!=groups or not 1<=len(set(mapping.values()))<=4:
            raise ValueError('device map must cover every application group using up to four emulators')
        targets = {serial:Android(serial,timeout=args.adb_timeout) for serial in set(mapping.values())}
        manifests = [read_frozen(args.app_manifest,target) for target in targets.values()]
        manifest = manifests[0]
        android = {group:targets[serial] for group,serial in mapping.items()}
    else:
        android = Android(args.serial,timeout=args.adb_timeout)
        manifest = read_frozen(args.app_manifest,android)
    run(config,{app['group']:app for app in manifest['config']['apps']},android,args.out)


if __name__=='__main__':
    main()
