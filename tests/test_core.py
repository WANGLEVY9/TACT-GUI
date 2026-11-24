import concurrent.futures
import tempfile
import unittest
from pathlib import Path
from budgetmas.ledger import Ledger, BudgetExceeded
from budgetmas.data import split_manifest
from budgetmas.scheduling import Candidate,LinearValue,Scheduler,train
from budgetmas.android import Android
from unittest.mock import patch

class Core(unittest.TestCase):
    def test_atomic_concurrent_admission(self):
        with tempfile.TemporaryDirectory() as d:
            ledger=Ledger(Path(d)/'ledger.db',{'calls':3})
            def reserve(i):
                try: ledger.reserve(str(i),{'calls':1}); return True
                except BudgetExceeded: return False
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                self.assertEqual(sum(pool.map(reserve,range(20))),3)
            self.assertEqual(ledger.available(),{'calls':0})
    def test_settlement_and_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'ledger.db'; ledger=Ledger(path,{'tokens':10})
            ledger.reserve('a',{'tokens':8})
            self.assertEqual(Ledger(path).available()['tokens'],2)
            with self.assertRaises(BudgetExceeded): ledger.settle('a',{'tokens':9})
            ledger.settle('a',{'tokens':3})
            self.assertEqual(ledger.available()['tokens'],7)
            with self.assertRaises(ValueError): ledger.settle('a',{'tokens':3})
    def test_split_isolation(self):
        rows=[{'id':str(i),'app_group_id':str(i//2)} for i in range(20)]
        result=split_manifest(rows)
        for g in range(10): self.assertEqual(len({r['split'] for r in result['rows'] if r['app_group_id']==str(g)}),1)
        self.assertEqual(result,split_manifest(rows))
        rows[0]['apk_sha256']='same';rows[3]['apk_sha256']='same'
        with self.assertRaises(ValueError): split_manifest(rows)
    def test_training_guard_and_scheduler(self):
        with self.assertRaises(ValueError): train([{'split':'test'}],2)
        m=LinearValue(2);m.update((1,1),1)
        scheduler=Scheduler(m)
        c=Candidate('x',(1,1),{'calls':2},('login',))
        self.assertIsNone(scheduler.choose([c],{'calls':1},['login']))
        self.assertIsNone(scheduler.choose([c],{'calls':2}))
        self.assertEqual(scheduler.choose([c],{'calls':2},['login']),c)
    def test_android_rejects_shell_action(self):
        device=Android('emulator-5554')
        with patch.object(device,'adb') as adb:
            with self.assertRaises(ValueError): device.execute('<tool_call>{"name":"computer_use","arguments":{"action":"shell","command":"id"}}</tool_call>',100,100)
            adb.assert_not_called()
    def test_nonfinite_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError): Ledger(Path(d)/'db',{'calls':float('nan')})

if __name__=='__main__': unittest.main()
