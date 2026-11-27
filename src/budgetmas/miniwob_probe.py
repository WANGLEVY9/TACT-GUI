"""Smoke-test pinned MiniWoB++ tasks in Chromium with their native oracle."""
import argparse
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
import json
from pathlib import Path
import re
import subprocess
from threading import Thread
from urllib.parse import urlsplit

from .environments import BrowserCheckpoint
from .gui_team import device_call
from .ledger import Ledger
from .pilot import digest, write
from .scopes import Scope


REVISION = 'a7658beb68e1d3572329e06734c5822bd284ddc8'
TASKS = {'click-button':1, 'click-dialog':1, 'click-button-sequence':2}
NATIVE_TASKS = {*TASKS,'click-link','click-color','click-tab-2-easy','click-dialog-2','click-shape'}


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


class Episode(BrowserCheckpoint):
    def __init__(self, origin, **settings):
        super().__init__(None, {'entry':origin, 'local_origin':origin, **settings})
        self.origin = origin

    def launch_task(self, task, seed):
        self.entry = f'{self.origin}/miniwob/{task}.html'
        super().launch()
        self.page.evaluate("""seed => {
            Math.seedrandom(seed);
            core.setDataMode('test');
            core.startEpisodeReal();
        }""", seed)

    def observation(self):
        return self.page.evaluate("""() => {
          const query = document.querySelector('#query').innerText;
          const buttons = Array.from(document.querySelectorAll('#area button, .ui-dialog button'));
          const controls = buttons.flatMap(el => {
            const {left, top, width, height} = el.getBoundingClientRect();
            if (!width || !height) return [];
            const label = (el.getAttribute('aria-label') || el.textContent || el.title).trim();
            const points = [[.5,.5], [.25,.25], [.75,.25], [.25,.75], [.75,.75]];
            const point = points.map(([x,y]) => [Math.floor(left + width*x), Math.floor(top + height*y)])
                .find(([x,y]) => el.contains(document.elementFromPoint(x,y))) || null;
            return [{label, point, box:[left,top,width,height]}];
          });
          return {query, controls};
        }""")

    def native_reward(self):
        return self.page.evaluate("""() => ({
            done: WOB_DONE_GLOBAL, raw: WOB_RAW_REWARD_GLOBAL, reason: WOB_REWARD_REASON
        })""")


class NativeEpisode(Episode):
    """MiniWoB++ continuation with the published episode reward as its oracle."""
    def __init__(self, app):
        origin = urlsplit(app['entry'])
        if (origin.scheme != 'http' or origin.hostname != '127.0.0.1' or not origin.port
                or origin.path or origin.query or origin.fragment or origin.username or origin.password):
            raise ValueError('MiniWoB++ requires a local benchmark asset server')
        if app.get('task') not in NATIVE_TASKS or type(app.get('seed')) is not int or app['seed'] < 0:
            raise ValueError('unregistered MiniWoB++ task or seed')
        super().__init__(app['entry'], viewport={'width':320,'height':320})
        if self.browser.version != '131.0.6778.33':
            self.close()
            raise ValueError('unexpected Chromium build for the frozen Web protocol')
        self.task, self.seed = app['task'], app['seed']

    def launch(self):
        self.launch_task(self.task,self.seed)

    def inspect(self, case):
        observation = self.observation()
        if (observation['query'] != case['start'].get('utterance') or case['goal'] != observation['query']
                or case['oracle'] != {'native_raw_reward':1} or self.native_reward()['done']):
            raise RuntimeError('MiniWoB++ restored a different or terminal episode')
        markup = self.page.evaluate("() => document.querySelector('#area').outerHTML")
        visible_hash = digest({'visible':observation,'task_dom':markup})
        features = self.state(case)
        if case.get('match_visible_state'):
            features['decision_observation_sha256'] = visible_hash
        return visible_hash, features

    def oracle(self, spec):
        if spec != {'native_raw_reward':1}:
            raise ValueError('unregistered MiniWoB++ oracle')
        result = self.native_reward()
        return result['done'] and result['raw']==1


def visible_intent(query):
    text = re.fullmatch(r'Click on the "(.+)" button\.', query)
    if text:
        return [text[1]]
    if query == 'Click button ONE, then click button TWO.':
        return ['ONE', 'TWO']
    if query == 'Close the dialog box by clicking the "x".':
        return ['Close']
    return []


def candidates_for(observation):
    return [[el['point'] for el in observation['controls']
             if el['label'].casefold() == label.casefold() and el['point']]
            for label in visible_intent(observation['query'])]


def trial(env, ledger, folder, task, seed, negative, *, candidate=None):
    folder.mkdir()
    used = Scope(ledger, {'device_steps':2 * (2 + 3 * TASKS[task])})

    def restore():
        device_call(used,lambda:env.launch_task(task, seed))
        return device_call(used,env.observation)

    def drive(locations, prefix):
        results = []
        for i, location in enumerate(locations):
            (folder/f'{prefix}-{i}.png').write_bytes(device_call(used,env.screenshot))
            device_call(used,lambda:env.tap(location))
            results.append(device_call(used,env.native_reward))
            if results[-1]['done']:
                break
        return results

    before = restore()
    candidates = candidates_for(before)
    locations = [points[0] for points in candidates] if all(len(points) == 1 for points in candidates) else []
    if candidate is not None:
        locations = [candidates[0][candidate]]
    if negative:
        locations.reverse()
    observed = drive(locations, 'original')
    after = restore()
    replay = drive(locations, 'replay')
    cost = used.close()
    expected = -1 if negative else 1
    initial_match = digest(before) == digest(after)
    passed = (initial_match and observed == replay and bool(observed)
              and observed[-1]['done'] and observed[-1]['raw'] == expected)
    record = {'task':task,'seed':seed,'negative_control':negative,'utterance':before['query'],
              'first_state_sha256':digest(before),'replay_state_sha256':digest(after),
              'initial_match':initial_match,'candidate_counts':[len(points) for points in candidates],
              'locations':locations,'observed':observed,
              'replay':replay,'device_steps':cost['device_steps'],'passed':passed}
    write(folder/'report.json',record)
    return record


@contextmanager
def serve_assets(asset_root):
    git = ['git', '-C', str(asset_root)]
    revision = subprocess.check_output([*git, 'rev-parse', 'HEAD'], text=True).strip()
    dirty = subprocess.check_output([*git, 'status', '--porcelain'], text=True).strip()
    if revision != REVISION or dirty:
        raise ValueError('a clean MiniWoB++ v1.1 checkout is required')
    handler = partial(QuietHandler, directory=str(asset_root/'miniwob'/'html'))
    with ThreadingHTTPServer(('127.0.0.1', 0), handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f'http://127.0.0.1:{server.server_port}'
        finally:
            server.shutdown()
            thread.join()


def run(asset_root, output, seeds):
    schedules = [(task, seed, False) for task in TASKS for seed in seeds]
    schedules += [('click-button-sequence', seed, True) for seed in seeds]
    limit = sum(2 * (2 + 3 * TASKS[task]) for task, _, _ in schedules)
    with serve_assets(asset_root) as origin:
        output.mkdir(parents=True, exist_ok=False)
        ledger = Ledger(output/'budget.sqlite', {'device_steps':limit})
        env = Episode(origin)
        try:
            browser = env.browser.version
            trials = []
            for task,seed,negative in schedules:
                name = f'{task}-{seed}' + ('-reversed' if negative else '')
                trials.append(trial(env, ledger, output/name, task, seed, negative))
            ambiguous = [(row['task'], row['seed'], option) for row in trials
                         if row['task']=='click-button' and len(row['candidate_counts']) == 1
                         and row['candidate_counts'][0] > 1
                         for option in range(row['candidate_counts'][0])]
            audit_rows = []
            audit_budget = None
            if ambiguous:
                audit = Ledger(output/'ambiguity-budget.sqlite', {'device_steps':10*len(ambiguous)})
                for task,seed,option in ambiguous:
                    result = trial(env, audit, output/f'{task}-{seed}-option-{option}', task, seed,
                                   False, candidate=option)
                    audit_rows.append({**result, 'candidate_index':option})
                audit_budget = audit.audit()
        finally:
            env.close()
    report = {'scope':'published Web benchmark adapter validation with a template-based controller',
              'model_calls':0,'source':'https://github.com/Farama-Foundation/MiniWoB-plusplus',
              'revision':REVISION,'license':'MIT','browser':browser,'playwright':version('playwright'),
              'seeds':seeds,'viewport':[1280,800],'locale':'en-US','timezone':'UTC',
              'positive_passed':sum(t['passed'] for t in trials if not t['negative_control']),
              'negative_passed':sum(t['passed'] for t in trials if t['negative_control']),
              'budget':ledger.audit(),'trials':trials,
              'ambiguity_audit':{'trials':audit_rows, 'budget':audit_budget}}
    write(output/'report.json',report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('asset_root',type=Path)
    parser.add_argument('output',type=Path)
    parser.add_argument('--seeds',nargs='+',type=int,default=list(range(8)))
    args = parser.parse_args()
    if any(seed < 0 for seed in args.seeds) or len(set(args.seeds)) != len(args.seeds):
        parser.error('seeds must be distinct nonnegative integers')
    report = run(args.asset_root.resolve(), args.output, args.seeds)
    print(json.dumps({k:v for k,v in report.items() if k!='trials'},indent=2))


if __name__ == '__main__':
    main()
