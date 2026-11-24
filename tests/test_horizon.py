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
