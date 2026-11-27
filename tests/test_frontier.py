from itertools import product
import random
import unittest

from budgetmas.frontier import Option, best_plan, frontier


class FrontierTests(unittest.TestCase):
    def test_solver_matches_independent_enumeration(self):
        rng = random.Random(27)
        for _ in range(40):
            by_task = [
                [Option(str(task), str(mode), rng.randint(0, 4), rng.randint(0, 12),
                        rng.randint(0, 8) / 8, rng.randint(0, 40)) for mode in range(3)]
                for task in range(4)
            ]
            calls, operations = rng.randrange(10), rng.randrange(20)
            price = rng.choice((0., 1 / 64, 1 / 32))
            feasible = []
            for selected in product(*[[None, *task] for task in by_task]):
                choices = [pick for pick in selected if pick is not None]
                reserved = sum(o.calls for o in choices), sum(o.operations for o in choices)
                if reserved[0] <= calls and reserved[1] <= operations:
                    success = sum(o.expected_success for o in choices)
                    tokens = sum(o.expected_tokens for o in choices)
                    feasible.append((reserved, (success - price * tokens, success, -tokens)))
            opts = [opt for task in by_task for opt in task]
            plan = best_plan(opts, calls, operations, token_price=price)
            self.assertEqual(plan.priority(price), max(score for _, score in feasible))
            for point in frontier(opts, calls, operations, token_price=price):
                self.assertFalse(any(
                    c <= point.calls and d <= point.operations and score >= point.priority(price)
                    and (c < point.calls or d < point.operations or score > point.priority(price))
                    for (c, d), score in feasible))

    def test_rejects_duplicate_modes_and_can_skip(self):
        option = Option('test', 'mode', 2, 3, .9, 100)
        with self.assertRaises(ValueError):
            frontier([option, option], 2, 3)
        self.assertEqual(best_plan([option], 2, 3, token_price=.02).choices, ())
        self.assertEqual(best_plan([option], 2, 3).choices, (option,))
