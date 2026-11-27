from copy import deepcopy
import math
import unittest

from budgetmas.calibration import binomial_upper
from budgetmas.frontier import Option, best_plan
from budgetmas.paired_learning import FEATURES
from budgetmas.paired_uplift import ARMS, calibrate, evaluate, replay, selection_stats


def row(name, family, results=(1, 1, 0)):
    resources = {'model_calls':2,'device_steps':12}
    return {'id':name,'group':family,'x':[1]+[0]*(len(FEATURES)-1),'upper':resources,
            'outcomes':dict(zip(ARMS,results)),
            'actual':dict.fromkeys(ARMS,resources),
            'cost_cny':dict(zip(ARMS,(.001,.001,.02)))}


def scores(rows):
    return {r['id']:{'probability':dict.fromkeys(ARMS,.9),'cost_cny':r['cost_cny']}
            for r in rows}


class PairedUpliftTests(unittest.TestCase):
    def test_exact_upper_bound_and_empty_sample(self):
        self.assertIsNone(binomial_upper(0,0,.1))
        self.assertAlmostEqual(binomial_upper(0,3,.1),1-.1**(1/3))
        self.assertAlmostEqual(binomial_upper(1,2,.1),math.sqrt(.9))
        self.assertEqual(binomial_upper(2,2,.1),1.)

    def test_repeated_cases_in_one_family_cannot_certify_a_gate(self):
        repeated = [row(str(i),'same',(1,1,1)) for i in range(24)]
        estimates = scores(repeated)
        stat = selection_stats(repeated,estimates,.8)
        self.assertEqual(stat['active_families'],1)
        self.assertEqual(stat['selected_options'],72)
        self.assertIsNone(calibrate(repeated,estimates)['threshold'])
        separate = [row(str(i),str(i),(1,1,1)) for i in range(16)]
        self.assertEqual(calibrate(separate,scores(separate))['threshold'],.8)

    def test_held_out_outcomes_cannot_change_head_or_gate_for_that_fold(self):
        original = [row(x,x) for x in ('a','b','c','d','e')]
        modified = deepcopy(original)
        modified[0]['outcomes'][ARMS[0]] = 0
        before = evaluate(original,9)['folds'][0]
        after = evaluate(modified,9)['folds'][0]
        self.assertEqual(before['held_family'],'a')
        self.assertNotIn('a',before['train_families']+before['calibration_families'])
        for key in ('fitted_heads','predictions','calibration'):
            self.assertEqual(before[key],after[key])
        self.assertNotEqual(before['held_risk_coverage'],after['held_risk_coverage'])

    def test_budget_replay_uses_cost_and_abstains_if_no_calibrated_threshold(self):
        pool = [row('example','a')]
        limits = pool[0]['upper']
        predictions = scores(pool)
        predictions['example']['probability'][ARMS[-1]] = .95
        replayed = replay(pool,predictions,limits,'gain_frontier')
        self.assertNotEqual(replayed['decisions'][0]['mode'],ARMS[-1])
        gated = replay(pool,predictions,limits,'calibrated_frontier')
        self.assertEqual((gated['admitted'],gated['not_admitted']),(0,1))

    def test_monetary_price_selects_cheaper_option_under_identical_reservations(self):
        choices = [Option('task','cheap',2,8,.7,expected_cny=.001),
                   Option('task','strong',2,8,.8,expected_cny=.02)]
        self.assertEqual(best_plan(choices,2,8).choices[0].mode,'strong')
        self.assertEqual(best_plan(choices,2,8,cny_price=8).choices[0].mode,'cheap')
