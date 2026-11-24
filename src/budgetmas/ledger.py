"""Durable atomic multi-resource reservations; unknown usage stays reserved."""
import json
import math
import sqlite3
from contextlib import contextmanager

class BudgetExceeded(RuntimeError): pass

def valid(values):
    if not values or any(type(v) not in (int,float) or not math.isfinite(v) or v < 0 for v in values.values()):
        raise ValueError("resource amounts must be finite and nonnegative")

class Ledger:
    def __init__(self, path, limits=None):
        self.path=str(path)
        with self.connect() as c:
            c.execute("CREATE TABLE IF NOT EXISTS limits (resource TEXT PRIMARY KEY, amount REAL)")
            c.execute("CREATE TABLE IF NOT EXISTS reservations (id TEXT PRIMARY KEY, upper_json TEXT, actual_json TEXT, status TEXT)")
            if limits is not None:
                valid(limits)
                existing=dict(c.execute("SELECT resource,amount FROM limits"))
                if existing and existing != limits: raise ValueError("cannot change existing budget")
                c.executemany("INSERT OR IGNORE INTO limits VALUES (?,?)", limits.items())
    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally: c.close()
    @staticmethod
    def balance(c):
        limits=dict(c.execute("SELECT resource,amount FROM limits"))
        used=dict.fromkeys(limits,0.0)
        for upper,actual,status in c.execute("SELECT upper_json,actual_json,status FROM reservations"):
            for key,value in json.loads(actual if status == 'settled' else upper).items(): used[key]+=value
        return {k:limits[k]-used[k] for k in limits}
    def available(self):
        with self.connect() as c: return self.balance(c)
    def reserve(self, reservation_id, upper):
        valid(upper)
        with self.connect() as c:
            available=self.balance(c)
            if set(upper)!=set(available): raise ValueError("all budget dimensions required")
            if any(upper[k]>available[k] for k in upper): raise BudgetExceeded("insufficient resources")
            c.execute("INSERT INTO reservations VALUES (?,?,NULL,'pending')",(reservation_id,json.dumps(upper)))
    def settle(self, reservation_id, actual):
        valid(actual)
        with self.connect() as c:
            row=c.execute("SELECT upper_json,status FROM reservations WHERE id=?",(reservation_id,)).fetchone()
            if not row or row[1]!='pending': raise ValueError("unknown or settled reservation")
            upper=json.loads(row[0])
            if set(actual)!=set(upper) or any(actual[k]>upper[k] for k in actual):
                raise BudgetExceeded("actual usage exceeds reservation; reservation retained for reconciliation")
            c.execute("UPDATE reservations SET actual_json=?,status='settled' WHERE id=?",(json.dumps(actual),reservation_id))

    def audit(self):
        """Return durable accounted usage and unresolved commitments, not invoices."""
        with self.connect() as c:
            limits=dict(c.execute("SELECT resource,amount FROM limits"))
            spent=dict.fromkeys(limits,0.0)
            reserved=dict.fromkeys(limits,0.0)
            pending=[]
            for rid,upper,actual,status in c.execute("SELECT id,upper_json,actual_json,status FROM reservations ORDER BY id"):
                amount=json.loads(actual if status=='settled' else upper)
                target=spent if status=='settled' else reserved
                for key,value in amount.items(): target[key]+=value
                if status!='settled': pending.append(rid)
            return {'limits':limits,'spent':spent,'reserved':reserved,
                    'available':{k:limits[k]-spent[k]-reserved[k] for k in limits},
                    'pending_reservations':pending}
