"""Freeze, collect and compare live application-group experiments."""
import hashlib
import json
import random
import re
import time
from pathlib import Path

from .android import Android
from .data import split_manifest
from .decision import Action, FixedOrderPlanner, BudgetPlanner
from .ledger import Ledger
from .pilot_device import Device, Trial, features, quotas, FEATURE_NAMES, MODES
from .runtime import Journal, TeamExecutor


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,allow_nan=False).encode()).hexdigest()


def write(path, value):
    with Path(path).open('x') as stream:
        json.dump(value,stream,indent=2,allow_nan=False)
        stream.write('\n')


def freeze(config, android):
    if config['schema'] != 1:
        raise ValueError('unsupported config schema')
    avd = android.adb('emu','avd','name').splitlines()[0].decode().strip()
    if avd != config['avd'] or avd != 'BudgetMASPilot':
        raise ValueError('pilot requires the dedicated BudgetMASPilot AVD')
    fingerprint = android.adb('shell','getprop','ro.build.fingerprint').decode().strip()
    if not fingerprint:
        raise ValueError('empty Android build fingerprint')
    apps = config['apps']
    for key in ('group','package'):
        if len({app[key] for app in apps}) != len(apps):
            raise ValueError('applications must have distinct groups and package identifiers')
    rows = []
    for app in apps:
        if not re.fullmatch(r'[a-z][a-z0-9-]*',app['group']):
            raise ValueError('invalid application group name')
        package = app['package']
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.]+',package) or not re.fullmatch(r'[A-Za-z0-9_.$]+',app['activity']):
            raise ValueError('invalid Android package or activity')
        version = android.adb('shell','dumpsys','package',package).decode()
        match = re.search(r'versionCode=(\d+)',version)
        if not match:
            raise ValueError(f'{package} is missing')
        for task in app['tasks']:
            if not re.fullmatch(r'[a-z][a-z0-9-]*',task['id']):
                raise ValueError('invalid task identifier')
            if not task['steps'] or not task['oracle'] or any(
                    not step or not all(isinstance(x,str) and x.strip() for x in step)
                    for step in task['steps']):
                raise ValueError('invalid task specification')
            if set(task['oracle']) - {'resource','text','description','selected'}:
                raise ValueError('unknown oracle field')
            rows.append({'id':app['group']+'/'+task['id'],'app_group_id':app['group'],
                         'package':package,'version_code':match.group(1),'task':task,
                         'canonical_tab':bool(app.get('canonical_tab'))})
    split = split_manifest(rows,config['seed'])
    return {'config':config,'split':split,'fingerprint':fingerprint,
            'sha256':digest({'config':config,'split':split,'fingerprint':fingerprint})}


def validate_manifest(manifest):
    contents = {k:manifest[k] for k in ('config','split','fingerprint')}
    if digest(contents) != manifest['sha256']:
        raise ValueError('frozen manifest checksum mismatch')
    stored = manifest['split']
    rows = [{k:v for k,v in row.items() if k != 'split'} for row in stored['rows']]
    if split_manifest(rows,manifest['config']['seed']) != stored:
        raise ValueError('frozen application partition mismatch')


def read_frozen(path, android):
    manifest = json.loads(Path(path).read_text())
    validate_manifest(manifest)
    if freeze(manifest['config'],android) != manifest:
        raise ValueError('frozen manifest differs from device, applications, or task definitions')
    return manifest


def action_for(row, mode):
    task = row['task']
    limits = quotas(task,mode,row.get('canonical_tab',False))
    upper = {key:sum(role[key] for role in limits.values()) for key in limits['execute']}
    return Action(row['id']+':'+mode,row['id'],mode,features(task,mode),upper)


def run_trial(device, row, app, action, ledger, path, seed):
    path.mkdir(parents=True,exist_ok=False)
    journal = Journal(path/'events.jsonl')
    runner = Trial(device,app,row['task'],action.configuration)
    executor = TeamExecutor(ledger,journal,runner,runner,None,runner)
    before = ledger.audit()['spent']
    started = time.monotonic()
    try:
        result = executor.run(action,quotas(row['task'],action.configuration,row.get('canonical_tab',False)),initial_state='fresh-launch',
                              seed=seed,split=row['split'])
    except Exception as error:
        result = {'status':'unresolved','error_type':type(error).__name__}
        journal.append('pilot_trial_failed',error_type=type(error).__name__)
    verification = result.get('verification')
    audit = ledger.audit()
    record = {'task_id':row['id'],'app_group_id':row['app_group_id'],'split':row['split'],
              'configuration':action.configuration,'features':list(action.features),'outcome':result['status'],
              'success':int(result['status']=='task_completed') if result['status'] in ('task_completed','task_incomplete') else None,
              'verification':verification,'error_type':result.get('error_type'),
              'elapsed_seconds':time.monotonic()-started,
              'actual':{k:audit['spent'][k]-before[k] for k in before},'ledger':audit}
    write(path/'trial.json',record)
    return record


def prepare(device, app):
    # Contacts onboarding is completed once during setup, outside the measured
    # runs and identically for every subsequent arm. Other apps need no setup.
    if not app.get('setup'):
        return
    from .pilot_device import find_target
    device.launch(app)
    for labels in app['setup']:
        location = find_target(device.nodes(),labels)
        if location is not None:
            device.tap(location)


def collect(manifest,android,output):
    config = manifest['config']
    rng = random.Random(config['seed'])
    out = Path(output); out.mkdir(parents=True,exist_ok=False)
    device = Device(android)
    records = []
    rows = [row for row in manifest['split']['rows'] if row['split'] != 'test']
    for app in config['apps']:
        if not any(row['app_group_id']==app['group'] for row in rows):
            continue
        prepare(device,app)
    order = []
    for row in rows:
        modes = list(MODES); rng.shuffle(modes)
        order.append((row,modes))
    for row,modes in order:
        app = next(a for a in config['apps'] if a['group']==row['app_group_id'])
        for mode in modes:
            action = action_for(row,mode)
            dest = out/row['id']/mode
            dest.parent.mkdir(parents=True,exist_ok=True)
            ledger = Ledger(dest.parent/(mode+'.sqlite'),action.upper)
            rec = run_trial(device,row,app,action,ledger,dest,config['seed'])
            rec['id'] = action.id
            records.append(rec)
            print(rec['id'],rec['outcome'],flush=True)
    # Matched start-state checks are separate from each arm's independent replay.
    for row, _ in order:
        arms = [rec for rec in records if rec['task_id']==row['id']]
        same = all(rec['verification'] for rec in arms) and len({rec['verification']['initial_hash'] for rec in arms})==1
        for rec in arms:
            rec['paired_initial_state_matches'] = bool(same)
    payload = {'manifest_sha256':manifest['sha256'],'feature_names':FEATURE_NAMES,'records':records}
    write(out/'collection.json',payload)
    return payload


def compare(manifest, android, model, output):
    config = manifest['config']
    if model.metadata['manifest_sha256'] != manifest['sha256']:
        raise ValueError('model and holdout manifest do not match')
    out = Path(output); out.mkdir(parents=True,exist_ok=False)
    device = Device(android)
    rng = random.Random(config['seed'])
    summaries = []
    for app in config['apps']:
        rows = [row for row in manifest['split']['rows'] if row['split']=='test' and row['app_group_id']==app['group']]
        if not rows:
            continue
        prepare(device,app)
        policies = ['fixed_single','fixed_collaborative','fixed_single_alias','learned']
        rng.shuffle(policies)
        held_out = {row['id']:row for row in rows}
        for policy in policies:
            folder = out/app['group']/policy
            folder.mkdir(parents=True,exist_ok=False)
            ledger = Ledger(folder/'budget.sqlite',config['limits'])
            journal = Journal(folder/'decisions.jsonl')
            options = MODES if policy == 'learned' else (policy.removeprefix('fixed_'),)
            pending = [action_for(row,mode) for row in rows for mode in options]
            planner = BudgetPlanner(model,**model.metadata['policy_parameters']) if policy=='learned' else FixedOrderPlanner()
            trials = []; started = time.monotonic()
            while pending:
                available = ledger.available()
                plan = planner.plan(pending,available)
                journal.append('decision',available=available,candidate_scores=plan['candidate_scores'],
                               selected=[a.id for a in plan['actions']])
                if not plan['actions']:
                    break
                action = plan['actions'][0]
                record = run_trial(device,held_out[action.task],app,action,ledger,folder/action.task.split('/')[-1],config['seed'])
                trials.append(record)
                pending = [other for other in pending if other.task != action.task]
                if record['outcome']=='unresolved':
                    break
            summary = {'app_group_id':app['group'],'policy':policy,'task_count':len(rows),
                       'verified_completions':sum(rec['success']==1 for rec in trials),
                       'verified_incomplete':sum(rec['success']==0 for rec in trials),
                       'unresolved':sum(rec['success'] is None for rec in trials),
                       'not_attempted':len(rows)-len(trials),
                       'trials':trials,'elapsed_seconds':time.monotonic()-started,'budget':ledger.audit()}
            write(folder/'summary.json',summary)
            summaries.append(summary)
            print(app['group'],policy,summary['verified_completions'],summary['budget']['spent'],flush=True)
    paired = []
    for item in summaries:
        if item['policy'] != 'learned':
            continue
        for other in summaries:
            if other['app_group_id']==item['app_group_id'] and other['policy']!='learned':
                paired.append({'app_group_id':item['app_group_id'],'baseline':other['policy'],
                               'completion_difference':item['verified_completions']-other['verified_completions']})
    initial_states = {}
    for arm in summaries:
        for trial in arm['trials']:
            if trial['verification']:
                initial_states.setdefault(trial['task_id'],{})[arm['policy']] = trial['verification']['initial_hash']
    matched = {task:{'policies':sorted(states),'hashes_match':len(set(states.values()))==1}
               for task,states in initial_states.items()}
    totals = {}
    for arm in summaries:
        total = totals.setdefault(arm['policy'],{
            'apps':0,'task_count':0,'verified_completions':0,
            'verified_incomplete':0,'unresolved':0,'not_attempted':0,
            'spent':dict.fromkeys(config['limits'],0),'reserved':dict.fromkeys(config['limits'],0)})
        total['apps'] += 1
        for key in ('task_count','verified_completions','verified_incomplete','unresolved','not_attempted'):
            total[key] += arm[key]
        for measure in ('spent','reserved'):
            for key in total[measure]:
                total[measure][key] += arm['budget'][measure][key]
    report = {'manifest_sha256':manifest['sha256'],'model_metadata_sha256':digest(model.metadata),
              'scope':'live Android functional-task pilot','task_initial_states':matched,
              'resources':config['limits'],'aggregate_by_policy':totals,
              'summaries':summaries,'paired_completion_differences':paired}
    write(out/'comparison.json',report)
    return report


def cli(args):
    if args.stage == 'fit':
        from .pilot_model import fit_pilot
        manifest = json.loads(Path(args.manifest).read_text())
        collection = json.loads(Path(args.collection).read_text())
        result = fit_pilot(manifest,collection,args.out)
        print(json.dumps(result['validation_metrics']))
        return
    android = Android(args.serial)
    if args.stage == 'freeze':
        write(args.manifest,freeze(json.loads(Path(args.config).read_text()),android))
    elif args.stage == 'collect':
        collect(read_frozen(args.manifest,android),android,args.out)
    elif args.stage == 'evaluate':
        from .pilot_model import PilotModel
        compare(read_frozen(args.manifest,android),android,PilotModel.load(args.model),args.out)
