import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
from budgetmas.android import Android, parse_action, png_dimensions
from budgetmas.gui_session import GUISession, crash_signature
from budgetmas.providers import GUIPlus
from budgetmas.ledger import Ledger
from budgetmas.runtime import Journal, TeamExecutor
from budgetmas.fixture import Cart, Proposer, Critic, DifferentialVerifier
from budgetmas.decision import Action
from budgetmas.calibration import residual_radius, CalibratedModel

PNG=b'\x89PNG\r\n\x1a\n'+b'\0\0\0\rIHDR'+struct.pack('>II',800,600)
LOG='FATAL EXCEPTION: main\nProcess: org.test.app, PID: 42\njava.lang.IllegalStateException: failed\n at org.test.app.Main.open(Main.java:8)\n'

def tool(**a): return '<tool_call>'+json.dumps({'name':'computer_use','arguments':a})+'</tool_call>'

class Device:
    def __init__(self, log=LOG): self.resets=0; self.actions=[]; self.log=log
    def reset(self,*args): self.resets+=1
    def screenshot(self): return PNG
    def execute(self,text,*size): self.actions.append(parse_action(text))
    def crash_log(self): return self.log

class Provider:
    def __init__(self,ledger): self.ledger=ledger; self.i=0
    def predict(self,*args):
        self.i+=1
        self.ledger.reserve(str(self.i),{'model_calls':1,'device_steps':0})
        self.ledger.settle(str(self.i),{'model_calls':1,'device_steps':0})
        return {'text':tool(action='left_click',coordinate=[200,300]),'usage':{'total_tokens':10}}

class GUI(unittest.TestCase):
    def test_actions_and_typing_shell_quote(self):
        device=Android('emulator-5554')
        with patch.object(device,'adb') as adb:
            device.execute(tool(action='type',text="a;$(id)'x",coordinate=[500,500]),800,600)
            self.assertEqual(adb.call_args_list[0].args,('shell','input','tap','400','300'))
            quoted=adb.call_args_list[-1].args[-1]
            self.assertTrue(quoted.startswith("'"))
            device.execute(tool(action='scroll',pixels=-100),800,600)
            self.assertEqual(adb.call_args.args[2],'swipe')
        for a in ({'action':'left_click','coordinate':[True,1]}, {'action':'key','keys':['Enter','Back']}, {'action':'scroll','pixels':0}, {'action':'type','text':'中文'}, {'action':'type','text':'50%'}, {'action':'left_click','coordinate':[1,1],'command':'id'}):
            with self.assertRaises(ValueError): parse_action(tool(**a))

    def test_reset_does_not_launch_after_failed_clear(self):
        device=Android('emulator-5554')
        with patch.object(device,'adb',side_effect=[b'',b'Failed']) as adb:
            with self.assertRaises(RuntimeError): device.reset('org.test.app','.Main')
            self.assertEqual(adb.call_count,2)

    def test_png_and_process_attribution(self):
        self.assertEqual(png_dimensions(PNG),(800,600))
        self.assertIsNotNone(crash_signature(LOG,'org.test.app'))
        self.assertIsNone(crash_signature(LOG,'org.other.app'))
        self.assertIsNone(crash_signature('Process: org.test.app\nno crash','org.test.app'))
        with self.assertRaises(ValueError): png_dimensions(b'not png')

    def test_gui_replay_complete_accounting(self):
        with tempfile.TemporaryDirectory() as d:
            ledger=Ledger(Path(d)/'db',{'model_calls':3,'device_steps':20})
            device=Device()
            report=GUISession(device,Provider(ledger),ledger,Journal(Path(d)/'events')).run('org.test.app','.Main','test',Path(d)/'run',max_steps=3)
            self.assertEqual(report['status'],'reproduced_crash')
            self.assertEqual(device.resets,3)
            # Discovery: reset/screenshot/action/log = 4; 2 replay*(reset/action/log)=6.
            self.assertEqual(report['available'],{'model_calls':2,'device_steps':10})
            self.assertEqual(len(device.actions),3)

    def test_model_failure_keeps_verification_reservation(self):
        class Broken:
            def predict(self,*args): raise TimeoutError()
        with tempfile.TemporaryDirectory() as d:
            ledger=Ledger(Path(d)/'db',{'model_calls':1,'device_steps':20})
            report=GUISession(Device(),Broken(),ledger,Journal(Path(d)/'events')).run('org.test.app','.Main','test',Path(d)/'run',max_steps=3)
            self.assertEqual(report['status'],'unresolved')
            self.assertEqual(report['error_type'],'TimeoutError')
            self.assertEqual(report['available']['device_steps'],8) # 10 replay + 2 discovery reserved/used

    def test_no_crash_is_not_negative_defect_label(self):
        with tempfile.TemporaryDirectory() as d:
            ledger=Ledger(Path(d)/'db',{'model_calls':1,'device_steps':20})
            report=GUISession(Device(log=''),Provider(ledger),ledger,Journal(Path(d)/'events')).run('org.test.app','.Main','test',Path(d)/'run',max_steps=1)
            self.assertEqual(report['status'],'no_crash_observed')
            self.assertNotIn('verified_reward',report)
            self.assertEqual(report['available']['device_steps'],16)

    def test_gateway_uses_shared_resource_schema_and_timeout_retention(self):
        with tempfile.TemporaryDirectory() as d:
            ledger=Ledger(Path(d)/'db',{'model_calls':2,'device_steps':3})
            with patch('budgetmas.providers.settings',return_value=('https://dashscope.aliyuncs.com/compatible-mode/v1','test-key')), patch('urllib.request.urlopen',side_effect=TimeoutError()):
                with self.assertRaises(TimeoutError): GUIPlus(ledger).predict(PNG,'test')
            self.assertEqual(ledger.available(),{'model_calls':1,'device_steps':3})

    def test_dedup_survives_executor_restart(self):
        with tempfile.TemporaryDirectory() as d:
            ledger=Ledger(Path(d)/'db',{'operations':8}); path=Path(d)/'events'
            q={r:{'operations':1} for r in ('proposer','critic','execute','verify')}
            action=Action('a','t','team',(1,),{'operations':4})
            for expected in (1,0):
                executor=TeamExecutor(ledger,Journal(path),Cart(),Proposer(),Critic(),DifferentialVerifier())
                self.assertEqual(executor.run(action,q,initial_state='empty-cart',seed=0,split='train')['reward'],expected)

    def test_conformal_rank_not_interpolated(self):
        self.assertEqual(residual_radius(list(range(9)),alpha=.2),7)
        self.assertEqual(residual_radius([1,2],alpha=.1),float('inf'))

    def test_ledger_audit_separates_unknown_from_spent(self):
        with tempfile.TemporaryDirectory() as d:
            ledger=Ledger(Path(d)/'db',{'calls':5})
            ledger.reserve('known',{'calls':2}); ledger.settle('known',{'calls':1})
            ledger.reserve('unknown',{'calls':2})
            report=ledger.audit()
            self.assertEqual(report['spent'],{'calls':1})
            self.assertEqual(report['reserved'],{'calls':2})
            self.assertEqual(report['available'],{'calls':2})
            self.assertEqual(report['pending_reservations'],['unknown'])

    def test_gateway_success_and_snapshot_mismatch(self):
        from io import BytesIO
        from budgetmas.providers import MODEL
        with tempfile.TemporaryDirectory() as d:
            ledger=Ledger(Path(d)/'db',{'model_calls':2,'device_steps':0})
            body={'model':MODEL,'usage':{'prompt_tokens':2,'completion_tokens':1,'total_tokens':3},'choices':[{'message':{'content':tool(action='terminate',status='success')}}]}
            with patch('budgetmas.providers.settings',return_value=('https://dashscope.aliyuncs.com/compatible-mode/v1','test-key')), patch('urllib.request.urlopen',return_value=BytesIO(json.dumps(body).encode())):
                result=GUIPlus(ledger).predict(PNG,'test')
                self.assertTrue(result['usage_complete'])
            body['model']='unexpected-model'
            with patch('budgetmas.providers.settings',return_value=('https://dashscope.aliyuncs.com/compatible-mode/v1','test-key')), patch('urllib.request.urlopen',return_value=BytesIO(json.dumps(body).encode())):
                with self.assertRaises(ValueError): GUIPlus(ledger).predict(PNG,'test')
            self.assertEqual(ledger.available()['model_calls'],0)

    def test_snapshot_termination_without_status_is_only_stop_request(self):
        action=parse_action(tool(action='terminate',coordinate=[870,926]))
        self.assertEqual(action,{'action':'terminate','status':'unknown'})
        with self.assertRaises(ValueError): parse_action(tool(action='terminate',status='hacked'))
