"""Real-device GUI loop with explicit reset, replay headroom and crash evidence.

This runner measures logical device operations, not individual ADB commands.
It does not install apps, change snapshots, or infer business-logic correctness.
"""
import hashlib
import json
import math
import re
import time
import uuid
from pathlib import Path
from .android import png_dimensions, parse_action
from .ledger import BudgetExceeded


def crash_signature(log, package):
    """Java fatal exceptions attributed to the configured process only."""
    blocks = re.split(r'(?=FATAL EXCEPTION:)', log)
    for block in blocks:
        process = re.search(r'Process:\s*([^,\s]+)',block)
        if 'FATAL EXCEPTION:' not in block or not process or process.group(1) != package:
            continue
        exception = re.search(r'\b((?:[\w$]+\.)+[\w$]*(?:Exception|Error))(?::|\s|$)',block)
        frames = re.findall(r'\bat\s+([\w.$]+)\([^\n]*\)',block)
        if exception and frames:
            identity = exception.group(1)+'|'+'|'.join(frames[:5])
            return hashlib.sha256(identity.encode()).hexdigest()
    return None


class GUISession:
    def __init__(self, device, provider, ledger, journal):
        self.device, self.provider, self.ledger, self.journal = device,provider,ledger,journal

    def run(self, package, activity, instruction, output, *, max_steps=8, replays=2, seconds=120):
        if type(max_steps) is not int or max_steps < 1 or type(replays) is not int or replays < 0 or not math.isfinite(seconds) or seconds <= 0:
            raise ValueError('invalid session limits')
        if set(self.ledger.available()) != {'model_calls','device_steps'}:
            raise ValueError('GUI session requires model_calls/device_steps budgets')
        out = Path(output); out.mkdir(parents=True,exist_ok=False)
        started = time.monotonic()
        actions = []; calls = []
        replay_id = uuid.uuid4().hex
        # Reserve worst-case replay before any discovery. One reset + log per replay.
        replay_quota = {'model_calls':0,'device_steps':replays*(max_steps+2)}
        self.ledger.reserve(replay_id,replay_quota)
        replay_used = 0
        status = 'unresolved'
        signature = None
        error_type = None
        self.journal.append('gui_session_started',package=package,max_steps=max_steps,replays=replays)

        def check_time():
            if time.monotonic()-started >= seconds:
                raise TimeoutError('session deadline reached')

        def operation(callback):
            check_time()
            rid = uuid.uuid4().hex
            amount = {'model_calls':0,'device_steps':1}
            self.ledger.reserve(rid,amount)
            result = callback()  # Exceptions keep this operation reserved.
            self.ledger.settle(rid,amount)
            return result

        try:
            operation(lambda:self.device.reset(package,activity))
            for step in range(max_steps):
                png = operation(self.device.screenshot)
                width,height = png_dimensions(png)
                (out/f'screen-{step:03d}.png').write_bytes(png)
                check_time()
                # No test oracle or crash signature supplied to the execution model.
                response = self.provider.predict(png,instruction+'\nExecuted actions: '+json.dumps([a['action'] for a in actions]))
                calls.append({k:v for k,v in response.items() if k != 'text'})
                (out/f'model-{step:03d}.txt').write_text(response['text'])
                action = parse_action(response['text'])
                if action['action'] == 'terminate':
                    status = 'terminated_without_crash'
                    break
                operation(lambda:self.device.execute(response['text'],width,height))
                actions.append({'action':action,'width':width,'height':height})
                log = operation(self.device.crash_log)
                signature = crash_signature(log,package)
                self.journal.append('gui_step',step=step,screen_sha256=hashlib.sha256(png).hexdigest(),action_kind=action['action'],crash_candidate=signature is not None)
                if signature:
                    (out/'candidate-crash.txt').write_text(log)
                    break
            if signature:
                reproduced = []
                for repeat in range(replays):
                    check_time()
                    replay_used += 1
                    self.device.reset(package,activity)
                    for item in actions:
                        check_time()
                        text = '<tool_call>'+json.dumps({'name':'computer_use','arguments':item['action']})+'</tool_call>'
                        replay_used += 1
                        self.device.execute(text,item['width'],item['height'])
                    check_time()
                    replay_used += 1
                    log = self.device.crash_log()
                    (out/f'replay-{repeat:02d}.txt').write_text(log)
                    reproduced.append(crash_signature(log,package) == signature)
                status = ('reproduced_crash' if all(reproduced) else 'unstable_crash') if replays else 'candidate_crash'
            elif status == 'unresolved':
                status = 'no_crash_observed'
            self.ledger.settle(replay_id,{'model_calls':0,'device_steps':replay_used})
        except Exception as error:
            # Preserve prepaid verification on failure; no false negative label.
            error_type = type(error).__name__
            status = 'budget_exhausted' if isinstance(error,BudgetExceeded) else 'unresolved'
        report = {'schema':1,'scope':'GUI crash replay; not fixed-version defect confirmation',
                  'status':status,'crash_signature':signature,'actions':actions,'calls':calls,
                  'error_type':error_type,'elapsed_seconds':time.monotonic()-started,
                  'available':self.ledger.available(),
                  'deadline_semantics':'checked between bounded calls; one in-flight call may exceed session deadline',
                  'reset_semantics':'pm clear and launch; external services and OS settings are not reset'}
        (out/'report.json').write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
        self.journal.append('gui_session_completed',status=status,error_type=error_type,steps=len(actions))
        return report
