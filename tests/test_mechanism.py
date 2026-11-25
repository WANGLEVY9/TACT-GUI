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

    def test_guard_prompt_uses_visible_progress_and_oracle_is_metric_only(self):
        from budgetmas.mechanism import GUARDED_DIRECT,STOP_PROMPT
        case = {'id':'guard','app_group':'sample','prefix':[],'goal':'Open settings',
                'start':{'text':'START'},'oracle':{'text':'HIDDEN-EXPECTED'},
                'slots':2,'measure_progress':True}
        contexts = []

        class Device:
            def __init__(self, android):
                self.android=self

            def launch(self, app):
                self.index=0

            def screenshot(self):
                return PNG

            def execute(self, raw, width, height):
                self.index+=1

            def nodes(self):
                text=('START','HIDDEN-EXPECTED','OTHER')[self.index]
                return list(fromstring(f'<hierarchy><node package="test.demo" text="{text}" '
                                       'bounds="[0,0][40,40]"/></hierarchy>').iter('node'))

        class Provider:
            def __init__(self, ledger):
                self.ledger=ledger
                self.count=0

            def predict(self, screen, context, *, max_tokens, advice):
                self.count+=1
                rid=str(self.count)
                amount={'model_calls':1,'device_steps':0}
                self.ledger.reserve(rid,amount)
                self.ledger.settle(rid,amount)
                contexts.append(context)
                if advice:
                    return {'text':'Open settings'}
                if self.count==2 and STOP_PROMPT in context:
                    return {'text':tool(action='terminate',status='success')}
                return {'text':tool(action='left_click',coordinate=[10,10])}

        with tempfile.TemporaryDirectory() as folder, patch('budgetmas.mechanism.Device',Device):
            cfg={'schema':1,'seed':4,'arms':[*ARMS,GUARDED_DIRECT],'checkpoints':[case]}
            result=run(cfg,{'sample':{'package':'test.demo'}},None,Path(folder)/'result',provider_factory=Provider)
        arms={arm['arm']:arm for arm in result['blocks'][0]['arms']}
        self.assertTrue(arms['direct']['progress']['lost_goal'])
        self.assertEqual(arms['direct']['progress']['first_confirmed_action'],1)
        self.assertEqual(arms[GUARDED_DIRECT]['success'],1)
        self.assertFalse(arms[GUARDED_DIRECT]['progress']['lost_goal'])
        self.assertTrue(all('HIDDEN-EXPECTED' not in context for context in contexts))
        self.assertEqual(sum(STOP_PROMPT in context for context in contexts),2)
        self.assertEqual(result['budget']['pending_reservations'],[])

    def test_two_device_workers_share_one_run_budget(self):
        from types import SimpleNamespace

        class Device:
            def __init__(self, android):
                self.android=self
                self.package=android.serial

            def launch(self, app):
                assert app['package']==self.package

            def nodes(self):
                return list(fromstring(f'<hierarchy><node package="{self.package}" text="START" '
                                       'bounds="[0,0][40,40]"/></hierarchy>').iter('node'))

            def screenshot(self):
                return PNG

        class Provider:
            def __init__(self, ledger):
                self.ledger=ledger
                self.count=0

            def predict(self, screen, context, *, max_tokens, advice):
                self.count+=1
                amount={'model_calls':1,'device_steps':0}
                self.ledger.reserve(str(self.count),amount)
                self.ledger.settle(str(self.count),amount)
                return {'text':'stop' if advice else tool(action='terminate')}

        names=('first','second')
        cases=[{'id':name,'app_group':name,'prefix':[],'goal':'Open settings',
                'start':{'text':'START'},'oracle':{'text':'DONE'}} for name in names]
        with tempfile.TemporaryDirectory() as folder, patch('budgetmas.mechanism.Device',Device):
            report=run({'schema':1,'seed':4,'checkpoints':cases},{name:{'package':name} for name in names},
                       {name:SimpleNamespace(serial=name) for name in names},
                       Path(folder)/'result',provider_factory=Provider)
        self.assertEqual([b['device'] for b in report['blocks']],list(names))
        self.assertEqual(report['budget']['limits']['model_calls'],12)
        self.assertEqual(report['budget']['spent']['model_calls'],10)
        self.assertFalse(report['budget']['pending_reservations'])
