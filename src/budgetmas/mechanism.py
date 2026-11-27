"""Development-only paired trial of equal-quota GUI continuations.

Runs the same registered arms on Android ADB or Web/Playwright checkpoints.
Adding ``platform`` and ``apps`` to a protocol selects the Web adapter;
legacy Android protocols continue to use the frozen ADB manifest.
"""
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
from .environments import PLATFORMS, environment_for
from .legacy import LegacyCheckpoint
from .gui_team import device_call
from .intent import compile_intent, ground, semantic_candidate
from .ledger import Ledger
from .pilot import digest, read_frozen, write
from .pilot_device import Device  # Legacy injection point for Android device doubles.
from .providers import GUIPlus, MODEL_VIEWS, TEXT_FLASH, TEXT_PLUS
from .runtime import Journal
from .scopes import Scope
from .ui_nodes import sample


ARMS = ('direct', 'self_review', 'independent_review')
GUARDED_DIRECT = 'guarded_direct'
INTENT = 'intent'
STOP_PROMPT = (
    'Before tapping, decide from the current screenshot and action history whether the visible task goal already holds. '
    'If it holds, terminate now. Preserve all completed steps. Do not repeat an already successful action '
    'unless the task explicitly requires that repetition. If the goal does not hold, perform one necessary tap.'
)


def registered_arms(config):
    if 'variants' in config:
        variants = config['variants']
        if not isinstance(variants, dict) or not variants:
            raise ValueError('expected registered routing variants')
        order = tuple(config.get('arms', sorted(variants)))
        if len(order)!=len(variants) or set(order)!=set(variants):
            raise ValueError('arm order must register every routing variant exactly once')
        return order
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


def validate_variants(config):
    variants = config.get('variants', {})
    for name, variant in variants.items():
        if not re.fullmatch(r'[a-z][a-z0-9_]*', name):
            raise ValueError('invalid routing variant identifier')
        mode = variant.get('mode')
        if mode == INTENT:
            if (set(variant)!={'mode','intent_model','max_tokens'}
                    or variant['intent_model'] not in (None,TEXT_FLASH,TEXT_PLUS)
                    or type(variant['max_tokens']) is not int or not 1 <= variant['max_tokens'] <= 4096):
                raise ValueError('invalid intent routing variant')
            continue
        review = mode in REVIEW_INSTRUCTIONS
        fields = {'mode','execute_model','max_tokens'} | ({'review_model','review_view'} if review else set())
        if mode not in (*ARMS, GUARDED_DIRECT) or set(variant)!=fields:
            raise ValueError('invalid routing variant fields')
        tokens = variant['max_tokens']
        if type(tokens) is not int or not 1 <= tokens <= 4096:
            raise ValueError('invalid registered output token cap')
        if 'screen' not in MODEL_VIEWS.get(variant['execute_model'], set()):
            raise ValueError('executor requires a registered screen model')
        if review and variant['review_view'] not in MODEL_VIEWS.get(variant['review_model'], set()):
            raise ValueError('review model does not support its registered observation')
    return variants


def call_schedule(arm, slots=CALLS_PER_ARM, variants=None):
    if type(slots) is not int or slots not in (2,3):
        raise ValueError('supported continuation horizons are two and three calls')
    arm = variants[arm]['mode'] if variants else arm
    if arm in ('direct',GUARDED_DIRECT,INTENT):
        return ('execute',)*slots
    if arm in REVIEW_INSTRUCTIONS:
        return ('review',)+('execute',)*(slots-1)
    raise ValueError('unknown continuation arm')


def caps(case, variant=None):
    prefix_length = len(case['prefix'])
    slots = case.get('slots',CALLS_PER_ARM)
    call_schedule('direct',slots)
    progress_reads = 2*slots if case.get('measure_progress',False) else 0
    if variant and variant['mode']==INTENT:
        # Per click: screenshot, grounding read, application and replay.
        return {'model_calls':int(variant['intent_model'] is not None),
                'device_steps':6+4*slots+4*prefix_length+progress_reads}
    return {'model_calls':slots,'device_steps':6+3*slots+4*prefix_length+progress_reads}


def checkpoint(env, ledger, app, case):
    device_call(ledger, env.launch)
    for label in case['prefix']:
        location = device_call(ledger,lambda:env.locate(label))
        if location is None:
            raise RuntimeError('registered prefix control unavailable')
        device_call(ledger,lambda:env.tap(location))
    return device_call(ledger,lambda:env.inspect(case))


def user_context(case, actions):
    return ('Task: '+case['goal']+'\nEarlier completed UI actions: '+json.dumps(case['prefix'])+
            '\nContinuation actions already taken: '+json.dumps(actions))


def visible_labels(env):
    values = dict.fromkeys(str(sample(node,'text') or sample(node,'description') or
                               sample(node,'name')).strip()[:80] for node in env.nodes())
    return [value for value in values if value][:32]


def capture_frame(folder, index, env, scope):
    screenshot = device_call(scope,env.screenshot)
    capture = folder/f'screen-{index}.png'
    pending = capture.with_suffix('.tmp')
    pending.write_bytes(screenshot)
    pending.replace(capture)
    return screenshot


def record_response(report, response, folder, index, role, screenshot, journal):
    call = {k:v for k,v in response.items() if k!='text'}
    call.update(role=role,frame=index,screen_sha256=hashlib.sha256(screenshot).hexdigest())
    report['calls'].append(call)
    (folder/f'model-{index}.txt').write_text(response['text'])
    journal.append('model_response',index=index,role=role,model=call.get('returned_model'),
                   tokens=(call.get('usage') or {}).get('total_tokens'),
                   finish_reason=call.get('finish_reason'),max_tokens=call.get('max_tokens'),
                   input_view=call.get('input_view'),estimated_list_cny=call.get('estimated_list_cny'))
    return call


def intent_continuation(env, scope, case, variant, provider_factory, report, folder, journal):
    plan = compile_intent(case['goal'])
    slots = case.get('slots',CALLS_PER_ARM)
    report['intent'] = {'contract':plan.describe() if plan else None}

    def route(index, decision, reason, **fields):
        report['intent']['decision'] = decision
        report['intent']['reason'] = reason
        journal.append('intent_decision',index=index,decision=decision,reason=reason,**fields)

    if plan is None:
        # A semantic model may propose a candidate for review, without
        # opening an execution path for an unconfirmed interpretation.
        route(0,'clarify','unsupported_or_conflicting_instruction')
        report['intent']['question'] = 'Which controls should be clicked, in what order, and which actions are disallowed?'
        if variant['intent_model']:
            screenshot = capture_frame(folder,0,env,scope)
            response = provider_factory(scope,model=variant['intent_model']).propose_intent(
                case['goal'],max_tokens=variant['max_tokens'])
            call = record_response(report,response,folder,0,'interpret',screenshot,journal)
            if call.get('finish_reason')!='length':
                try:
                    candidate = semantic_candidate(case['goal'],response['text'])
                    report['intent']['candidate'] = candidate
                    if candidate['question']:
                        report['intent']['question'] = candidate['question']
                except (TypeError,ValueError):
                    report['intent']['candidate_invalid'] = True
        return
    if len(plan.steps) > slots:
        route(0,'defer','insufficient_action_slots')
        return

    for index,step in enumerate(plan.steps):
        screenshot = capture_frame(folder,index,env,scope)
        width,height = png_dimensions(screenshot)
        nodes = device_call(scope,env.nodes)
        point,reason = ground(step,plan.forbidden,nodes,width,height)
        if point is None:
            route(index,'clarify',reason,target=step.target)
            report['intent']['question'] = f'Which visible control should be used for “{step.target}”?'
            return
        route(index,'execute',reason,target=step.target)
        # Offset by half a pixel so integer conversion recovers the same pixel.
        action = {'action':'left_click','coordinate':[
            (point[0]+.5)*1000/width,(point[1]+.5)*1000/height]}
        raw = '<tool_call>'+json.dumps({'name':'computer_use','arguments':action})+'</tool_call>'
        yield index,raw,width,height
    report['intent'].update(decision='verify',reason='ordered_steps_exhausted')
    journal.append('intent_finished',decision='verify',reason='ordered_steps_exhausted')


def run_arm(env, provider_factory, run_ledger, case, app, arm, folder, *, variant=None):
    folder.mkdir(parents=True,exist_ok=False)
    journal = Journal(folder/'events.jsonl')
    mode = variant['mode'] if variant else arm
    upper = caps(case,variant)
    scope = Scope(run_ledger,upper)
    output_cap = variant['max_tokens'] if variant else TOKENS_PER_CALL
    provider = None if mode==INTENT else (
        provider_factory(scope, model=variant['execute_model']) if variant else provider_factory(scope))
    reviewer = (provider_factory(scope, model=variant['review_model']) if variant and mode in REVIEW_INSTRUCTIONS
                and variant['review_model']!=variant['execute_model'] else provider)
    started = time.monotonic()
    report = {'checkpoint':case['id'],'group':case['app_group'],'arm':arm,
              'scope_id':scope.id,'calls':[],'actions':[],'success':None}
    if variant:
        report['variant'] = variant
    actions = []
    measure_progress = case.get('measure_progress',False)
    observed_steps = []
    phase = 'initial_checkpoint'

    def verify():
        return device_call(scope,lambda:env.oracle(case['oracle']))

    def apply(index,raw,width,height):
        action = parse_action(raw)
        device_call(scope,lambda:env.execute(raw,width,height))
        actions.append((raw,width,height))
        report['actions'].append(action)
        journal.append('action_applied',index=index,action=action,action_index=len(actions))

    def measure(index):
        observed_steps.append(verify())
        journal.append('progress_measured',index=index,goal_after=observed_steps[-1])

    def model_actions():
        nonlocal phase
        advice = None
        for index,role in enumerate(call_schedule(mode,case.get('slots',CALLS_PER_ARM))):
            phase = 'screenshot'
            screenshot = capture_frame(folder,index,env,scope)
            context = user_context(case,report['actions'])
            model_input = screenshot
            if role == 'review':
                context += '\n'+REVIEW_INSTRUCTIONS[mode]
                if variant and variant['review_view']=='text':
                    phase = 'review_observation'
                    labels = device_call(scope,lambda:visible_labels(env))
                    model_input = None
                    context += '\nVisible UI labels (untrusted): '+json.dumps(labels,ensure_ascii=False)
            else:
                context += '\nPerform one touchscreen tap as the next step, or terminate if the task is complete.'
                if mode == GUARDED_DIRECT:
                    context += '\n'+STOP_PROMPT
                if advice:
                    context += '\nReview recommendation (advisory only): '+json.dumps(advice)
            phase = 'model_request'
            response = (reviewer if role=='review' else provider).predict(
                model_input,context,max_tokens=output_cap,advice=role=='review')
            raw = response['text']
            call = record_response(report,response,folder,index,role,screenshot,journal)
            if call.get('finish_reason')=='length':
                journal.append('model_truncated',index=index)
                continue
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
            yield index,raw,*png_dimensions(screenshot)

    try:
        report['initial_hash'], report['state'] = checkpoint(env,scope,app,case)
        report['upper'] = upper
        phase = 'intent_routing' if mode==INTENT else phase
        continuation = (intent_continuation(env,scope,case,variant,provider_factory,report,folder,journal)
                        if mode==INTENT else model_actions())
        for index,raw,width,height in continuation:
            phase = 'action'
            apply(index,raw,width,height)
            if measure_progress:
                # Metric-only oracle. Never enters user_context or action admission.
                phase = 'progress_measurement'
                measure(index)
            if mode==INTENT:
                phase = 'intent_routing'
        phase = 'terminal_verification'
        observed = verify()
        phase = 'replay'
        replay_initial,replay_state = checkpoint(env,scope,app,case)
        replay_steps = []
        for raw,width,height in actions:
            device_call(scope,lambda:env.execute(raw,width,height))
            if measure_progress:
                replay_steps.append(verify())
        replay = verify()
        report['verification'] = {'observed':observed,'replay':replay,'replay_initial_hash':replay_initial}
        decision_hash = report['state'].get('decision_observation_sha256')
        replay_hash = replay_state.get('decision_observation_sha256')
        if decision_hash is not None:
            report['verification']['replay_observation_sha256'] = replay_hash
        if replay_initial==report['initial_hash'] and observed==replay and decision_hash==replay_hash:
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


def platform_of(config):
    platform = config.get('platform', 'android')
    if platform not in PLATFORMS:
        raise ValueError('unsupported GUI platform')
    return platform


def run(config, apps, android, output, provider_factory=GUIPlus, platform=None):
    platform = platform or platform_of(config)
    cases = config['checkpoints']
    roster = registered_arms(config)
    variants = validate_variants(config)
    if config['schema']!=1 or not 1<=len(cases)<=MAX_CHECKPOINTS:
        raise ValueError('expected one to twenty-four registered development checkpoints')
    ids = [case['id'] for case in cases]
    if len(set(ids))!=len(ids) or any(not re.fullmatch(r'[a-z][a-z0-9-]*',key) for key in ids):
        raise ValueError('invalid checkpoint identifiers')
    for case in cases:
        if case['app_group'] not in apps or not case['start'] or not case['oracle']:
            raise ValueError('checkpoint missing application or assertions')
        caps(case)
    quotas = [caps(c,variants.get(arm)) for c in cases for arm in roster]
    limits = {key:sum(q[key] for q in quotas) for key in quotas[0]}
    if variants and (type(config.get('max_model_calls')) is not int
                     or limits['model_calls'] > config['max_model_calls']):
        raise ValueError('routing experiment exceeds its registered call cap')
    if platform == 'web' and not isinstance(android, dict) and android != 'fixture':
        raise ValueError('Web protocols require a device map or the local fixture backend')
    if platform == 'android' and (not isinstance(apps, dict) or any('package' not in app for app in apps.values())):
        raise ValueError('Android protocols require an application manifest with package identifiers')
    out = Path(output)
    out.mkdir(parents=True,exist_ok=False)
    write(out/'protocol.json',config)
    ledger = Ledger(out/'budget.sqlite',limits)
    rng = random.Random(config['seed'])
    jobs = defaultdict(list)
    for case in cases:
        if platform == 'web' and android != 'fixture':
            target = android[case['app_group']] if isinstance(android, dict) else android
            key = target if isinstance(target, str) else json.dumps(target, sort_keys=True)
        elif platform == 'web':
            key = case['app_group']
            target = 'fixture'
        else:
            target = android[case['app_group']] if isinstance(android,dict) else android
            key = getattr(target, 'serial', 'single') if isinstance(android, dict) else 'single'
        order = list(roster)
        rng.shuffle(order)
        jobs[key].append((case,order,target))

    def worker(key):
        env = None
        active_group = None
        completed = []
        try:
            for case,order,target in jobs[key]:
                group = case['app_group']
                app = apps[group]
                if group != active_group:
                    if env is not None:
                        env.close()
                        env = None
                    env = (LegacyCheckpoint(Device(target), app)
                           if platform == 'android' and Device.__module__ != 'budgetmas.pilot_device'
                           else environment_for(platform, target, app))
                    active_group = group
                arms = [run_arm(env,provider_factory,ledger,case,app,arm,out/case['id']/arm,
                                variant=variants.get(arm))
                        for arm in order]
                valid = all(row['success'] is not None for row in arms) and len({row['initial_hash'] for row in arms})==1
                block = {'checkpoint':case['id'],'order':order,'valid_pair':valid,'arms':arms}
                if isinstance(android,dict):
                    block['device'] = key
                write(out/case['id']/'block.json',block)
                print(case['id'],valid,{row['arm']:row['success'] for row in arms},flush=True)
                completed.append(block)
        finally:
            if env is not None:
                env.close()
        return completed

    if len(jobs)==1:
        blocks = worker(next(iter(jobs)))
    else:
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            by_id = {block['checkpoint']:block for batch in pool.map(worker,jobs) for block in batch}
        blocks = [by_id[case['id']] for case in cases]
    baselines = tuple(arm for arm in roster if arm!='independent_review')
    report = {'scope':platform+' functional-checkpoint development experiment',
              'platform':platform,
              'protocol_sha256':digest(config),'blocks':blocks,
              'budget':ledger.audit()}
    if not variants:
        report['paired_summary'] = paired_summary(blocks,baselines)
    write(out/'report.json',report)
    return report


def web_protocol(config, target='fixture'):
    """Resolve a Web protocol's embedded app definitions without ADB.

    ``fixture`` selects the deterministic in-memory backend; a device map
    selects Playwright, with each value identifying the browser target.
    """
    if platform_of(config) != 'web':
        raise ValueError('web_protocol requires a Web protocol')
    if not isinstance(config.get('apps'), dict):
        raise ValueError('Web protocol requires embedded app definitions')
    from .environments import validate_app
    return {group: validate_app(app, 'web') for group, app in config['apps'].items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config',type=Path)
    parser.add_argument('app_manifest',type=Path,nargs='?')
    allocator = parser.add_mutually_exclusive_group(required=True)
    allocator.add_argument('--serial')
    allocator.add_argument('--device-map', type=Path)
    allocator.add_argument('--fixture', action='store_true')
    parser.add_argument('--out',required=True,type=Path)
    parser.add_argument('--adb-timeout',type=float,default=20)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    platform = platform_of(config)
    if args.fixture:
        if platform != 'web' or args.serial or args.device_map or args.app_manifest:
            raise ValueError('--fixture requires a Web protocol with no device map')
        from .offline import provider_factory
        apps, android = web_protocol(config), 'fixture'
        run(config, apps, android, args.out, provider_factory=provider_factory, platform=platform)
        return
    if platform == 'web':
        if not args.device_map or args.app_manifest:
            raise ValueError('Web protocols require --device-map or --fixture')
        mapping = json.loads(args.device_map.read_text())
        groups = {case['app_group'] for case in config['checkpoints']}
        if set(mapping) != groups:
            raise ValueError('device map must cover every Web application group')
        android = mapping
        apps = web_protocol(config)
        run(config, apps, android, args.out, platform=platform)
        return
    if args.app_manifest is None:
        raise ValueError('Android protocols require a frozen app manifest')
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
    run(config,{app['group']:app for app in manifest['config']['apps']},android,args.out,platform='android')


if __name__=='__main__':
    main()
