"""Feasible batch selection with dependency, task and worker constraints."""
from dataclasses import dataclass
import math
import random


@dataclass(frozen=True)
class Action:
    id: str
    task: str
    configuration: str
    features: tuple
    upper: dict
    dependencies: tuple = ()
    workers: tuple = ()


class PrimalDual:
    """Projected resource shadow-price updates based on planned pacing."""
    def __init__(self, resources, learning_rate=0.1):
        if learning_rate <= 0 or not math.isfinite(learning_rate):
            raise ValueError('invalid learning rate')
        self.prices = dict.fromkeys(resources, 0.0)
        self.learning_rate = learning_rate

    def update(self, actual, target):
        if set(actual) != set(self.prices) or set(target) != set(actual):
            raise ValueError('resource schema mismatch')
        if any(not math.isfinite(v) or v < 0 for v in [*actual.values(), *target.values()]):
            raise ValueError('invalid pacing data')
        for key in actual:
            error = (actual[key]-target[key]) / max(target[key], 1.0)
            self.prices[key] = max(0.0, self.prices[key]+self.learning_rate*error)


class BudgetPlanner:
    def __init__(self, model, *, beta=1.0, prices=None, max_candidates=20):
        if not math.isfinite(beta) or beta < 0 or not 1 <= max_candidates <= 24:
            raise ValueError('invalid planner configuration')
        self.model, self.beta = model, beta
        self.prices = prices or {}
        self.max_candidates = max_candidates

    def plan(self, actions, available, completed=(), slots=1):
        if slots < 1 or any(not math.isfinite(v) or v < 0 for v in available.values()):
            raise ValueError('invalid capacity')
        if len(actions) > self.max_candidates:
            raise ValueError('candidate cap exceeded; explicit pruning required')
        if len({a.id for a in actions}) != len(actions):
            raise ValueError('duplicate candidate id')
        candidates = []
        scores = {}
        for a in actions:
            if set(a.upper) != set(available) or any(not math.isfinite(v) or v < 0 for v in a.upper.values()):
                raise ValueError('invalid upper bound')
            if not set(a.dependencies) <= set(completed) or any(a.upper[k] > available[k] for k in available):
                continue
            prediction = self.model.predict(a.features, self.beta)
            score = prediction.get('reward_lower', prediction['reward']-self.beta*prediction['uncertainty'])
            score -= sum(self.prices[k]*prediction['costs'][k]['upper'] for k in available if self.prices.get(k,0) != 0)
            if not math.isfinite(score):
                continue  # Uninformative calibrated bounds abstain, never force execution.
            scores[a.id] = score
            candidates.append(a)
        best_score, best = 0.0, []  # Empty plan is an explicit stop option.
        def search(index, selected, used, tasks, workers, value):
            nonlocal best_score, best
            if value > best_score:
                best_score, best = value, list(selected)
            if index == len(candidates) or len(selected) == slots:
                return
            if value + sum(max(0, scores[c.id]) for c in candidates[index:]) <= best_score:
                return
            a = candidates[index]
            search(index+1, selected, used, tasks, workers, value)
            if a.task in tasks or set(a.workers) & workers or any(used[k]+a.upper[k] > available[k] for k in used):
                return
            search(index+1, selected+[a], {k:used[k]+a.upper[k] for k in used},
                   tasks | {a.task}, workers | set(a.workers), value+scores[a.id])
        search(0, [], dict.fromkeys(available,0), set(), set(), 0.0)
        return {'actions': best, 'score': best_score, 'candidate_scores': scores}


def randomized_action(actions, rng, epsilon, preferred):
    """Epsilon mixture over a prefiltered feasible set; exact propensity logged."""
    if not actions or not 0 <= epsilon <= 1 or preferred not in actions:
        raise ValueError('invalid behavior policy')
    chosen = rng.choice(actions) if rng.random() < epsilon else preferred
    probability = epsilon/len(actions)+(1-epsilon if chosen == preferred else 0)
    return chosen, probability


class FixedOrderPlanner:
    """Transparent no-learning baseline, sharing the campaign planner contract."""
    def __init__(self): self.prices = {}

    def plan(self,actions,available,completed=(),slots=1):
        feasible = [a for a in actions if set(a.dependencies) <= set(completed)
                    and set(a.upper) == set(available)
                    and all(math.isfinite(v) and 0 <= v <= available[k] for k,v in a.upper.items())]
        return {'actions':feasible[:1], 'score':None,
                'candidate_scores':{a.id:1.0 for a in feasible}}
