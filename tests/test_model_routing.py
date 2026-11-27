import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from budgetmas.ledger import Ledger
from budgetmas.mechanism import run, web_protocol
from budgetmas.offline import OfflineProvider
from budgetmas.providers import GUIPlus, MODEL, TEXT_FLASH, VISION_FLASH, list_cost


class TestModelRouting(unittest.TestCase):
    def test_semantic_request_uses_text_and_bills_pinned_model(self):
        usage = {'prompt_tokens':100,'completion_tokens':10,'total_tokens':110}
        raw = {'model':TEXT_FLASH,'usage':usage,'choices':[{'message':{'content':'Tap Continue'},
                                                         'finish_reason':'stop'}]}
        with TemporaryDirectory() as tmp, patch('budgetmas.providers.settings',return_value=(
                'https://dashscope.aliyuncs.com/compatible-mode/v1','fake-credential')), patch(
                'budgetmas.providers.urllib.request.urlopen',
                return_value=io.BytesIO(json.dumps(raw).encode())) as post:
            ledger = Ledger(Path(tmp)/'budget.sqlite',{'model_calls':1})
            provider = GUIPlus(ledger,model=TEXT_FLASH)
            with self.assertRaisesRegex(ValueError,'semantic review'):
                provider.predict(None,'visible task')
            result = provider.predict(None,'visible task',max_tokens=128,advice=True)
            self.assertEqual(ledger.audit()['spent']['model_calls'],1)
            self.assertEqual(result['estimated_list_cny'],.00003)
        payload = json.loads(post.call_args.args[0].data)
        self.assertEqual(payload['model'],TEXT_FLASH)
        self.assertEqual(payload['max_tokens'],128)
        self.assertEqual(payload['messages'][-1]['content'],'visible task')
        self.assertIs(payload['enable_thinking'],False)

    def test_beijing_list_price_changes_above_first_tier(self):
        def usage(n):
            return {'prompt_tokens':n,'completion_tokens':100,'total_tokens':n+100}
        self.assertEqual(list_cost(VISION_FLASH,usage(32000)),.00648)
        self.assertEqual(list_cost(VISION_FLASH,usage(32001)),.0194406)
        self.assertIsNone(list_cost(VISION_FLASH,{'total_tokens':300}))

    def test_variant_routes_review_and_does_not_apply_truncated_action(self):
        config = json.loads(Path('configs/web-mechanism-smoke.json').read_text())
        config['checkpoints'] = config['checkpoints'][:1]
        config.update(variants={
            'capped': {'mode':'direct','execute_model':MODEL,'max_tokens':128},
            'mixed': {'mode':'independent_review','execute_model':VISION_FLASH,'review_model':TEXT_FLASH,
                      'review_view':'text','max_tokens':512},
        }, max_model_calls=4)
        decisions = []

        class Fake(OfflineProvider):
            def __init__(self, scope, *, model):
                super().__init__(scope)
                self.model = model

            def predict(self, screen, context, *, max_tokens, advice):
                decisions.append((self.model,screen is None,max_tokens,advice))
                response = super().predict(screen,context,max_tokens=max_tokens,advice=advice)
                return {**response, 'finish_reason':'length' if self.model==MODEL else 'stop'}

        with TemporaryDirectory() as tmp:
            out = Path(tmp)/'run'
            result = run(config,web_protocol(config),'fixture',out,provider_factory=Fake)
        by_arm = {a['arm']:a for a in result['blocks'][0]['arms']}
        self.assertEqual(by_arm['capped']['actions'],[])
        self.assertEqual(by_arm['capped']['success'],0)
        self.assertEqual(by_arm['mixed']['success'],1)
        self.assertCountEqual(decisions,[(MODEL,False,128,False)]*2+
                              [(TEXT_FLASH,True,512,True),(VISION_FLASH,False,512,False)])
