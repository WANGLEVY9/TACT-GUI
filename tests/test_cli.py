"""Subprocess checks exercise installed-module entry points and JSON artifacts."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from budgetmas.models import JointModel
from budgetmas.calibration import calibrate_by_application
from budgetmas.decision import Action,BudgetPlanner


class CLI(unittest.TestCase):
    def test_training_checkpoint_and_team_commands(self):
        with tempfile.TemporaryDirectory() as folder:
            d=Path(folder)
            def run(*args):
                return subprocess.run([sys.executable,'-m','budgetmas.cli',*map(str,args)],check=True,capture_output=True,text=True,env={**os.environ,'PYTHONPATH':str(Path('src').resolve())})
            row={'id':'train-1','app_group_id':'fixture-train','split':'train','features':[1,1], 'verification_status':'confirmed','verified_reward':1,'actual':{'operations':4}}
            val=dict(row,id='val-1',app_group_id='fixture-val',split='validation')
            (d/'train.jsonl').write_text(json.dumps(row)+'\n'); (d/'val.jsonl').write_text(json.dumps(val)+'\n')
            result=run('fit-joint',d/'train.jsonl',d/'val.jsonl',d/'model.json','--dimension','2','--resources','operations')
            self.assertEqual(json.loads(result.stdout)['validation_rows'],1)
            model=JointModel.load(d/'model.json')
            self.assertEqual(model.metadata['train_groups'],['fixture-train'])
            result=run('team-fixture','--out',d/'team')
            self.assertEqual([r['reward'] for r in json.loads(result.stdout)['results']],[1,0])
            rows=[{'id':str(i),'app_group_id':str(i)} for i in range(10)]
            (d/'manifest.json').write_text(json.dumps(rows))
            run('split',d/'manifest.json',d/'splits.json')
            self.assertEqual(len(json.loads((d/'splits.json').read_text())['rows']),10)

    def test_calibration_separation_and_abstention(self):
        model=JointModel(1,['operations'])
        model.metadata={'train_groups':['train'],'validation_groups':['tuning']}
        row={'id':'cal-1','app_group_id':'cal','split':'calibration','features':[1], 'verification_status':'confirmed','verified_reward':1,'actual':{'operations':4}}
        calibrated,report=calibrate_by_application(model,[row],.1)
        self.assertFalse(report['informative'])
        action=Action('a','t','single',(1,),{'operations':4})
        self.assertEqual(BudgetPlanner(calibrated).plan([action],{'operations':4})['actions'],[])
        for group in ('train','tuning'):
            with self.assertRaises(ValueError): calibrate_by_application(model,[dict(row,app_group_id=group)])
