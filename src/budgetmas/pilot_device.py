"""Versioned Android task contract and bounded execution for the multi-app pilot."""
import hashlib
import re
import shlex
import time
import xml.etree.ElementTree as ET

from .gui_team import device_call
from .runtime import RoleResult


MODES = ('single', 'collaborative')
FEATURE_NAMES = ('intercept', 'workflow_steps', 'alias_steps', 'collaborative',
                 'collaboration_x_alias_steps', 'primary_phrase_length', 'single_symbol_steps')


def features(task, mode):
    steps = task['steps']
    alias_steps = sum(len(step) > 1 for step in steps) / len(steps)
    collaboration = float(mode == 'collaborative')
    return (1.0, len(steps)/4, alias_steps, collaboration, collaboration*alias_steps,
            sum(len(step[0]) for step in steps)/(40*len(steps)),
            sum(len(step[0]) == 1 for step in steps)/len(steps))


def quotas(task, mode, canonical_tab=False):
    setup = 2 if canonical_tab else 0
    count = len(task['steps'])
    return {
        'proposer': {'agent_calls': 0, 'device_steps': 0},
        'execute': {'agent_calls': count*(2 if mode == 'collaborative' else 1),
                    'device_steps': 2+2*count+setup},
        'verify': {'agent_calls': 0, 'device_steps': 4+count+setup},
    }


def labels(node):
    return (node.get('text', ''), node.get('content-desc', ''))


def tap_location(node):
    match = re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', node.get('bounds',''))
    if not match:
        return None
    left, top, right, bottom = map(int, match.groups())
    if not left < right or not top < bottom:
        return None
    return (left+right)//2, (top+bottom)//2


def find_target(nodes, phrases):
    for phrase in phrases:
        eligible = [node for node in nodes
                    if phrase.casefold() in {label.casefold() for label in labels(node)}
                    and tap_location(node) and node.get('enabled') == 'true']
        if eligible:
            return tap_location(max(eligible, key=lambda node: node.get('clickable') == 'true'))
    return None


def matches(nodes, oracle):
    fields = {'text': 'text', 'description': 'content-desc', 'selected': 'selected'}
    for node in nodes:
        if 'resource' in oracle and node.get('resource-id','').split('/')[-1] != oracle['resource']:
            continue
        if all(node.get(attr) == (str(oracle[field]).lower() if field == 'selected' else oracle[field])
               for field, attr in fields.items() if field in oracle):
            return True
    return False


def state_hash(nodes, package):
    # Time, battery, arbitrary filesystem filenames and other dynamic text are
    # omitted. Track structure and active tab/selection, not byte-identical state.
    structure = [(n.get('resource-id'), n.get('class'), n.get('selected'),
                  n.get('checked'), n.get('bounds'))
                 for n in nodes if n.get('package') == package]
    if not structure:
        raise RuntimeError('configured application has no visible nodes')
    return hashlib.sha256(repr(structure).encode()).hexdigest()


class Device:
    def __init__(self, android):
        self.android = android

    def nodes(self):
        # ADB runs on a dedicated emulator with one experiment process.
        path = '/sdcard/budgetmas-pilot-window.xml'
        result = self.android.adb('shell','uiautomator','dump',path)
        if b'dumped to' not in result:
            raise RuntimeError('UI hierarchy dump failed')
        raw = self.android.adb('exec-out','cat',path)
        return list(ET.fromstring(raw).iter('node'))

    def launch(self, app):
        package, activity = app['package'], app['activity']
        if app.get('clear_data'):
            self.android.reset(package,activity)
        else:
            self.android.adb('shell','am','force-stop',package)
            result = self.android.adb('shell','am','start','-W','-n',shlex.quote(package+'/'+activity))
            if b'Status: ok' not in result:
                raise RuntimeError('application launch failed')
        time.sleep(.25)

    def tap(self, location):
        self.android.adb('shell','input','tap',*map(str,location))
        time.sleep(.2)


class PrimaryAgent:
    def propose(self, nodes, phrases):
        return find_target(nodes, phrases[:1])


class LocatorAgent:
    def review(self, nodes, phrases, proposed):
        return proposed if proposed is not None else find_target(nodes, phrases[1:])


class Trial:
    """Oracle is invoked only by verify, after the action path is fixed."""
    def __init__(self, device, app, task, mode):
        self.device, self.app, self.task, self.mode = device,app,task,mode
        self.primary, self.locator = PrimaryAgent(), LocatorAgent()

    def observe_initial(self, initial_state, seed):
        return tuple(tuple(step) for step in self.task['steps'])

    def propose(self, observation, quota):
        return RoleResult(observation, dict.fromkeys(quota,0))

    @staticmethod
    def agent_call(quota, role_id, callback):
        amount = {'agent_calls':1,'device_steps':0}
        quota.ledger.reserve(role_id,amount)
        result = callback()
        quota.ledger.settle(role_id,amount)
        return result

    def reset(self, ledger):
        device_call(ledger,lambda:self.device.launch(self.app))
        tab = self.app.get('canonical_tab')
        if tab:
            package = self.app['package']
            nodes = [node for node in device_call(ledger,self.device.nodes) if node.get('package')==package]
            target = find_target(nodes,(tab,))
            if target is None:
                raise RuntimeError('canonical tab is unavailable')
            if not any(tab in labels(node) and node.get('selected')=='true' for node in nodes):
                device_call(ledger,lambda:self.device.tap(target))

    def execute(self, initial_state, seed, proposal, quota):
        ledger = quota.ledger
        self.reset(ledger)
        nodes = device_call(ledger,self.device.nodes)
        package = self.app['package']
        initial = state_hash(nodes,package)
        taps, decisions = [], []
        for index,phrases in enumerate(proposal):
            local = [node for node in nodes if node.get('package') == package]
            def propose():
                if self.mode == 'single_alias':
                    return find_target(local,phrases)
                return self.primary.propose(local,phrases)
            suggested = self.agent_call(quota,f'primary-{index}',propose)
            chosen = suggested
            if self.mode == 'collaborative':
                chosen = self.agent_call(quota,f'locator-{index}',lambda:self.locator.review(local,phrases,suggested))
            decisions.append({'primary':suggested,'chosen':chosen})
            if chosen is None:
                break
            device_call(ledger, lambda:self.device.tap(chosen))
            taps.append(chosen)
            nodes = device_call(ledger,self.device.nodes)
        return RoleResult({'taps':taps,'initial':initial,'decisions':decisions},
                          ledger.audit()['spent'])

    def verify(self, initial_state, seed, proposal, evidence, quota):
        ledger = quota.ledger
        package = self.app['package']
        oracle = self.task['oracle']
        def check():
            nodes = device_call(ledger,self.device.nodes)
            return matches([node for node in nodes if node.get('package') == package],oracle)

        observed = check()
        self.reset(ledger)
        before_nodes = device_call(ledger,self.device.nodes)
        initial_matches = state_hash(before_nodes,package) == evidence['initial']
        initial_positive = matches([node for node in before_nodes if node.get('package') == package], oracle)
        for tap in evidence['taps']:
            device_call(ledger,lambda:self.device.tap(tap))
        replay = check()
        stable = initial_matches and observed == replay and not initial_positive
        status = ('task_completed' if observed else 'task_incomplete') if stable else 'unresolved'
        return RoleResult({'status':status, 'verification':{
            'observed':observed,'replay':replay,'initial_matches':initial_matches,
            'initial_positive':initial_positive,'initial_hash':evidence['initial'],
            'decisions':evidence['decisions'],
        }}, ledger.audit()['spent'])
