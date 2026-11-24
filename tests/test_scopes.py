import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from budgetmas.ledger import Ledger,BudgetExceeded
from budgetmas.scopes import Scope
from budgetmas.runtime import Journal,TeamExecutor
from budgetmas.gui_team import GUIEnvironment,InstructionProposer,PolicyCritic,CrashReplayVerifier
from budgetmas.decision import Action,FixedOrderPlanner
from budgetmas.campaign import Campaign
from test_gui import Device,Provider,LOG
from budgetmas.gui_session import crash_signature


class Scopes(unittest.TestCase):
    def test_nested_no_double_charging(self):
        with tempfile.TemporaryDirectory() as d:
            ledger=Ledger(Path(d)/'db',{'calls':10})
            trial=Scope(ledger,{'calls':8}); role=Scope(trial,{'calls':5})
            role.reserve('api',{'calls':4}); role.settle('api',{'calls':2})
            self.assertEqual(ledger.audit()['reserved'],{'calls':8})
            role.close(); trial.close()
            self.assertEqual(ledger.audit()['spent'],{'calls':2})
            self.assertEqual(ledger.available(),{'calls':8})
            with self.assertRaises(ValueError): role.reserve('another',{'calls':1})

    def test_pending_prevents_scope_close(self):
        with tempfile.TemporaryDirectory() as d:
            ledger=Ledger(Path(d)/'db',{'calls':10})
            trial=Scope(ledger,{'calls':5}); role=Scope(trial,{'calls':3})
            role.reserve('timeout',{'calls':1})
            with self.assertRaises(ValueError): role.close()
            with self.assertRaises(ValueError): trial.close()
            self.assertEqual(ledger.available(),{'calls':5})
            self.assertEqual(Ledger(Path(d)/'db').audit()['reserved'],{'calls':5})

    def test_over_quota_and_misreported_actual(self):
        with tempfile.TemporaryDirectory() as d:
            ledger=Ledger(Path(d)/'db',{'calls':5}); role=Scope(ledger,{'calls':2})
            with self.assertRaises(BudgetExceeded): role.reserve('big',{'calls':3})
            role.reserve('ok',{'calls':1}); role.settle('ok',{'calls':1})
            with self.assertRaises(ValueError): role.close({'calls':0})
            role.close()
            self.assertEqual(ledger.available()['calls'],4)

    def test_real_adapter_contract_shared_accounting(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); journal=Journal(root/'events')
            ledger=Ledger(root/'db',{'model_calls':3,'device_steps':20})
            device=Device()
            environment=GUIEnvironment(device,'org.test.app','.Main',root/'exec',journal,max_steps=3,provider_factory=Provider)
            verifier=CrashReplayVerifier(device,'org.test.app','.Main',root/'verify',known_signatures={crash_signature(LOG,'org.test.app'):'curated-bug-1'})
            executor=TeamExecutor(ledger,journal,environment,InstructionProposer('test cart'),PolicyCritic(),verifier)
            q={'proposer':{'model_calls':0,'device_steps':0},'critic':{'model_calls':0,'device_steps':0},'execute':{'model_calls':3,'device_steps':10},'verify':{'model_calls':0,'device_steps':10}}
            action=Action('a','task','gui',(1,),{'model_calls':3,'device_steps':20})
            report=Campaign(FixedOrderPlanner(),executor).run([action],{'a':q},initial_states={'task':'clean'},split='test',seed=0,max_trials=1)
            self.assertEqual(report['results'][0]['reward'],1)
            self.assertEqual(ledger.audit()['spent'],{'model_calls':1,'device_steps':10})
            self.assertEqual(ledger.audit()['pending_reservations'],[])
            self.assertEqual(device.resets,3)

    def test_unknown_crash_not_rewarded(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); journal=Journal(root/'events')
            ledger=Ledger(root/'db',{'model_calls':1,'device_steps':12})
            device=Device()
            env=GUIEnvironment(device,'org.test.app','.Main',root/'exec',journal,max_steps=1,provider_factory=Provider)
            verifier=CrashReplayVerifier(device,'org.test.app','.Main',root/'verify')
            executor=TeamExecutor(ledger,journal,env,InstructionProposer('test'),None,verifier)
            q={'proposer':{'model_calls':0,'device_steps':0},'execute':{'model_calls':1,'device_steps':4},'verify':{'model_calls':0,'device_steps':6}}
            result=executor.run(Action('a','task','gui',(1,),{'model_calls':1,'device_steps':10}),q,initial_state='clean',seed=0,split='test')
            self.assertEqual(result['status'],'reproduced_crash')
            self.assertIsNone(result['reward'])

    def test_independent_task_oracle_rejects_model_claim(self):
        from budgetmas.runtime import RoleResult
        with tempfile.TemporaryDirectory() as d:
            ledger=Ledger(Path(d)/'db',{'model_calls':0,'device_steps':10})
            scope=Scope(ledger,{'model_calls':0,'device_steps':10})
            from budgetmas.scopes import Quota
            quota=Quota({'model_calls':0,'device_steps':10},scope)
            verifier=CrashReplayVerifier(Device(log=''),'org.test.app','.Main',Path(d)/'verify',outcome_probe=lambda:False)
            result=verifier.verify('clean',0,{}, {'actions':[],'crash_signature':None},quota)
            self.assertEqual(result.value['status'],'task_incomplete')
            self.assertEqual(result.actual['device_steps'],7) # primary probe + 2*(reset/log/probe)
            scope.close(result.actual)
            self.assertEqual(ledger.available()['device_steps'],3)

    def test_live_entrypoint_uses_real_adapter_contract(self):
        from budgetmas.live import run_gui_team
        from budgetmas.gui_team import GUIEnvironment as RealEnvironment
        with tempfile.TemporaryDirectory() as d:
            def environment(*args,**kwargs):
                return RealEnvironment(*args,**kwargs,provider_factory=Provider)
            with patch('budgetmas.live.Android',return_value=Device(log='')),patch('budgetmas.live.GUIEnvironment',side_effect=environment):
                report=run_gui_team('emulator-test','org.test.app','.Main','calculate',Path(d)/'out',max_steps=1)
            self.assertEqual(report['execution_status'],'completed')
            self.assertEqual(report['budget']['spent'],{'model_calls':1,'device_steps':10})
            self.assertEqual(report['budget']['pending_reservations'],[])
