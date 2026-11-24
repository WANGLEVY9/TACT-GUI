"""Real GUI execution adapter for TeamExecutor's shared budget scopes.

The instruction proposer and syntax critic are deterministic baselines. Only
execution calls GUI-Plus. Crash labels/known signatures live in the verifier.
"""
import json
import uuid
from pathlib import Path
from .android import parse_action
from .gui_session import GUISession, crash_signature
from .providers import GUIPlus
from .runtime import RoleResult


def zeros(quota): return dict.fromkeys(quota,0)


def device_call(ledger, callback):
    amount = dict.fromkeys(ledger.available(),0); amount['device_steps'] = 1
    rid = uuid.uuid4().hex
    ledger.reserve(rid,amount)
    result = callback()
    ledger.settle(rid,amount)
    return result


class InstructionProposer:
    def __init__(self,instruction):
        if not isinstance(instruction,str) or not instruction.strip(): raise ValueError('instruction required')
        self.instruction = instruction
    def propose(self,observation,quota):
        return RoleResult({'instruction':self.instruction},zeros(quota))


class PolicyCritic:
    def critique(self,observation,proposal,quota):
        accepted = (isinstance(proposal,dict) and isinstance(proposal.get('instruction'),str)
                    and 0 < len(proposal['instruction']) <= 8000)
        return RoleResult(accepted,zeros(quota))


class GUIEnvironment:
    def __init__(self,device,package,activity,output,journal,*,max_steps=4,provider_factory=GUIPlus):
        self.device,self.package,self.activity = device,package,activity
        self.output,self.journal = Path(output),journal
        self.max_steps,self.provider_factory = max_steps,provider_factory

    def observe_initial(self,initial_state,seed):
        # Metadata only. Actual reset and screenshots occur inside charged scope.
        return {'package':self.package,'initial_state':initial_state,'seed':seed}

    def execute(self,initial_state,seed,proposal,quota):
        session = GUISession(self.device,self.provider_factory(quota.ledger),quota.ledger,self.journal)
        report = session.run(self.package,self.activity,proposal['instruction'],
                             self.output/uuid.uuid4().hex,max_steps=self.max_steps,replays=0)
        if report['status'] in ('unresolved','budget_exhausted'):
            raise RuntimeError('GUI execution incomplete: '+str(report['error_type']))
        return RoleResult(report,quota.ledger.audit()['spent'])


class CrashReplayVerifier:
    def __init__(self,device,package,activity,output,*,repeats=2,known_signatures=None,outcome_probe=None):
        if type(repeats) is not int or repeats < 1: raise ValueError('positive replay count required')
        self.device,self.package,self.activity = device,package,activity
        self.output = Path(output)
        self.repeats,self.known = repeats,known_signatures or {}
        self.outcome_probe = outcome_probe

    def verify(self,initial_state,seed,proposal,evidence,quota):
        signatures = []
        outcomes = []
        if self.outcome_probe:
            outcomes.append(device_call(quota.ledger,self.outcome_probe))
        out = self.output/uuid.uuid4().hex; out.mkdir(parents=True,exist_ok=False)
        for i in range(self.repeats):
            device_call(quota.ledger,lambda:self.device.reset(self.package,self.activity))
            for item in evidence['actions']:
                text = '<tool_call>'+json.dumps({'name':'computer_use','arguments':item['action']})+'</tool_call>'
                parse_action(text)
                device_call(quota.ledger,lambda:self.device.execute(text,item['width'],item['height']))
            log = device_call(quota.ledger,self.device.crash_log)
            (out/f'replay-{i:02d}.txt').write_text(log)
            signatures.append(crash_signature(log,self.package))
            if self.outcome_probe:
                outcomes.append(device_call(quota.ledger,self.outcome_probe))
        candidate = evidence.get('crash_signature')
        if candidate and all(s == candidate for s in signatures):
            # Matching a curated known-defect signature is necessary for reward.
            status = 'confirmed' if candidate in self.known else 'reproduced_crash'
            defect_id = self.known.get(candidate)
        elif candidate:
            status,defect_id = 'unstable_crash',None
        else:
            status,defect_id = 'no_crash_observed',None
            if self.outcome_probe:
                status = 'task_completed' if all(outcomes) else 'task_incomplete'
        (out/'verdict.json').write_text(json.dumps({'status':status,'signatures':signatures,'known_defect_id':defect_id,'outcome_checks':outcomes},indent=2))
        return RoleResult({'status':status,'defect_id':defect_id},quota.ledger.audit()['spent'])
