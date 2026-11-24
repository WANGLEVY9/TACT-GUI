"""Development-only matched-effect models and leave-one-application-out replay."""
import argparse
import json
import math
from pathlib import Path
import re

from .mechanism import ARMS, caps, paired_summary
from .models import BayesianHead, solve_spd
from .pilot import digest, write


FEATURES = ('bias', 'slots', 'prefix_steps', 'goal_words', 'sequential_connectives',
            'ui_nodes', 'label_fraction', 'clickable_fraction', 'duplicate_label_fraction')
RESOURCES = ('model_calls', 'device_steps')
BASELINES = tuple(ARMS) + ('execution_slot_rule','learned')
# Frozen development replay budgets. A live campaign can have different costs.
REPLAY_BUDGETS = {'tight':{'model_calls':3,'device_steps':30},
                  'loose':{'model_calls':6,'device_steps':60}}


def encode(case, state):
    words = re.findall(r"[a-z0-9]+",case['goal'].lower())
    nodes = state['nodes']
    if nodes <= 0:
        raise ValueError('empty decision observation')
    counts = [state[k] for k in ('labelled_nodes','clickable_nodes','duplicate_visible_labels')]
    if any(type(v) is not int or not 0 <= v <= nodes for v in counts):
        raise ValueError('invalid decision observation counts')
    return [1, caps(case)['model_calls']/3, len(case['prefix'])/4, len(words)/40,
            sum(w in ('and','then') for w in words)/4, math.log1p(nodes)/5,
            *(v/nodes for v in counts)]


def extract(protocol, report):
    if digest(protocol)!=report['protocol_sha256']:
        raise ValueError('report does not match registered protocol')
    cases = {case['id']:case for case in protocol['checkpoints']}
    ids = [block['checkpoint'] for block in report['blocks']]
    if len(ids)!=len(set(ids)) or len(protocol['checkpoints'])!=len(cases) or set(ids)!=set(cases):
        raise ValueError('report blocks must match registered checkpoints')
    usable, excluded = [], []
    for block in report['blocks']:
        case = cases[block['checkpoint']]
        if not block['valid_pair']:
            excluded.append({'id':case['id'],'reason':'noncomparable execution or verification'})
            continue
        records = block['arms']
        arms = {arm['arm']:arm for arm in records}
        if len(records)!=len(ARMS) or set(arms)!=set(ARMS):
            raise ValueError('incomplete or duplicate arms')
        if any(arm['group']!=case['app_group'] or arm['checkpoint']!=case['id'] for arm in records):
            raise ValueError('arm identity mismatch')
        if len({arm['initial_hash'] for arm in records})!=1 or any(arm['success'] not in (0,1) for arm in records):
            raise ValueError('invalid matched outcomes')
        if any('state' not in arm for arm in records) or len({digest(arm['state']) for arm in records})!=1:
            excluded.append({'id':case['id'],'reason':'missing or differing pre-decision features'})
            continue
        upper = caps(case)
        for arm in records:
            actual = arm['actual']
            if arm['upper']!=upper or set(actual)!=set(upper) or any(
                    not math.isfinite(actual[k]) or not 0 <= actual[k] <= upper[k]
                    or int(actual[k])!=actual[k] for k in upper):
                raise ValueError('invalid measured resources')
        usable.append({'id':case['id'],'group':case['app_group'],
                       'x':encode(case,records[0]['state']),'upper':upper,
                       'outcomes':{mode:arms[mode]['success'] for mode in ARMS},
                       'actual':{mode:arms[mode]['actual'] for mode in ARMS}})
    return usable,excluded


def ridge(rows, targets, prior=1.0):
    # Give every family equal total weight. The prior shrinks poorly supported effects.
    groups = {row['group'] for row in rows}
    sizes = {g:sum(row['group']==g for row in rows) for g in groups}
    model = BayesianHead(len(FEATURES),prior=prior)
    for row,target in zip(rows,targets):
        model.noise = sizes[row['group']]*len(groups)/len(rows)
        model.update(row['x'],target)
    return solve_spd(model.precision,model.rhs)


def fit_heads(rows):
    if not rows:
        raise ValueError('no comparable development samples')
    values = [row['outcomes'] for row in rows]
    heads = {
        'direct':ridge(rows,[y['direct'] for y in values],prior=.1),
        'review_increment':ridge(rows,[y['self_review']-y['direct'] for y in values]),
        'independent_increment':ridge(rows,[y['independent_review']-y['self_review'] for y in values]),
    }
    cost = {mode:{key:ridge(rows,[math.log1p(row['actual'][mode][key]) for row in rows],prior=.1)
                  for key in RESOURCES} for mode in ARMS}
    return {'reward':heads,'log_cost':cost}


def dot(weights,x):
    return sum(w*v for w,v in zip(weights,x))


def predict(model,x):
    raw = {name:dot(weights,x) for name,weights in model['reward'].items()}
    mean = {'direct':raw['direct'],
            'self_review':raw['direct']+raw['review_increment'],
            'independent_review':raw['direct']+raw['review_increment']+raw['independent_increment']}
    return {'reward':{mode:max(0.0,min(1.0,value)) for mode,value in mean.items()},
            'delta_independent_vs_self':raw['independent_increment'],
            'cost':{mode:{key:math.expm1(max(0.0,dot(weights,x))) for key,weights in heads.items()}
                    for mode,heads in model['log_cost'].items()}}


def replay(rows, predictions, limit, policy):
    available = dict(limit)
    pending = list(rows)
    decisions = []
    completed = 0
    while pending:
        available_rows = [row for row in pending if all(row['upper'][k]<=available[k] for k in RESOURCES)]
        if not available_rows:
            break
        if policy == 'learned':
            def score(row,mode):
                p = predictions[row['id']]
                return p['reward'][mode]-0.04*p['cost'][mode]['model_calls']-0.008*p['cost'][mode]['device_steps']
            row,mode = max(((r,m) for r in available_rows for m in ARMS),key=lambda pair:score(*pair))
            if score(row,mode)<=0:
                break
        else:
            row = available_rows[0]
            # Favor execution if two calls remain or goal syntax suggests a sequence.
            mode = ('direct' if row['upper']['model_calls']==2 or row['x'][4]>0 else 'self_review') if policy=='execution_slot_rule' else policy
        for key in RESOURCES:
            available[key] -= row['actual'][mode][key]
        value = row['outcomes'][mode]
        completed += value
        decisions.append({'id':row['id'],'arm':mode,'verified':value})
        pending.remove(row)
    return {'verified':completed,'admitted':len(decisions),
            'not_admitted':len(pending),'decisions':decisions,
            'spent':{k:limit[k]-available[k] for k in RESOURCES}}


def analyze(protocol,report):
    rows,excluded = extract(protocol,report)
    groups = sorted({row['group'] for row in rows})
    if len(groups)<2:
        raise ValueError('need at least two comparable application families')
    folds = []
    for group in groups:
        train = [r for r in rows if r['group']!=group]
        held = [r for r in rows if r['group']==group]
        model = fit_heads(train)
        predictions = {row['id']:predict(model,row['x']) for row in held}
        brier = {mode:sum((predictions[r['id']]['reward'][mode]-r['outcomes'][mode])**2
                          for r in held)/len(held) for mode in ARMS}
        cost_mae = {mode:{key:sum(abs(predictions[r['id']]['cost'][mode][key] -
                                     r['actual'][mode][key]) for r in held)/len(held)
                          for key in RESOURCES} for mode in ARMS}
        folds.append({'held_group':group,'train_groups':sorted({r['group'] for r in train}),
                      'train_checkpoint_ids':[r['id'] for r in train],
                      'predictions':predictions,'outcome_brier':brier,'resource_mae':cost_mae,
                      'policy_replay':{
                          name:{policy:replay(held,predictions,limit,policy) for policy in BASELINES}
                          for name,limit in REPLAY_BUDGETS.items()},
                      'zero_increment_mae':sum(abs(r['outcomes']['independent_review'] -
                                                   r['outcomes']['self_review']) for r in held)/len(held),
                      'paired_independent_mae':sum(
                          abs(predictions[r['id']]['delta_independent_vs_self'] -
                          (r['outcomes']['independent_review']-r['outcomes']['self_review']))
                          for r in held)/len(held)})
    return {'scope':'observed-arm development replay with grouped cross-fitting',
            'online_efficacy_established':False,'deployment_eligible':False,
            'protocol_sha256':digest(protocol),'report_sha256':digest(report),
            'features':FEATURES,'comparable_checkpoints':len(rows),'excluded':excluded,
            'mechanism_screen':paired_summary(report['blocks']),'replay_budgets':REPLAY_BUDGETS,
            'folds':folds,
            'group_mean_brier':{mode:sum(f['outcome_brier'][mode] for f in folds)/len(folds) for mode in ARMS},
            'group_mean_paired_independent_mae':sum(f['paired_independent_mae'] for f in folds)/len(folds),
            'group_mean_zero_increment_mae':sum(f['zero_increment_mae'] for f in folds)/len(folds),
            'full_development_fit':fit_heads(rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('protocol',type=Path)
    parser.add_argument('trials',type=Path)
    parser.add_argument('output',type=Path)
    args = parser.parse_args()
    report = analyze(json.loads(args.protocol.read_text()),json.loads(args.trials.read_text()))
    write(args.output,report)
    print('development checkpoints',report['comparable_checkpoints'],'folds',len(report['folds']))


if __name__=='__main__':
    main()
