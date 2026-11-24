import itertools
import json
from pathlib import Path
import random
import tempfile
import unittest
from budgetmas.models import BayesianHead, JointModel
from budgetmas.decision import Action, BudgetPlanner, PrimalDual, randomized_action
from budgetmas.training import fit
from budgetmas.runtime import Journal, TeamExecutor
from budgetmas.fixture import Cart, Proposer, Critic, DifferentialVerifier
from budgetmas.ledger import Ledger
from budgetmas.evaluation import logged_policy_value, paired_cluster_interval


class Research(unittest.TestCase):
    def test_full_posterior_correlated_features(self):
        h=BayesianHead(2)
        h.update((1,1),3)
        mean,sd=h.predict((1,1))
        self.assertAlmostEqual(mean,2)
        self.assertAlmostEqual(sd**2,2/3)
        self.assertAlmostEqual(h.predict((1,-1))[1]**2,2)

    def test_posterior_contracts(self):
        h=BayesianHead(2)
        for x in ((1,), (float('nan'),1)):
            with self.assertRaises(ValueError): h.predict(x)
        with self.assertRaises(ValueError): JointModel(2,['reward'])

    def test_checkpoint_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            m=JointModel(2,['calls']); m.update((1,1),1,{'calls':3})
            path=Path(d)/'model.json'; m.save(path)
            self.assertEqual(m.predict((1,2)), JointModel.load(path).predict((1,2)))
            with self.assertRaises(FileExistsError): m.save(path)

    def test_batch_optimum_and_conflicts(self):
        class Known:
            def predict(self,x,beta): return {'reward':x[0], 'uncertainty':0, 'costs':{'calls':{'upper':1}}}
        a=Action('a','t1','single',(6,),{'calls':4},workers=('w1',))
        b=Action('b','t2','single',(5,),{'calls':2},workers=('w2',))
        c=Action('c','t3','single',(5,),{'calls':2},workers=('w3',))
        planner=BudgetPlanner(Known(),beta=0)
        result=planner.plan([a,b,c],{'calls':4},slots=2)
        self.assertEqual({x.id for x in result['actions']},{'b','c'})
        conflict=Action('d','t2','pair',(20,),{'calls':2},workers=('w3',))
        self.assertEqual(len(planner.plan([b,c,conflict],{'calls':4},slots=3)['actions']),1)

    def test_planner_matches_bruteforce(self):
        class Known:
            def predict(self,x,beta): return {'reward':x[0], 'uncertainty':0, 'costs':{'calls':{'upper':1}}}
        rng=random.Random(2)
        for _ in range(20):
            actions=[Action(str(i),str(i),'single',(rng.random(),),{'calls':rng.randint(1,4)}) for i in range(6)]
            expected=max(sum(a.features[0] for a in subset) for n in range(4) for subset in itertools.combinations(actions,n) if sum(a.upper['calls'] for a in subset)<=5)
            self.assertAlmostEqual(BudgetPlanner(Known(),beta=0).plan(actions,{'calls':5},slots=3)['score'],expected)

    def test_shadow_prices(self):
        dual=PrimalDual(['calls'])
        dual.update({'calls':5},{'calls':1}); self.assertGreater(dual.prices['calls'],0)
        for _ in range(10): dual.update({'calls':0},{'calls':1})
        self.assertEqual(dual.prices['calls'],0)

    def test_propensity(self):
        actions=['a','b']
        for seed in range(10):
            a,p=randomized_action(actions,random.Random(seed),.2,'a')
            self.assertAlmostEqual(p,.9 if a=='a' else .1)

    def test_training_leakage_and_unresolved(self):
        row={'id':'a','app_group_id':'app1','split':'train','features':[1,0],'verified_reward':1,'verification_status':'confirmed','actual':{'calls':1}}
        val=dict(row,id='b',split='validation',app_group_id='app2')
        m,report=fit([row],[val],2,['calls']); self.assertEqual(report['train_rows'],1)
        with self.assertRaises(ValueError): fit([row],[dict(val,app_group_id='app1')],2,['calls'])
        with self.assertRaises(ValueError): fit([dict(row,verification_status='unresolved')],[val],2,['calls'])

    def test_team_verification_and_dedup(self):
        with tempfile.TemporaryDirectory() as d:
            ledger=Ledger(Path(d)/'db',{'operations':8})
            journal=Journal(Path(d)/'events')
            team=TeamExecutor(ledger,journal,Cart(),Proposer(),Critic(),DifferentialVerifier())
            a=Action('a','task','team',(1,),{'operations':4})
            q={key:{'operations':1} for key in ('proposer','critic','execute','verify')}
            results=[team.run(a,q,initial_state='empty-cart',seed=i,split='train') for i in range(2)]
            self.assertEqual([r['reward'] for r in results],[1,0])
            self.assertEqual(ledger.available()['operations'],0)
            self.assertEqual(len(Journal.read(journal.path)),len(journal.path.read_text().splitlines()))
            journal.path.write_text(journal.path.read_text().replace('confirmed','unresolved',1))
            with self.assertRaises(ValueError): Journal.read(journal.path)

    def test_failure_retains_complete_trial_reservation(self):
        class Broken:
            def propose(self,*args): raise TimeoutError()
        with tempfile.TemporaryDirectory() as d:
            ledger=Ledger(Path(d)/'db',{'operations':4})
            team=TeamExecutor(ledger,Journal(Path(d)/'events'),Cart(),Broken(),None,DifferentialVerifier())
            q={key:{'operations':1} for key in ('proposer','execute','verify')}
            with self.assertRaises(TimeoutError): team.run(Action('a','t','s',(1,),{'operations':3}),q,initial_state='empty-cart',seed=0,split='train')
            self.assertEqual(ledger.available()['operations'],1)

    def test_campaign_connects_model_to_verified_execution(self):
        from budgetmas.campaign import Campaign
        with tempfile.TemporaryDirectory() as d:
            ledger=Ledger(Path(d)/'db',{'operations':4})
            team=TeamExecutor(ledger,Journal(Path(d)/'events'),Cart(),Proposer(),Critic(),DifferentialVerifier())
            model=JointModel(1,['operations'])
            for _ in range(10): model.update((1,),1,{'operations':4})
            planner=BudgetPlanner(model,beta=0)
            action=Action('a','cart','team',(1,),{'operations':4})
            quotas={'a':{r:{'operations':1} for r in ('proposer','critic','execute','verify')}}
            report=Campaign(planner,team).run([action],quotas,initial_states={'cart':'empty-cart'},split='train',seed=0)
            self.assertEqual(report['results'][0]['reward'],1)
            self.assertEqual(report['stop_reason'],'no_positive_feasible_action')

    def test_one_step_estimators(self):
        rows=[{'behavior_probability':.5,'target_probability':1,'reward':1,'q_logged':1,'q_target':1}, {'behavior_probability':.5,'target_probability':0,'reward':0,'q_logged':0,'q_target':1}]
        result=logged_policy_value(rows)
        self.assertEqual(result['ips'],1); self.assertEqual(result['dr'],1)
        self.assertEqual(result['effective_sample_size'],1)

    def test_cluster_aggregation(self):
        rows=[{'app_group_id':'a','method':1,'baseline':0}]*10+[{'app_group_id':'b','method':0,'baseline':0}]
        self.assertEqual(paired_cluster_interval(rows,draws=100)['mean_app_difference'],.5)
