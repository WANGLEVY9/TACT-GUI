"""Exact budget frontier for independent GUI checkpoint configurations."""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class Option:
    task: str
    mode: str
    calls: int
    operations: int
    expected_success: float
    expected_tokens: float = 0.
    expected_cny: float = 0.


@dataclass(frozen=True)
class Plan:
    calls: int = 0
    operations: int = 0
    expected_success: float = 0.
    expected_tokens: float = 0.
    choices: tuple[Option, ...] = ()
    expected_cny: float = 0.

    def priority(self, token_price=0., cny_price=0.):
        score = self.expected_success - token_price*self.expected_tokens - cny_price*self.expected_cny
        return (score, self.expected_success, -(self.expected_cny if cny_price else self.expected_tokens))


def undominated(plans, token_price, cny_price):
    """Sweep calls; query prefix-best utility over compressed operation counts."""
    ranks = {steps: pos for pos, steps in enumerate(sorted({p.operations for p in plans}), 1)}
    best = [(-math.inf,)] * (len(ranks) + 1)
    for plan in sorted(plans, key=lambda p: (p.calls, p.operations)):
        i = ranks[plan.operations]
        priority = plan.priority(token_price,cny_price)
        while i and best[i] < priority:
            i -= i & -i
        if i:
            continue
        yield plan
        i = ranks[plan.operations]
        while i < len(best):
            best[i] = max(best[i], priority)
            i += i & -i


def frontier(options, calls, operations, *, token_price=0., cny_price=0.):
    """Keep at most one arm per task and include skipping at every task.

    The bounded dimensions are executable integer reservations. Token and
    CNY prices are optional soft preferences, in their respective units.
    """
    if (type(calls) is not int or calls < 0 or type(operations) is not int or operations < 0
            or any(not math.isfinite(p) or p < 0 for p in (token_price,cny_price))):
        raise ValueError('invalid frontier budget or price')
    tasks = {}
    identities = set()
    for option in options:
        key = option.task, option.mode
        if (key in identities or type(option.calls) is not int or option.calls < 0
                or type(option.operations) is not int or option.operations < 0
                or not 0 <= option.expected_success <= 1
                or any(not math.isfinite(v) or v < 0 for v in (option.expected_tokens,option.expected_cny))):
            raise ValueError('invalid or duplicate checkpoint option')
        identities.add(key)
        tasks.setdefault(option.task, []).append(option)

    plans = [Plan()]
    for modes in tasks.values():
        cells = {(plan.calls, plan.operations): plan for plan in plans}
        for plan in plans:
            for option in modes:
                use = plan.calls + option.calls, plan.operations + option.operations
                if use[0] > calls or use[1] > operations:
                    continue
                new = Plan(*use, plan.expected_success + option.expected_success,
                           plan.expected_tokens + option.expected_tokens, (*plan.choices, option),
                           plan.expected_cny + option.expected_cny)
                existing = cells.get(use)
                if existing is None or new.priority(token_price,cny_price) > existing.priority(token_price,cny_price):
                    cells[use] = new
        plans = list(undominated(cells.values(), token_price,cny_price))
    return plans


def best_plan(options, calls, operations, *, token_price=0., cny_price=0.):
    plans = frontier(options, calls, operations, token_price=token_price,cny_price=cny_price)
    return max(plans, key=lambda plan: (*plan.priority(token_price,cny_price), -plan.calls, -plan.operations))
