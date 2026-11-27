"""Deterministic zero-credential provider for Web adaptation fixtures only.

It is not a model and must never be used for research comparisons. Every call
still passes through the shared ledger so budget admission and settlement are
exercised exactly as they are for a live provider.
"""
import json
import uuid


def tool(**arguments):
    return '<tool_call>' + json.dumps({'name': 'computer_use', 'arguments': arguments}) + '</tool_call>'


class OfflineProvider:
    def __init__(self, scope, tap=(100, 471)):
        self.scope = scope
        self.tap = list(tap)

    def predict(self, screen, context, *, max_tokens=512, advice=False):
        rid = uuid.uuid4().hex
        amount = dict.fromkeys(self.scope.available(), 0)
        amount['model_calls'] = 1
        self.scope.reserve(rid, amount)
        self.scope.settle(rid, amount)
        text = ('Tap the registered control' if advice else
                tool(action='left_click', coordinate=self.tap))
        return {'text': text, 'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0},
                'usage_complete': True, 'returned_model': 'offline-web-fixture'}


def provider_factory(scope):
    return OfflineProvider(scope)
