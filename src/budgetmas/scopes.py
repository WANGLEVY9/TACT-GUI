"""Hierarchical durable reservations in a single SQLite accounting domain.

Parent commitments include child spending until close: no double charging.
Unknown child usage prevents close and preserves the parent's commitment.
"""
import json
import uuid
from .ledger import valid, BudgetExceeded


class Quota(dict):
    def __init__(self, amounts, ledger):
        super().__init__(amounts)
        self.ledger = ledger


class Scope:
    def __init__(self, parent, limits, *, scope_id=None):
        valid(limits)
        self.parent = parent
        self.root = parent.root if isinstance(parent,Scope) else parent
        self.id = scope_id or uuid.uuid4().hex
        parent.reserve(self.id,limits)
        with self.root.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS scopes (id TEXT PRIMARY KEY, limits_json TEXT, status TEXT)')
            c.execute('CREATE TABLE IF NOT EXISTS scope_calls (scope_id TEXT, id TEXT, upper_json TEXT, actual_json TEXT, status TEXT, PRIMARY KEY(scope_id,id))')
            c.execute('INSERT INTO scopes VALUES (?,?,?)',(self.id,json.dumps(limits),'open'))

    def _state(self,c):
        row = c.execute('SELECT limits_json,status FROM scopes WHERE id=?',(self.id,)).fetchone()
        if not row or row[1] != 'open':
            raise ValueError('scope not open')
        limits = json.loads(row[0])
        spent = dict.fromkeys(limits,0.0); reserved = dict.fromkeys(limits,0.0); pending = []
        count = 0
        for rid,upper,actual,status in c.execute('SELECT id,upper_json,actual_json,status FROM scope_calls WHERE scope_id=?',(self.id,)):
            count += 1
            target = spent if status == 'settled' else reserved
            for k,v in json.loads(actual if status == 'settled' else upper).items(): target[k] += v
            if status != 'settled': pending.append(rid)
        return {'limits':limits,'spent':spent,'reserved':reserved,'pending_reservations':pending,
                'available':{k:limits[k]-spent[k]-reserved[k] for k in limits},'bookings':count}

    def audit(self):
        with self.root.connect() as c: return self._state(c)

    def available(self): return self.audit()['available']

    def reserve(self,rid,upper):
        valid(upper)
        with self.root.connect() as c:
            available = self._state(c)['available']
            if set(upper) != set(available): raise ValueError('resource schema mismatch')
            if any(upper[k] > available[k] for k in upper): raise BudgetExceeded('scope quota exhausted')
            c.execute('INSERT INTO scope_calls VALUES (?,?,?,NULL,?)',(self.id,rid,json.dumps(upper),'pending'))

    def settle(self,rid,actual):
        valid(actual)
        with self.root.connect() as c:
            self._state(c)
            row = c.execute('SELECT upper_json,status FROM scope_calls WHERE scope_id=? AND id=?',(self.id,rid)).fetchone()
            if not row or row[1] != 'pending': raise ValueError('unknown or settled scoped call')
            upper = json.loads(row[0])
            if set(actual) != set(upper) or any(actual[k] > upper[k] for k in upper):
                raise BudgetExceeded('scoped usage exceeds reservation')
            c.execute('UPDATE scope_calls SET actual_json=?,status=? WHERE scope_id=? AND id=?',(json.dumps(actual),'settled',self.id,rid))

    def close(self,actual=None):
        with self.root.connect() as c:
            state = self._state(c)
            if state['pending_reservations']: raise ValueError('unresolved scoped calls prevent settlement')
            if actual is None: actual = state['spent']
            valid(actual)
            if set(actual) != set(state['limits']) or any(actual[k] > state['limits'][k] for k in actual):
                raise BudgetExceeded('scope usage exceeds limits')
            if state['bookings'] and actual != state['spent']:
                raise ValueError('role usage differs from instrumented calls')
            # On a process crash between these transactions parent stays committed.
            c.execute('UPDATE scopes SET status=? WHERE id=?',('closed',self.id))
        self.parent.settle(self.id,actual)
        return actual
