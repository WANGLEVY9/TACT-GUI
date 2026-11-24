import tempfile
from pathlib import Path
from collections import Counter
from xml.etree.ElementTree import fromstring
import unittest
from unittest.mock import patch

from budgetmas.mechanism import ARMS, TOKENS_PER_CALL, run, user_context
from test_gui import PNG, tool


class TestMechanism(unittest.TestCase):
    def test_paired_budget_protocol_and_replay(self):
        case = {'id':'case','app_group':'sample','prefix':[],'goal':'Open settings',
                'start':{'text':'START'},'oracle':{'text':'DONE'}}
        app = {'package':'test.demo'}
        events = []

        class Device:
            def __init__(self, android):
                self.android = self

            def launch(self, app):
                self.text = 'START'

            def nodes(self):
                return list(fromstring('<hierarchy><node package="test.demo" text="' + self.text +
                                       '" bounds="[0,0][40,40]"/></hierarchy>').iter('node'))

            def screenshot(self):
                return PNG

            def execute(self, raw, width, height):
                self.text = 'DONE'

        class Provider:
            def __init__(self, ledger):
                self.ledger = ledger
                self.count = 0

            def predict(self, screen, context, *, max_tokens, advice):
                self.count += 1
                used = {'model_calls':1,'device_steps':0}
                self.ledger.reserve(str(self.count),used)
                self.ledger.settle(str(self.count),used)
                events.append((advice,max_tokens,context))
                return {'text':'Tap settings' if advice else tool(action='left_click',coordinate=[10,10])}

        with tempfile.TemporaryDirectory() as folder, patch('budgetmas.mechanism.Device',Device):
            result = run({'schema':1,'seed':3,'checkpoints':[case]}, {'sample':app}, None,
                         Path(folder)/'result',provider_factory=Provider)
        self.assertTrue(result['blocks'][0]['valid_pair'])
        self.assertEqual({arm['arm'] for arm in result['blocks'][0]['arms']},set(ARMS))
        self.assertEqual([arm['success'] for arm in result['blocks'][0]['arms']],[1,1,1])
        self.assertEqual(Counter(advice for advice,_,_ in events),{True:2,False:4})
        self.assertTrue(all(limit==TOKENS_PER_CALL for _,limit,_ in events))
        self.assertEqual(result['paired_summary']['comparisons']['self_review']['both_success'],1)
        self.assertEqual(result['budget']['spent']['model_calls'],6)
        self.assertEqual(result['budget']['pending_reservations'],[])

    def test_user_context_cannot_include_oracle_fields(self):
        context = user_context({'goal':'Find the settings page','prefix':['Menu'],
                                'oracle':{'text':'secret verifier label'}},[])
        self.assertIn('Menu',context)
        self.assertNotIn('secret verifier label',context)
