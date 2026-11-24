from copy import deepcopy
import unittest

from budgetmas.mechanism import ARMS,caps
from budgetmas.paired_learning import analyze, encode, extract, replay
from budgetmas.pilot import digest


def fixture():
    cases = [{'id':g+'-case','app_group':g,'slots':slots,'prefix':[],
              'goal':'Show the result','oracle':{'text':'secret'}}
             for g,slots in [('one',2),('two',3),('three',2)]]
    protocol={'schema':1,'checkpoints':cases}
    state={'nodes':10,'labelled_nodes':4,'clickable_nodes':4,'duplicate_visible_labels':1}
    blocks=[]
    for index,case in enumerate(cases):
        blocks.append({'checkpoint':case['id'],'valid_pair':True,'arms':[
            {'arm':arm,'group':case['app_group'],'checkpoint':case['id'],'upper':caps(case),
             'actual':{'model_calls':2,'device_steps':8},'state':state.copy(),'initial_hash':'h',
             'success':int(arm=='direct' or (index==1 and arm=='independent_review'))}
            for arm in ARMS]})
    return protocol,{'protocol_sha256':digest(protocol),'blocks':blocks}


class PairedLearning(unittest.TestCase):
    def test_predecision_features_ignore_oracle(self):
        protocol,report=fixture()
        case=protocol['checkpoints'][0]
        state=report['blocks'][0]['arms'][0]['state']
        x=encode(case,state)
        changed={**case,'oracle':{'text':'a different answer'}}
        self.assertEqual(x,encode(changed,state))
        self.assertNotEqual(x,encode({**case,'slots':3},state))

    def test_crossfit_by_group_and_budget_limits(self):
        protocol,report=fixture()
        learned=analyze(protocol,report)
        self.assertFalse(learned['deployment_eligible'])
        self.assertEqual(len(learned['folds']),3)
        for fold in learned['folds']:
            self.assertNotIn(fold['held_group'],fold['train_groups'])
            self.assertNotIn(fold['held_group']+'-case',fold['train_checkpoint_ids'])
            for budget,policies in fold['policy_replay'].items():
                for result in policies.values():
                    self.assertTrue(all(value<=learned['replay_budgets'][budget][key]
                                        for key,value in result['spent'].items()))

    def test_mismatched_features_exclude_entire_block_and_tamper_rejected(self):
        protocol,report=fixture()
        report['blocks'][0]['arms'][0]['state']['nodes']+=1
        rows,excluded=extract(protocol,report)
        self.assertEqual(len(rows),2)
        self.assertEqual(excluded[0]['id'],'one-case')
        invalid=deepcopy(report)
        invalid['blocks'][1]['arms'][0]['actual']['model_calls']=4
        with self.assertRaisesRegex(ValueError,'measured resources'):
            extract(protocol,invalid)

    def test_admission_uses_upper_bound_not_measured_spend(self):
        protocol,report=fixture()
        rows,_=extract(protocol,report)
        result=replay([rows[1]],{}, {'model_calls':2,'device_steps':30},'direct')
        self.assertEqual(result['admitted'],0)
        self.assertEqual(result['not_admitted'],1)
