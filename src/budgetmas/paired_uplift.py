"""Learn diagnostic uplift, calibrate at the family level, and replay allocation."""
import argparse
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import random
from statistics import mean

from .calibration import binomial_upper
from .frontier import Option, best_plan
from .mechanism import call_schedule, caps, registered_arms
from .paired_learning import FEATURES, dot, encode, ridge
from .pilot import digest, write
from .providers import MODEL, VISION_FLASH, list_cost


DIRECT, REVIEW, UPGRADE = 'flash_direct', 'flash_review', 'gui_review_flash'
ARMS = (DIRECT, REVIEW, UPGRADE)
THRESHOLDS = (.4,.6,.8)
RISK_TARGET = .2
DELTA = .1
CNY_PRICE = 8.
RESOURCES = ('model_calls','device_steps')
POLICIES = (*ARMS,'gain_frontier','calibrated_frontier')


def extract(root):
    def read(name):
        return json.loads((root/name).read_text())

    config, report = read('protocol.json'), read('report.json')
    if digest(config)!=report['protocol_sha256'] or set(registered_arms(config))!=set(ARMS):
        raise ValueError('unmatched protocol or wrong contrast roster')
    variants = config['variants']
    modes = ('direct','independent_review','independent_review')
    for arm, mode in zip(ARMS,modes):
        expected = {'mode':mode,'execute_model':VISION_FLASH,'max_tokens':512}
        if mode!='direct':
            expected.update(review_model=MODEL if arm==UPGRADE else VISION_FLASH,review_view='screen')
        if variants[arm]!=expected:
            raise ValueError('model, role or output cap differs from the frozen contrast')
    platform = config.get('platform','android')
    if platform not in ('android','web') or report['platform']!=platform:
        raise ValueError('invalid platform partition')
    cases = {c['id']:c for c in config['checkpoints']}
    blocks = {b['checkpoint']:b for b in report['blocks']}
    if (cases.keys()!=blocks.keys() or len(cases)!=len(config['checkpoints'])
            or len(blocks)!=len(report['blocks'])):
        raise ValueError('duplicate or missing checkpoint')
    rows, excluded = [], []
    screens = 0
    measured_costs = []
    for case_id, case in cases.items():
        block = blocks[case_id]
        records = block['arms']
        arms = {arm['arm']:arm for arm in records}
        if len(records)!=len(ARMS) or set(arms)!=set(ARMS):
            raise ValueError('duplicate or missing checkpoint arm')
        group = case['app_group']
        family = config['apps'][group]['task'] if platform=='web' else group
        upper = caps(case)
        costs = {}
        for name, arm in arms.items():
            if arm['checkpoint']!=case_id or arm['group']!=group or arm['variant']!=variants[name]:
                raise ValueError('arm identity mismatch')
            schedule = call_schedule(name,case['slots'],variants)
            if len(arm['calls'])>len(schedule):
                raise ValueError('arm exceeded schedule')
            amounts = []
            for i, call in enumerate(arm['calls']):
                expected = variants[name]['review_model'] if schedule[i]=='review' else VISION_FLASH
                price = list_cost(expected,call['usage'])
                if (call['requested_model'],call['returned_model'],call['role'],call['input_view'],
                        call['max_tokens'],call['estimated_list_cny'])!=(expected,expected,schedule[i],
                                                                       'screen',512,price):
                    raise ValueError('unexpected provider receipt')
                frame = root/case_id/name/f'screen-{i}.png'
                if hashlib.sha256(frame.read_bytes()).hexdigest()!=call['screen_sha256']:
                    raise ValueError('trajectory frame mismatch')
                screens += 1
                amounts.append(price)
                if price is not None:
                    measured_costs.append(price)
            if None not in amounts:
                costs[name] = sum(amounts)
        states = [a.get('state',{}) for a in records]
        fingerprint = states[0].get('decision_observation_sha256')
        reasons = []
        if not block['valid_pair']:
            reasons.append('unresolved arm or mismatched initial structure')
        if not fingerprint or not all(s==states[0] for s in states):
            reasons.append('missing or different decision observation')
        if len(costs)!=len(arms):
            reasons.append('incomplete usage receipts')
        if reasons:
            excluded.append({'id':case_id,'family':family,'reasons':reasons,
                             'unresolved_arms':[{'arm':name,'error_type':arm.get('error_type'),
                                                 'phase':arm.get('error_phase')}
                                                for name,arm in arms.items() if arm['success'] is None]})
            continue
        actual = {}
        outcomes = {}
        for name, arm in arms.items():
            verified = arm['success']
            check, used = arm['verification'], arm['actual']
            if (type(verified) is not int or verified not in (0,1) or check['observed']!=verified
                    or check['replay']!=verified or check['replay_initial_hash']!=arm['initial_hash']
                    or check.get('replay_observation_sha256')!=fingerprint
                    or arm['upper']!=upper or set(used)!=set(upper)
                    or any(not 0<=used[k]<=upper[k] or int(used[k])!=used[k] for k in upper)
                    or used['model_calls']!=len(arm['calls'])):
                raise ValueError('verification or accounting does not match protocol')
            actual[name] = {k:int(v) for k,v in used.items()}
            outcomes[name] = verified
        rows.append({'id':case_id,'group':family,'x':encode(case,states[0]),'upper':upper,
                     'decision_observation_sha256':fingerprint,
                     'outcomes':outcomes,'actual':actual,'cost_cny':costs})
    return rows, {'platform':platform,'protocol_sha256':report['protocol_sha256'],
                  'report_sha256':digest(report),'registered_checkpoints':len(cases),
                  'audited_screens':screens,'recorded_list_cny':round(sum(measured_costs),9),
                  'budget':report['budget'],'excluded':excluded}


def fit(rows):
    y = [r['outcomes'] for r in rows]
    reward = {'direct':ridge(rows,[a[DIRECT] for a in y],prior=.1),
              'review_gain':ridge(rows,[a[REVIEW]-a[DIRECT] for a in y]),
              'upgrade_gain':ridge(rows,[a[UPGRADE]-a[REVIEW] for a in y])}
    cost = {a:ridge(rows,[math.log1p(r['cost_cny'][a]*1_000_000) for r in rows],prior=.1)
            for a in ARMS}
    return {'reward':reward,'log_cost':cost}


def predict(model, row):
    direct, review, upgrade = (dot(model['reward'][key],row['x'])
                              for key in ('direct','review_gain','upgrade_gain'))
    raw = (direct,direct+review,direct+review+upgrade)
    return {'probability':{arm:min(1.,max(0.,value)) for arm,value in zip(ARMS,raw)},
            'upgrade_gain':min(1.,max(-1.,upgrade)),
            'cost_cny':{a:math.expm1(max(0.,dot(model['log_cost'][a],row['x'])))/1_000_000
                        for a in ARMS}}


def selection_stats(rows, predictions, threshold):
    active = {}
    selected = successes = 0
    for row in rows:
        scores = predictions[row['id']]['probability']
        eligible = [row['outcomes'][arm] for arm in ARMS if scores[arm]>=threshold]
        if eligible:
            active[row['group']] = active.get(row['group'],False) or 0 in eligible
        selected += len(eligible)
        successes += sum(eligible)
    return {'active_families':len(active),'families_with_error':sum(active.values()),
            'selected_options':selected,'verified_options':successes,'candidate_options':len(rows)*len(ARMS)}


def calibrate(rows, predictions):
    candidates = []
    for threshold in THRESHOLDS:
        stat = selection_stats(rows,predictions,threshold)
        upper = binomial_upper(stat['families_with_error'],stat['active_families'],DELTA/len(THRESHOLDS))
        candidates.append({'threshold':threshold,**stat,'family_risk_upper':upper})
    passing = [v for v in candidates if v['family_risk_upper'] is not None
               and v['family_risk_upper']<=RISK_TARGET]
    winner = max(passing,key=lambda v:(v['selected_options'],v['threshold'])) if passing else None
    return {'threshold':winner['threshold'] if winner else None,'candidates':candidates}


def replay(rows, predictions, caps, policy, threshold=None):
    remaining = dict(caps)
    pending = {r['id']:r for r in rows}
    decisions = []
    while pending:
        options = []
        for row in pending.values():
            p = predictions[row['id']]
            for arm in ARMS:
                prob = p['probability'][arm]
                if policy=='calibrated_frontier' and (threshold is None or prob<threshold):
                    continue
                if policy in ARMS and policy!=arm:
                    continue
                options.append(Option(row['id'],arm,row['upper']['model_calls'],
                                      row['upper']['device_steps'],prob,
                                      expected_cny=p['cost_cny'][arm]))
        if policy in ARMS:
            picked = next((o for o in options if o.calls<=remaining['model_calls']
                           and o.operations<=remaining['device_steps']),None)
        else:
            chosen = best_plan(options,remaining['model_calls'],remaining['device_steps'],
                               cny_price=CNY_PRICE).choices
            picked = chosen[0] if chosen else None
        if picked is None:
            break
        row = pending.pop(picked.task)
        for key in RESOURCES:
            remaining[key] -= row['actual'][picked.mode][key]
        decisions.append({'task':picked.task,'mode':picked.mode,'p':picked.expected_success,
                          'predicted_cny':picked.expected_cny,
                          'list_cny':row['cost_cny'][picked.mode],
                          'verified':row['outcomes'][picked.mode]})
    return {'verified':sum(r['verified'] for r in decisions),'admitted':len(decisions),
            'not_admitted':len(pending),'list_cny':round(sum(r['list_cny'] for r in decisions),9),
            'actual':{k:caps[k]-remaining[k] for k in RESOURCES},'decisions':decisions}


def evaluate(rows, seed):
    groups = sorted({r['group'] for r in rows})
    if len(groups)<3:
        raise ValueError('need disjoint training, calibration and evaluation families')
    folds = []
    for i, family in enumerate(groups):
        other = [g for g in groups if g!=family]
        random.Random(seed+i).shuffle(other)
        count = len(other)//2
        calibration = set(other[:count])
        training = set(other[count:])
        train = [r for r in rows if r['group'] in training]
        tune = [r for r in rows if r['group'] in calibration]
        test = [r for r in rows if r['group']==family]
        model = fit(train)
        frozen_gate = calibrate(tune,{r['id']:predict(model,r) for r in tune})
        preds = {r['id']:predict(model,r) for r in test}
        diff = lambda r: r['outcomes'][UPGRADE]-r['outcomes'][REVIEW]
        constant = mean(mean(diff(r) for r in train if r['group']==g) for g in other[count:])
        errors = {'learned':mean(abs(preds[r['id']]['upgrade_gain']-diff(r)) for r in test),
                  'zero':mean(abs(diff(r)) for r in test),
                  'training_mean':mean(abs(constant-diff(r)) for r in test)}
        limits = {
            'tight':{k:max(min(r['upper'][k] for r in test),sum(r['upper'][k] for r in test)//2)
                     for k in RESOURCES},
            'full':{k:sum(r['upper'][k] for r in test) for k in RESOURCES},
        }
        threshold = frozen_gate['threshold']
        folds.append({'held_family':family,'train_families':sorted(training),
                      'calibration_families':sorted(calibration),'test_ids':[r['id'] for r in test],
                      'fitted_heads':model,'predictions':preds,'upgrade_gain_mae':errors,
                      'calibration':frozen_gate,
                      'held_risk_coverage':[{'threshold':v,**selection_stats(test,preds,v)} for v in THRESHOLDS],
                      'limits':limits,
                      'replay':{level:{p:replay(test,preds,limit,p,threshold) for p in POLICIES}
                                for level,limit in limits.items()}})
    totals = {level:{p:{k:round(sum(f['replay'][level][p][k] for f in folds),9)
                        for k in ('admitted','verified','list_cny')} for p in POLICIES}
              for level in ('tight','full')}
    return {'active_calibrated_folds':sum(f['calibration']['threshold'] is not None for f in folds),
            'group_mean_uplift_mae':{k:mean(f['upgrade_gain_mae'][k] for f in folds)
                                     for k in ('learned','zero','training_mean')},
            'totals':totals,'folds':folds}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run',type=Path)
    parser.add_argument('output',type=Path)
    parser.add_argument('--split-seed',type=int,default=20261128)
    args = parser.parse_args()
    rows, meta = extract(args.run)
    report = {**meta,'scope':'family-split development replay, no deployment claim',
              'paired_checkpoints':len(rows),'split_seed':args.split_seed,'features':FEATURES,
              'thresholds':THRESHOLDS,'target_family_failure_risk':RISK_TARGET,
              'calibration_delta':DELTA,'cny_price':CNY_PRICE,
              **evaluate(rows,args.split_seed)}
    write(args.output,report)
    print(json.dumps({k:v for k,v in report.items() if k!='folds'},indent=2))


if __name__=='__main__':
    main()
