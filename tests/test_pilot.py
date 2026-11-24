from pathlib import Path
import tempfile
import unittest
from xml.etree.ElementTree import fromstring

from budgetmas.data import split_manifest
from budgetmas.pilot import action_for, digest, run_trial, validate_manifest
from budgetmas.pilot_device import FEATURE_NAMES
from budgetmas.pilot_model import splits, group_metric
from budgetmas.ledger import Ledger


class MockDevice:
    def launch(self, app):
        self.current = self.initial
    def nodes(self):
        return list(fromstring(self.current).iter('node'))
    def tap(self, location):
        self.current = self.result

    initial = '<hierarchy><node package="test.demo" resource-id="open" text="Settings" enabled="true" clickable="true" bounds="[0,0][20,20]"/></hierarchy>'
    result = '<hierarchy><node package="test.demo" resource-id="title" text="Settings page" bounds="[0,0][20,20]"/></hierarchy>'


class Pilot(unittest.TestCase):
    def test_real_contract_collaboration_charged_and_replayed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            row = {'id':'sample/settings','app_group_id':'sample','split':'train',
                   'task':{'steps':[['Preferences','Settings']],
                           'oracle':{'resource':'title','text':'Settings page'}}}
            app = {'package':'test.demo'}
            device = MockDevice()
            for mode, success, calls in [('single',0,1),('collaborative',1,2),('single_alias',1,1)]:
                action = action_for(row,mode)
                ledger = Ledger(root/(mode+'.db'),action.upper)
                result = run_trial(device,row,app,action,ledger,root/mode,0)
                self.assertEqual(result['success'],success)
                self.assertEqual(result['actual']['agent_calls'],calls)
                self.assertTrue(result['verification']['initial_matches'])
                self.assertFalse(result['ledger']['pending_reservations'])

    def test_preexisting_success_is_unresolved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            row = {'id':'sample/settings','app_group_id':'sample','split':'train',
                  'task':{'steps':[['Missing']], 'oracle':{'resource':'open','text':'Settings'}}}
            app = {'package':'test.demo'}
            action = action_for(row,'single')
            result = run_trial(MockDevice(),row,app,action,Ledger(root/'db',action.upper),root/'run',0)
            self.assertIsNone(result['success'])
            self.assertTrue(result['verification']['initial_positive'])

    def test_groups_never_cross_folds_even_with_unequal_sizes(self):
        records = [{'app_group_id':'a'}, {'app_group_id':'a'}, {'app_group_id':'b'}]
        self.assertEqual(group_metric(records,[0,0,1]),.5)
        for train, held in splits(records):
            self.assertFalse({r['app_group_id'] for r in train} & {r['app_group_id'] for r in held})

    def test_encoding_has_no_oracle_or_app_identity(self):
        row = {'id':'app1/task','task':{'steps':[['Open','Start']], 'oracle':{'text':'expected'}}}
        alternate = {'id':'app2/different','task':{'steps':[['Open','Start']], 'oracle':{'text':'other'}}}
        self.assertEqual(action_for(row,'single').features,action_for(alternate,'single').features)
        self.assertEqual(len(action_for(row,'single').features),len(FEATURE_NAMES))

    def test_manifest_rejects_a_rehashed_split_mutation(self):
        split = split_manifest([{'id':str(i),'app_group_id':str(i)} for i in range(5)])
        contents = {'config':{'seed':split['seed']},'fingerprint':'test-image','split':split}
        validate_manifest({**contents,'sha256':digest(contents)})
        row = next(row for row in split['rows'] if row['split']=='train')
        row['split'] = 'test'
        with self.assertRaisesRegex(ValueError,'partition mismatch'):
            validate_manifest({**contents,'sha256':digest(contents)})
