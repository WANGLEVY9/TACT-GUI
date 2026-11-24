"""Budgeted proposal/critique/execution/replay with auditable failure semantics."""
from dataclasses import dataclass
import hashlib
import json
import math
import os
import time
import uuid
from pathlib import Path
from .scopes import Scope, Quota


class Journal:
    def __init__(self, path):
        self.path = Path(path)
        self.sequence = 0
        self.previous = '0'*64
        if self.path.exists():
            rows = self.read(self.path)
            if rows:
                self.sequence = len(rows)
                self.previous = rows[-1]['hash']

    def append(self, event, **data):
        row = {'sequence': self.sequence, 'previous': self.previous,
               'event': event, 'time_ns': time.time_ns(), 'data': data}
        raw = json.dumps(row, sort_keys=True, allow_nan=False)
        row['hash'] = hashlib.sha256(raw.encode()).hexdigest()
        with self.path.open('a') as stream:
            stream.write(json.dumps(row, sort_keys=True, allow_nan=False)+'\n')
            stream.flush()
            os.fsync(stream.fileno())
        self.previous = row['hash']; self.sequence += 1

    @staticmethod
    def read(path):
        rows = []
        previous = '0'*64
        for line in Path(path).read_text().splitlines():
            row = json.loads(line)
            unsigned = {k:v for k,v in row.items() if k != 'hash'}
            digest = hashlib.sha256(json.dumps(unsigned, sort_keys=True, allow_nan=False).encode()).hexdigest()
            if row['sequence'] != len(rows) or row['previous'] != previous or row['hash'] != digest:
                raise ValueError('corrupt journal')
            previous = row['hash']; rows.append(row)
        return rows


@dataclass(frozen=True)
class RoleResult:
    value: object
    actual: dict


class TeamExecutor:
    """Roles implement propose/critique; env owns actions; verifier owns labels.

    Each callback must enforce its quota. Unknown actual usage on exception is
    deliberately kept reserved. Journal has a single owner (campaign process).
    """
    def __init__(self, ledger, journal, environment, proposer, critic, verifier):
        self.ledger, self.journal, self.env = ledger, journal, environment
        self.proposer, self.critic, self.verifier = proposer, critic, verifier
        self.seen = {row['data']['defect_id'] for row in Journal.read(journal.path) if row['event'] == 'verified' and row['data'].get('status') == 'confirmed'} if journal.path.exists() else set()

    def _call(self, role, upper, callback):
        rid = uuid.uuid4().hex
        role_scope = Scope(self.trial_scope,upper,scope_id=rid)
        self.journal.append('role_started', role=role, reservation_id=rid, upper=upper)
        try:
            result = callback(Quota(upper,role_scope))
            if not isinstance(result, RoleResult):
                raise TypeError('role must return measured usage')
            if set(result.actual) != set(upper) or any(type(v) not in (int,float) or not math.isfinite(v) or v < 0 or v > upper[k] for k,v in result.actual.items()):
                raise ValueError('role exceeded or misreported quota')
            role_scope.close(result.actual)
            for key,value in result.actual.items():
                self.actual[key] += value
            self.journal.append('settled', role=role, reservation_id=rid, actual=result.actual)
            return result.value
        except Exception as error:
            self.journal.append('failed', role=role, reservation_id=rid, error_type=type(error).__name__)
            raise

    def run(self, action, quotas, *, initial_state, seed, split, propensity=1.0):
        if split not in ('train','validation','test') or not 0 < propensity <= 1:
            raise ValueError('invalid trial metadata')
        required = ['proposer','execute','verify'] + (['critic'] if self.critic else [])
        if set(quotas) != set(required):
            raise ValueError('role quota schema mismatch')
        available = self.ledger.available()
        if any(any(type(v) not in (int,float) or not math.isfinite(v) or v < 0 for v in q.values()) for q in quotas.values()):
            raise ValueError('invalid role quota')
        if any(set(q) != set(available) for q in quotas.values()):
            raise ValueError('resource schema mismatch')
        if any(sum(q[k] for q in quotas.values()) > available[k] for k in available):
            raise ValueError('cannot afford complete trial including verification')
        trial_id = uuid.uuid4().hex
        total = {k:sum(q[k] for q in quotas.values()) for k in available}
        if set(action.upper) != set(total) or any(total[k] > action.upper[k] for k in total):
            raise ValueError('role quotas exceed scheduled action bound')
        self.trial_scope = Scope(self.ledger,total,scope_id=trial_id)
        self.actual = dict.fromkeys(available,0)
        self.journal.append('trial', reservation_id=trial_id, upper=total, action_id=action.id, task=action.task,
                            configuration=action.configuration, features=action.features,
                            initial_state=initial_state, seed=seed, split=split,
                            propensity=propensity)
        # Reset is charged as part of execution in this synchronous contract.
        # Real adapters must include reset cost in execute quota.
        observation = self.env.observe_initial(initial_state, seed)
        proposal = self._call('proposer', quotas['proposer'], lambda q:self.proposer.propose(observation,q))
        if self.critic:
            accepted = self._call('critic', quotas['critic'], lambda q:self.critic.critique(observation,proposal,q))
            if not accepted:
                self.journal.append('rejected', action_id=action.id)
                self.trial_scope.close(self.actual)
                return {'status':'rejected', 'reward':None}
        evidence = self._call('execute', quotas['execute'], lambda q:self.env.execute(initial_state,seed,proposal,q))
        verdict = self._call('verify', quotas['verify'], lambda q:self.verifier.verify(initial_state,seed,proposal,evidence,q))
        confirmed = verdict['status'] == 'confirmed'
        defect_id = verdict.get('defect_id')
        if confirmed and not defect_id:
            raise ValueError('confirmed result requires stable deduplication id')
        reward = int(confirmed and defect_id not in self.seen) if verdict['status'] in ('confirmed','not_confirmed') else None
        if confirmed:
            self.seen.add(defect_id)
        result = {'status':verdict['status'], 'reward':reward, 'defect_id':defect_id}
        if 'verification' in verdict:
            result['verification'] = verdict['verification']
        self.trial_scope.close(self.actual)
        self.journal.append('verified', action_id=action.id, actual=self.actual, **result)
        return result
