import json
from pathlib import Path
import signal
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from budgetmas import console_data
from budgetmas.console import Workspace
from budgetmas.ledger import Ledger
from budgetmas.runtime import Journal


CHECKPOINT = {
    'id': 'calculator-equals', 'app_group': 'calculator', 'prefix': ['2', '+', '3'],
    'goal': 'Press equals', 'start': {'text': '2+3'}, 'oracle': {'text': '5'},
}
ACTION = {'action': 'left_click', 'coordinate': [800, 900]}
RAW = '<tool_call>' + json.dumps({'name': 'computer_use', 'arguments': ACTION}) + '</tool_call>'


class ConsoleTests(TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'configs').mkdir()
        self.workspace = Workspace(self.root, self.root / 'manifest.json')
        self.results = self.workspace.results
        self.recording = self.results / 'round8' / 'live'
        self.arm = self.recording / CHECKPOINT['id'] / 'direct'
        self.arm.mkdir(parents=True)
        self.protocol = {'schema': 1, 'seed': 3, 'checkpoints': [CHECKPOINT]}
        self.write_json(self.recording / 'protocol.json', self.protocol)
        self.write_json(self.root / 'configs' / 'android-mechanism-smoke.json', self.protocol)
        self.payload = {
            'profile': 'smoke', 'cases': [CHECKPOINT['id']], 'seed': 4,
            'timeout': 20, 'call_limit': 6, 'serial': 'emulator-5582',
        }

    def write_json(self, path, value):
        path.write_text(json.dumps(value))

    def test_plan_enforces_registered_tasks_and_call_limit(self):
        plan = self.workspace.plan(self.payload)
        self.assertEqual(plan['totals'], {'model_calls': 6, 'device_steps': 72})
        for change, error in [
            ({'call_limit': 5}, 'above your limit'), ({'guard': True}, 'above your limit'),
            ({'cases': ['other']}, 'unregistered'), ({'slots': 2.0}, 'registered horizon'),
            ({'serial': 'unregistered-phone'}, 'emulator serial'), ({'progress': 'true'}, 'boolean'),
        ]:
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, error):
                self.workspace.plan(self.payload | change)

    def test_crossover_preserves_paired_horizons(self):
        pair = [CHECKPOINT | {'id': f'calculator-equals-h{slots}', 'slots': slots,
                              'base_checkpoint': CHECKPOINT['id']} for slots in (2, 3)]
        self.write_json(self.root / 'configs' / 'android-stop-crossover.json',
                        self.protocol | {'checkpoints': pair})
        payload = self.payload | {'profile': 'crossover', 'call_limit': 20, 'guard': True}
        with self.assertRaisesRegex(ValueError, 'both conditions'):
            self.workspace.plan(payload | {'cases': [pair[0]['id']]})
        payload['cases'] = [case['id'] for case in pair]
        with self.assertRaisesRegex(ValueError, 'preserve'):
            self.workspace.plan(payload | {'slots': 3})
        self.assertEqual(self.workspace.plan(payload)['totals']['model_calls'], 20)

    def test_archived_actions_use_independent_replay_status(self):
        for i in range(2):
            (self.arm / f'screen-{i}.png').touch()
            (self.arm / f'model-{i}.txt').write_text(RAW if i == 0 else RAW.replace('900', '500'))
        self.write_json(self.arm / 'report.json', {
            'actions': [ACTION], 'success': 1, 'verification': {'observed': True, 'replay': True},
            'progress': {'stable': True, 'observed_after_actions': [True]}, 'calls': [],
        })
        trace = console_data.trace(self.results, 'round8/live', CHECKPOINT['id'], 'direct')
        first, second = trace['frames']
        self.assertTrue(first['executed'])
        self.assertTrue(first['goal_after'])
        self.assertFalse(second['executed'])
        self.assertIsNone(second['goal_after'])
        self.assertEqual(trace['success'], 1)
        self.assertEqual(console_data.archive_index(self.results)[0]['id'], 'round8/live')

    def test_partial_journal_exposes_live_progress_and_held_budget(self):
        (self.arm / 'screen-0.png').touch()
        (self.arm / 'model-0.txt').write_text(RAW)
        journal = Journal(self.arm / 'events.jsonl')
        journal.append('model_response', index=0, tokens=21)
        journal.append('action_applied', index=0, action=ACTION, action_index=1)
        journal.append('progress_measured', index=0, goal_after=True)
        with journal.path.open('a') as stream:
            stream.write('{"event":')
        ledger = Ledger(self.recording / 'budget.sqlite', {'model_calls': 6})
        ledger.reserve('settled', {'model_calls': 2})
        ledger.settle('settled', {'model_calls': 1})
        ledger.reserve('held', {'model_calls': 2})
        trace = console_data.trace(self.results, 'round8/live', CHECKPOINT['id'], 'direct')
        frame = trace['frames'][0]
        self.assertTrue(frame['executed'])
        self.assertTrue(frame['live_goal'])
        self.assertIsNone(frame['goal_after'])
        self.assertIsNone(trace['success'])
        detail = console_data.run_detail(self.results, 'round8/live', active=True)
        self.assertEqual(detail['activity']['tokens'], 21)
        self.assertEqual(detail['counts']['running'], 1)
        self.assertEqual(detail['budget'], ledger.audit())
        self.assertEqual(console_data.run_detail(self.results, 'round8/live')['counts']['interrupted'], 1)

    def test_unregistered_artifact_paths_are_rejected(self):
        outside = self.root / 'outside.png'
        outside.touch()
        (self.arm / 'screen-0.png').symlink_to(outside)
        with self.assertRaises(ValueError):
            console_data.image_path(self.results, 'round8/live', CHECKPOINT['id'], 'direct', '0')
        with self.assertRaises(ValueError):
            console_data.run_directory(self.results, '../outside')
        with self.assertRaises(FileNotFoundError):
            console_data.image_path(self.results, 'round8/live', CHECKPOINT['id'], 'direct', '2')

    def test_start_and_stop_enforce_confirmation_and_dedicated_device(self):
        ws = self.workspace
        self.write_json(ws.manifest, {})
        with self.assertRaisesRegex(ValueError, 'Confirm'):
            ws.start(self.payload)
        payload = self.payload | {'confirmed': True}
        proc = Mock(pid=100)
        proc.poll.return_value = None
        with patch('budgetmas.console.validate_manifest'), patch('budgetmas.console.Android') as android, \
                patch('budgetmas.console.subprocess.Popen', return_value=proc) as launch, \
                patch('budgetmas.console.os.killpg') as stop:
            android.return_value.adb.return_value = b'PersonalAVD\r\nOK'
            with self.assertRaisesRegex(ValueError, 'dedicated BudgetMASPilot'):
                ws.start(payload)
            launch.assert_not_called()
            android.return_value.adb.return_value = b'BudgetMASPilot\r\nOK'
            result = ws.start(payload)
            self.assertTrue(ws.is_active(result['run']))
            launch_args = launch.call_args.args[0]
            self.assertIn('budgetmas.mechanism', launch_args)
            self.assertIn('--serial', launch_args)
            self.assertTrue(launch.call_args.kwargs['start_new_session'])
            with self.assertRaisesRegex(ValueError, 'active'):
                ws.start(payload)
            self.assertEqual(ws.stop(result['job'])['state'], 'stopping')
            stop.assert_called_once_with(100, signal.SIGTERM)
            proc.poll.return_value = -signal.SIGTERM
            self.assertFalse(ws.is_active(result['run']))
            self.assertEqual(ws.snapshot()['jobs'][0]['exit'], -signal.SIGTERM)

    def test_web_fixture_profile_runs_without_emulator_or_manifest(self):
        from budgetmas import console
        protocol = json.loads((Path(__file__).resolve().parents[1] / 'configs' /
                               'web-mechanism-smoke.json').read_text())
        self.write_json(self.root / 'configs' / 'web-mechanism-smoke.json', protocol)
        ids = [case['id'] for case in protocol['checkpoints']]
        payload = {'profile': 'web-smoke', 'cases': ids, 'seed': 5, 'timeout': 20,
                   'call_limit': 18, 'serial': 'fixture', 'confirmed': True}
        result = self.workspace.start(payload)
        self.assertFalse(self.workspace.is_active(result['run']))
        report = json.loads((self.results / result['run'] / 'report.json').read_text())
        self.assertEqual(report['platform'], 'web')
        self.assertEqual(len(report['blocks']), 3)
        self.assertTrue(all(block['valid_pair'] for block in report['blocks']))
        self.assertNotIn('gui-plus', str(report).lower())
