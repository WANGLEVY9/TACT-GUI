import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from budgetmas.intent import Step, compile_intent, ground, semantic_candidate
from budgetmas.ledger import Ledger
from budgetmas.mechanism import run, web_protocol
from budgetmas.providers import GUIPlus, TEXT_FLASH


def button(text, box, **fields):
    return {'text':text, 'role':'button', 'clickable':'true', 'bounds':box, **fields}


class IntentTests(unittest.TestCase):
    def test_order_and_negative_constraints_are_retained(self):
        plan = compile_intent('点击“C”，然后点击“8”。不要点击“=”。')
        self.assertEqual(plan.steps, (Step('C'), Step('8')))
        self.assertEqual(plan.forbidden, ('=',))
        self.assertEqual(compile_intent('Click button ONE, then click button TWO.').steps,
                         (Step('ONE','button'), Step('TWO','button')))
        for uncertain in ('Click "A" if there is a balance.', 'Click "A", then type sensitive text.',
                          'Click "A". Do not click "a".', 'Do not click "A".'):
            with self.subTest(uncertain=uncertain):
                self.assertIsNone(compile_intent(uncertain))
        self.assertEqual(compile_intent('Click "A; next."').steps, (Step('A; next.'),))

    def test_grounding_handles_unavailable_and_ambiguous_targets(self):
        one=button('Continue',[10,20,30,40])
        two=button('Continue',[30,40,50,60])
        step=Step('Continue','button')
        self.assertEqual(ground(step,(),[one],100,100),((20,30),'unique_grounded_target'))
        self.assertEqual(ground(step,(),[one,two],100,100),(None,'ambiguous_target'))
        overlay=button('Remove',[0,0,30,50])
        self.assertEqual(ground(step,(),[one,overlay],100,100),(None,'overlapping_clickable_controls'))
        for unusable in (dict(one, enabled='false'), dict(one,center_hit=False),
                         dict(one, **{'aria-label':'Remove'}), dict(one,role='link')):
            self.assertEqual(ground(step,('Remove',),[unusable],100,100),(None,'missing_target'))

    def test_semantic_response_requires_cited_user_evidence(self):
        text='Proceed if the balance is unchanged.'
        candidate={'steps':[{'meaning':'Wait for a known balance','evidence':'balance is unchanged'}],
                   'constraints':[],'question':'Where can I check the balance?'}
        self.assertEqual(semantic_candidate(text,json.dumps(candidate)),candidate)
        candidate['steps'][0]['evidence']='The order has been paid'
        with self.assertRaises(ValueError):
            semantic_candidate(text,json.dumps(candidate))

    def test_semantic_candidate_is_metered_and_cannot_apply_a_tap(self):
        config=json.loads(Path('configs/web-mechanism-smoke.json').read_text())
        case=config['checkpoints'][0]
        case['goal']='Finish the current calculation'
        config.update(checkpoints=[case], max_model_calls=1, variants={
            'intent_gate':{'mode':'intent','intent_model':TEXT_FLASH,'max_tokens':256}})
        reply={'model':TEXT_FLASH,'usage':{'prompt_tokens':100,'completion_tokens':20,'total_tokens':120},
               'choices':[{'finish_reason':'stop','message':{'content':json.dumps({
                   'steps':[{'meaning':'Look for equals','evidence':'Finish the current calculation'}],
                   'constraints':[],'question':None})}}]}
        with TemporaryDirectory() as temp, patch('budgetmas.providers.settings',return_value=(
                'https://dashscope.aliyuncs.com/compatible-mode/v1','fake-key')), patch(
                'budgetmas.providers.urllib.request.urlopen',return_value=io.BytesIO(json.dumps(reply).encode())) as post:
            report=run(config,web_protocol(config),'fixture',Path(temp)/'run')
        arm=report['blocks'][0]['arms'][0]
        self.assertEqual(arm['actions'],[])
        self.assertEqual(arm['intent']['decision'],'clarify')
        self.assertEqual(arm['calls'][0]['role'],'interpret')
        self.assertIsNotNone(arm['intent']['candidate'])
        self.assertEqual(report['budget']['spent']['model_calls'],1)
        request=json.loads(post.call_args.args[0].data)
        self.assertEqual(request['response_format'],{'type':'json_object'})
        self.assertEqual(request['messages'][-1]['content'],case['goal'])
