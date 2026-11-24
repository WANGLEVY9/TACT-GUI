"""Sequential campaign coordinator using the learned planner and team runtime.

Environment trials are isolated at initial_state. Dependencies express verified
completion metadata, not a simulator snapshot carried from a previous task.
"""
import time


class Campaign:
    def __init__(self, planner, executor, *, dual=None):
        self.planner, self.executor, self.dual = planner, executor, dual

    def run(self, actions, quotas, *, initial_states, split, seed, max_trials=100):
        if max_trials < 1:
            raise ValueError('positive trial bound required')
        pending = {a.id:a for a in actions}
        if len(pending) != len(actions):
            raise ValueError('duplicate action id')
        completed = set()
        results = []
        started = time.monotonic()
        for step in range(max_trials):
            before = self.executor.ledger.available()
            if self.dual:
                self.planner.prices = dict(self.dual.prices)
            decision = self.planner.plan(list(pending.values()),before,completed,slots=1)
            if not decision['actions']:
                reason = 'no_positive_feasible_action'
                break
            action = decision['actions'][0]
            self.executor.journal.append('decision', action_id=action.id, scores=decision['candidate_scores'],
                                         available=before, prices=dict(self.planner.prices))
            result = self.executor.run(action,quotas[action.id],initial_state=initial_states[action.task],
                                       seed=seed+step,split=split,propensity=1.0)
            after = self.executor.ledger.available()
            actual = {k:before[k]-after[k] for k in before}
            results.append({'action_id':action.id,'task':action.task,'actual':actual,**result})
            # One selected configuration consumes its task opportunity, including rejection.
            pending = {k:a for k,a in pending.items() if a.task != action.task}
            if result['status'] in ('confirmed','not_confirmed','task_completed'):
                completed.add(action.task)
            if self.dual:
                remaining_trials = max(1,max_trials-step)
                self.dual.update(actual,{k:before[k]/remaining_trials for k in before})
        else:
            reason = 'trial_limit'
        report = {'stop_reason':reason,'results':results,'available':self.executor.ledger.available(),
                  'elapsed_seconds':time.monotonic()-started,'completed_tasks':sorted(completed)}
        self.executor.journal.append('campaign_completed', **report)
        return report
