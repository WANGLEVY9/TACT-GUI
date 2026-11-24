"""Explicit estimands: app-cluster intervals and logged-policy evaluation."""
import math
import random
from collections import defaultdict


def paired_cluster_interval(rows, seed=0, draws=2000):
    """Mean per-app paired improvement; seeds within an app are dependent."""
    if not rows or draws < 100: raise ValueError('insufficient evaluation inputs')
    groups = defaultdict(list)
    for row in rows:
        delta = row['method']-row['baseline']
        if not math.isfinite(delta): raise ValueError('nonfinite outcome')
        groups[row['app_group_id']].append(delta)
    means = [sum(v)/len(v) for v in groups.values()]
    if len(means) < 2: raise ValueError('at least two independent apps required')
    rng = random.Random(seed)
    samples = sorted(sum(rng.choice(means) for _ in means)/len(means) for _ in range(draws))
    return {'mean_app_difference':sum(means)/len(means),
            'interval_95':[samples[int(.025*draws)],samples[min(draws-1,int(.975*draws))]],
            'apps':len(means), 'draws':draws, 'seed':seed}


def logged_policy_value(rows):
    """Unclipped one-step IPS and DR. Not valid for arbitrary full campaigns.

    Assumes overlap, correctly logged behavior probabilities, and a fixed target
    policy. q_logged and q_target must be cross-fitted predictions. Sequential
    state interference requires a different estimator / randomized experiment.
    """
    if not rows: raise ValueError('empty evaluation')
    ips = []; dr = []; weights = []
    for row in rows:
        p, target = row['behavior_probability'], row['target_probability']
        values = [p,target,row['reward'],row['q_logged'],row['q_target']]
        if not all(math.isfinite(v) for v in values) or not 0 < p <= 1 or not 0 <= target <= 1:
            raise ValueError('invalid probabilities or outcomes')
        w = target/p
        weights.append(w); ips.append(w*row['reward'])
        dr.append(row['q_target']+w*(row['reward']-row['q_logged']))
    squared = sum(w*w for w in weights)
    return {'ips':sum(ips)/len(rows),'dr':sum(dr)/len(rows),
            'effective_sample_size':sum(weights)**2/squared if squared else 0,
            'max_weight':max(weights),'scope':'one-step logged-policy estimand'}
