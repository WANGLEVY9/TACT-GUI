"""Executable local fault fixture for integration, NOT a research benchmark."""
from .runtime import RoleResult


def charge(quota):
    if set(quota) != {'operations'} or quota['operations'] < 1:
        raise ValueError('fixture requires one operation')
    return {'operations':1}


class Proposer:
    def propose(self, observation, quota):
        return RoleResult(['add','remove','checkout'], charge(quota))


class Critic:
    def critique(self, observation, proposal, quota):
        return RoleResult(all(a in ('add','remove','checkout') for a in proposal), charge(quota))


class Cart:
    def __init__(self, buggy=True): self.buggy = buggy
    def replay(self, actions):
        count = 0
        for action in actions:
            if action == 'add': count += 1
            elif action == 'remove': count -= 1
            elif action == 'checkout' and count == 0 and self.buggy:
                return {'failure':'empty-cart-checkout'}
        return {'failure':None}
    def observe_initial(self, initial_state, seed):
        if initial_state != 'empty-cart': raise ValueError('unknown initial state')
        return {'page':'cart','count':0,'allowed':['add','remove','checkout']}
    def execute(self, initial_state, seed, proposal, quota):
        self.observe_initial(initial_state,seed)
        return RoleResult(self.replay(proposal),charge(quota))


class DifferentialVerifier:
    def verify(self, initial_state, seed, proposal, evidence, quota):
        actual = charge(quota)
        buggy = Cart(True).replay(proposal)
        fixed = Cart(False).replay(proposal)
        confirmed = buggy == evidence and buggy['failure'] is not None and fixed['failure'] is None
        return RoleResult({'status':'confirmed' if confirmed else 'not_confirmed',
                           'defect_id':buggy['failure'] if confirmed else None},actual)
