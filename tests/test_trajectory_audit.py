from pathlib import Path
import tempfile
import unittest
from xml.etree.ElementTree import fromstring

from budgetmas.ledger import Ledger
from budgetmas.pilot_device import state_hash
from budgetmas.trajectory_audit import audit


class TrajectoryAudit(unittest.TestCase):
    def test_first_hit_lost_is_verified_without_provider_calls(self):
        class Device:
            def __init__(self):
                self.android = self
                self.index = 0

            def launch(self,app):
                self.index = 0

            def nodes(self):
                labels = ('start','goal','overshot')
                return list(fromstring('<hierarchy><node package="test.app" text="' + labels[self.index] +
                                       '" bounds="[0,0][100,100]"/></hierarchy>').iter('node'))

            def execute(self,raw,width,height):
                self.index += 1

        device=Device()
        case={'id':'case','prefix':[],'start':{'text':'start'},'oracle':{'text':'goal'}}
        app={'package':'test.app'}
        arm={'actions':[{'action':'left_click','coordinate':[10,10]}]*2,
             'initial_hash':state_hash(device.nodes(),app['package'])}
        with tempfile.TemporaryDirectory() as d:
            ledger=Ledger(Path(d)/'db',{'device_steps':12})
            result=audit(device,app,case,arm,(100,100),ledger)
            self.assertTrue(result['verified'])
            self.assertTrue(result['early_goal_lost'])
            self.assertEqual(ledger.audit()['spent']['device_steps'],12)
