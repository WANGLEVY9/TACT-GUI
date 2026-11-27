"""Cross-platform environment contract and offline Web adaptation tests."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from budgetmas.environments import FixtureCheckpoint, PLATFORMS, validate_app, environment_for
from budgetmas.mechanism import ARMS, run, web_protocol
from budgetmas.offline import provider_factory
from budgetmas.providers import GUIPlus
from budgetmas.ui_nodes import matches, ui_features


def protocol():
    return json.loads((Path(__file__).resolve().parents[1] / 'configs' / 'web-mechanism-smoke.json').read_text())


def tool(**arguments):
    return '<tool_call>' + json.dumps({'name': 'computer_use', 'arguments': arguments}) + '</tool_call>'


class OfflineProvider:
    def __init__(self, ledger):
        self.ledger = ledger
        self.count = 0

    def predict(self, screen, context, *, max_tokens, advice):
        self.count += 1
        amount = {'model_calls': 1, 'device_steps': 0}
        self.ledger.reserve(str(self.count), amount)
        self.ledger.settle(str(self.count), amount)
        if advice:
            return {'text': 'Tap the registered control', 'usage_complete': True}
        return {'text': tool(action='left_click', coordinate=[100, 471]),
                'usage_complete': True, 'usage': {'total_tokens': 7}}


class Environments(unittest.TestCase):
    def test_platforms_are_explicit_and_apps_are_validated(self):
        self.assertEqual(PLATFORMS, ('android', 'web'))
        app = protocol()['apps']['web-calculator']
        self.assertEqual(validate_app(app, 'web')['group'], 'web-calculator')
        with self.assertRaisesRegex(ValueError, 'unsupported platform'):
            validate_app(app, 'ios')
        with self.assertRaisesRegex(ValueError, 'entry URL'):
            validate_app({'group': 'x'}, 'web')

    def test_shared_node_contract_uses_platform_neutral_fields(self):
        nodes = [{'text': 'Settings', 'role': 'button', 'name': 'Settings',
                  'resource-id': 'nav-settings', 'aria-selected': 'true',
                  'bounds': [0, 0, 100, 40], 'domain': 'web.app', 'clickable': 'true'}]
        self.assertTrue(matches(nodes, {'text': 'Settings', 'selected': True}))
        self.assertFalse(matches(nodes, {'text': 'Missing'}))
        self.assertEqual(ui_features(nodes, 'web.app')['nodes'], 1)
        with self.assertRaisesRegex(ValueError, 'unknown oracle selector'):
            matches(nodes, {'apk_resource_id': 'x'})

    def test_fixture_restores_state_and_replays_actions(self):
        app = protocol()['apps']['web-calculator']
        env = FixtureCheckpoint('fixture', app)
        env.launch()
        before = env.initial_hash()
        self.assertFalse(env.oracle({'text': 'equals'}))
        env.execute(tool(action='left_click', coordinate=[100, 471]), 1000, 700)
        self.assertTrue(env.oracle({'text': 'equals'}))
        env.launch()
        self.assertEqual(env.initial_hash(), before)
        self.assertFalse(env.oracle({'text': 'equals'}))

    def test_web_protocol_runs_paired_arms_without_adb_or_browser(self):
        config = protocol()
        with tempfile.TemporaryDirectory() as folder:
            result = run(config, web_protocol(config), 'fixture', Path(folder) / 'result',
                         provider_factory=OfflineProvider, platform='web')
        self.assertEqual(result['platform'], 'web')
        self.assertEqual(len(result['blocks']), 3)
        for block in result['blocks']:
            self.assertTrue(block['valid_pair'])
            self.assertEqual({arm['arm'] for arm in block['arms']}, set(ARMS))
            self.assertEqual({arm['success'] for arm in block['arms']}, {1})
            self.assertEqual(len({arm['initial_hash'] for arm in block['arms']}), 1)
        self.assertEqual(result['budget']['spent']['model_calls'], 18)
        self.assertEqual(result['budget']['spent']['device_steps'], 96)
        self.assertEqual(result['budget']['pending_reservations'], [])

    def test_offline_provider_settles_budget_without_credentials(self):
        from budgetmas.scopes import Scope
        from budgetmas.ledger import Ledger
        from pathlib import Path

        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(Path(folder)/'budget.sqlite', {'model_calls': 2, 'device_steps': 10})
            scope = Scope(ledger, {'model_calls': 2, 'device_steps': 10})
            provider = provider_factory(scope)
            first = provider.predict(b'\x89PNG\r\n\x1a\n', 'task')
            second = provider.predict(b'\x89PNG\r\n\x1a\n', 'review', advice=True)
            self.assertEqual(first['returned_model'], 'offline-web-fixture')
            self.assertIn('left_click', first['text'])
            self.assertNotIn('<tool_call>', second['text'])
            self.assertEqual(scope.audit()['spent']['model_calls'], 2)

    def test_web_worker_closes_one_app_before_opening_the_next(self):
        active = set()
        opened = []

        class WorkerEnvironment(FixtureCheckpoint):
            def __init__(self, target, app):
                if target in active:
                    raise AssertionError('worker already owns a browser session')
                super().__init__(target, app)
                self.target = target
                active.add(target)
                opened.append(app['group'])

            def close(self):
                active.remove(self.target)

        config = protocol()
        apps = web_protocol(config)
        with tempfile.TemporaryDirectory() as folder, patch(
                'budgetmas.mechanism.environment_for', side_effect=lambda platform, target, app:
                WorkerEnvironment(target, app)):
            report = run(config, apps, dict.fromkeys(apps, 'single-browser'), Path(folder)/'run',
                         provider_factory=provider_factory)
        self.assertEqual(opened, [case['app_group'] for case in config['checkpoints']])
        self.assertEqual(active, set())
        self.assertTrue(all(block['valid_pair'] for block in report['blocks']))

    def test_android_protocol_rejects_web_backend_and_vice_versa(self):
        apps = web_protocol(protocol())
        android_case = {'id': 'case', 'app_group': 'web-calculator', 'prefix': [],
                        'start': {'text': '2+3'}, 'oracle': {'text': 'equals'}}
        with self.assertRaisesRegex(ValueError, 'application manifest'):
            run({'schema': 1, 'seed': 1, 'checkpoints': [android_case]}, apps, 'fixture',
                Path('/tmp/unused'), platform='android')
        with self.assertRaisesRegex(ValueError, 'device map'):
            run(protocol(), apps, None, Path('/tmp/unused'), platform='web')
