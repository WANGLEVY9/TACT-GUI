"""Replay a cost-aware task allocator on application-held-out matched arms."""
import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path

from .frontier import Option, best_plan
from .mechanism import ARMS, GUARDED_DIRECT, caps, registered_arms
from .paired_learning import FEATURES, dot, encode, ridge
from .pilot import digest, write


MODES = (*ARMS, GUARDED_DIRECT)
CONTRASTS = {
    'direct': ('direct', None),
    'guarded_direct': ('guarded_direct', 'direct'),
    'self_review': ('self_review', 'direct'),
    'independent_review': ('independent_review', 'self_review'),
}
POLICIES = (*MODES, 'execution_slot_rule', 'greedy', 'frontier', 'frontier_zero_price')
CAP_LEVELS = {'tight':min, 'medium':max, 'full':sum}
TOKEN_PRICE = 0.00001


def ingest(campaign, results):
    batches = json.loads(campaign.read_text())['batches']
    index = {}
    exclusions = []
    rounds = Counter()
    for batch in batches:
        case_id = batch['case']
        run_id = batch['run']
        root = (results / run_id).resolve()
        if not root.is_relative_to(results.resolve()) or batch['batch'] in index:
            raise ValueError('duplicate or invalid campaign path')
        cycle = rounds[case_id]
        rounds[case_id] += 1
        protocol = json.loads((root / 'protocol.json').read_text())
        if protocol.get('platform', 'android') != 'android' or len(protocol['checkpoints']) != 1:
            raise ValueError('expected Android checkpoint campaign')
        case, = protocol['checkpoints']
        if (case['id'] != case_id or protocol['seed'] != batch['seed']
                or set(registered_arms(protocol)) != set(MODES)):
            raise ValueError('campaign does not match registered arms')
        report_path = root / 'report.json'
        record = {'id':case_id, 'run':run_id, 'batch':batch['batch'],
                  'group':case['app_group'], 'repeat':cycle, 'eligible':False}
        index[batch['batch']] = record
        if not report_path.is_file():
            exclusions.append({**record, 'reason':'interrupted before final report'})
            continue
        report = json.loads(report_path.read_text())
        if digest(protocol) != report['protocol_sha256'] or len(report['blocks']) != 1:
            raise ValueError('invalid campaign report')
        block, = report['blocks']
        arms = {row['arm']: row for row in block['arms']}
        if block['checkpoint'] != case_id or len(block['arms']) != len(MODES) or arms.keys() != set(MODES):
            raise ValueError('incomplete or duplicate registered arm')
        if (not block['valid_pair'] or len({a.get('initial_hash') for a in arms.values()}) != 1
                or len({digest(a.get('state')) for a in arms.values()}) != 1
                or any(not a.get('state') for a in arms.values())):
            exclusions.append({**record, 'reason':'missing matched initial observation or outcome'})
            continue
        upper = caps(case)
        tokens, outcomes, actual = {}, {}, {}
        for mode, arm in arms.items():
            check = arm['verification']
            used = arm['actual']
            flag = arm['success']
            if (arm['checkpoint'] != case_id or arm['group'] != case['app_group']
                    or check['observed'] != flag or check['replay'] != flag or flag not in (0, 1)
                    or arm['initial_hash'] != check['replay_initial_hash'] or arm['upper'] != upper
                    or used.keys() != upper.keys() or any(
                        not 0 <= used[k] <= upper[k] or int(used[k]) != used[k] for k in upper)
                    or used['model_calls'] != len(arm['calls'])):
                raise ValueError('report violates checkpoint or accounting contract')
            if any(not c.get('usage_complete') for c in arm['calls']):
                break
            tokens[mode] = sum(c['usage']['total_tokens'] for c in arm['calls'])
            actual[mode] = {key:int(amount) for key, amount in used.items()}
            outcomes[mode] = flag
        if tokens.keys() != arms.keys():
            exclusions.append({**record, 'reason':'incomplete token measurements'})
            continue
        record.update(eligible=True, x=encode(case, arms['direct']['state']), upper=upper,
                      outcomes=outcomes, actual=actual, tokens=tokens)
    return list(index.values()), exclusions


def fit(rows):
    """Shrink paired reward increments and fit cost heads using training families only."""
    heads = {}
    costs = {}
    for mode, (chosen, reference) in CONTRASTS.items():
        targets = [r['outcomes'][chosen] - (r['outcomes'][reference] if reference else 0)
                   for r in rows]
        heads[mode] = ridge(rows, targets, prior=1. if reference else .1)
        costs[mode] = ridge(rows, [math.log1p(r['tokens'][mode]) for r in rows], prior=.1)
    return heads, costs


def options_for(row, model):
    heads, costs = model
    values = {}
    for mode, (_, reference) in CONTRASTS.items():
        values[mode] = dot(heads[mode], row['x']) + (values[reference] if reference else 0.)
    return [Option(row['id'], mode, row['upper']['model_calls'], row['upper']['device_steps'],
                   min(1., max(0., values[mode])),
                   math.expm1(max(0., dot(costs[mode], row['x'])))) for mode in MODES]


def simulate(rows, options, limit, policy, token_price=TOKEN_PRICE):
    pending = {row['id']:row for row in rows}
    budget = dict(limit)
    picks = []
    tokens = 0
    verified = 0
    while pending:
        feasible = [opt for opt in options if opt.task in pending and opt.calls <= budget['model_calls']
                    and opt.operations <= budget['device_steps']]
        if not feasible:
            break
        if policy in ('frontier', 'frontier_zero_price'):
            price = token_price if policy == 'frontier' else 0.
            plan = best_plan(feasible, budget['model_calls'], budget['device_steps'], token_price=price)
            choice = plan.choices[0] if plan.choices else None
        elif policy == 'greedy':
            def utility(option):
                return option.expected_success - token_price * option.expected_tokens
            choice = max(feasible, key=lambda option: utility(option) / option.calls)
            if utility(choice) <= 0:
                choice = None
        elif policy == 'execution_slot_rule':
            task = pending[feasible[0].task]
            mode = 'guarded_direct' if task['upper']['model_calls'] == 2 or task['x'][4] else 'self_review'
            choice = next(opt for opt in feasible if opt.task == task['id'] and opt.mode == mode)
        else:
            choice = next(opt for opt in feasible if opt.mode == policy)
        if choice is None:
            break
        row = pending.pop(choice.task)
        measured = row['actual'][choice.mode]
        for resource, amount in measured.items():
            budget[resource] -= amount
        reward = row['outcomes'][choice.mode]
        cost = row['tokens'][choice.mode]
        picks.append({'task':choice.task, 'mode':choice.mode, 'verified':reward, 'tokens':cost})
        tokens += cost
        verified += reward
    return {'verified':verified, 'tokens':tokens, 'admitted':len(picks),
            'not_admitted':len(pending), 'spent':{k:limit[k]-budget[k] for k in budget}, 'picks':picks}


def evaluate(index, exclusions):
    candidates = [row for row in index if row['eligible']]
    groups = sorted({row['group'] for row in candidates})
    if len(groups) < 2:
        raise ValueError('need at least two comparable application families')
    folds = []
    for group in groups:
        training = [row for row in candidates if row['group'] != group]
        model = fit(training)
        repeats = defaultdict(list)
        for row in candidates:
            if row['group'] == group:
                repeats[row['repeat']].append(row)
        cases = {row['id'] for row in index if row['group'] == group}
        for repeat, rows in sorted(repeats.items()):
            # Freeze the denominator. An incomplete repeat is excluded as a pool.
            if {row['id'] for row in rows} != cases:
                continue
            options = [opt for row in rows for opt in options_for(row, model)]
            limits = {name:{key:combine(row['upper'][key] for row in rows) for key in rows[0]['upper']}
                      for name, combine in CAP_LEVELS.items()}
            folds.append({'family':group, 'repeat':repeat, 'train_count':len(training),
                          'tasks':sorted(cases), 'limits':limits,
                          'policies':{name:{policy:simulate(rows, options, cap, policy)
                                            for policy in POLICIES} for name, cap in limits.items()}})
    aggregate = {level:{policy:{
        key:sum(fold['policies'][level][policy][key] for fold in folds)
        for key in ('verified','tokens','admitted','not_admitted')}
                         for policy in POLICIES} for level in CAP_LEVELS}
    return {'scope':'Android development replay on matched observed arms',
            'training_features':FEATURES, 'families':groups, 'registered_batches':len(index),
            'comparable_batches':len(candidates), 'excluded':exclusions, 'evaluated_pools':len(folds),
            'token_price_per_token':TOKEN_PRICE, 'aggregate':aggregate, 'folds':folds}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('summary', type=Path)
    parser.add_argument('out', type=Path)
    parser.add_argument('--results', type=Path, default=Path('results'))
    args = parser.parse_args()
    report = evaluate(*ingest(args.summary, args.results))
    report['campaign_summary_sha256'] = digest(json.loads(args.summary.read_text()))
    write(args.out, report)
    print(json.dumps({k:report[k] for k in ('registered_batches','comparable_batches','evaluated_pools',
                                           'aggregate')}, indent=2))


if __name__ == '__main__':
    main()
