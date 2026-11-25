import unittest

from budgetmas.horizon import crossover,interactions
from budgetmas.pilot import digest


class Horizon(unittest.TestCase):
    def test_crossover_controls_checkpoint_and_excludes_initial_drift(self):
        base={'schema':1,'checkpoints':[{'id':'case','app_group':'app','prefix':[]}]}
        protocol=crossover(base,5)
        blocks=[]
        for case in protocol['checkpoints']:
            blocks.append({'checkpoint':case['id'],'valid_pair':True,'arms':[
                {'arm':role,'initial_hash':'same','success':y} for role,y in
                [('direct',1),('self_review',int(case['slots']==3)),('independent_review',1)]]})
        report={'protocol_sha256':digest(protocol),'blocks':blocks}
        row=interactions(protocol,report)['checkpoint_interactions'][0]
        self.assertEqual((row['review_by_horizon'],row['independent_by_horizon']),(1,-1))
        report['blocks'][1]['arms'][0]['initial_hash']='other'
        self.assertEqual(interactions(protocol,report)['comparable'],0)

    def test_guard_effect_and_goal_loss_use_complete_crossover(self):
        arms=['direct','guarded_direct','self_review','independent_review']
        base={'schema':1,'arms':arms,'checkpoints':[{'id':'case','app_group':'app','prefix':[]}]}
        protocol=crossover(base,10)
        blocks=[]
        for case in protocol['checkpoints']:
            outcomes={'direct':case['slots']==3,'guarded_direct':True,'self_review':True,'independent_review':True}
            blocks.append({'checkpoint':case['id'],'device':'same-device','valid_pair':True,'arms':[
                {'arm':mode,'initial_hash':'same','success':int(outcomes[mode]),
                 'progress':{'stable':True,'first_confirmed_action':1,'lost_goal':not outcomes[mode]}}
                for mode in arms]})
        report={'protocol_sha256':digest(protocol),'blocks':blocks}
        result=interactions(protocol,report)
        self.assertEqual(result['checkpoint_interactions'][0]['guard_by_horizon'],-1)
        self.assertEqual(result['outcomes_by_horizon']['2']['direct']['lost_goal'],1)
        self.assertEqual(result['outcomes_by_horizon']['2']['guarded_direct']['terminal_success'],1)
        blocks[0]['device']='different-device'
        self.assertEqual(interactions(protocol,report)['outcomes_by_horizon'],{})
