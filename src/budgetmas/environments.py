"""Platform adapters for checkpoint-based GUI mechanism experiments.

Every backend exposes the same contract: restore a registered initial state,
apply one bounded computer_use action, read the visible UI, and return an
evaluation-only oracle match. Android and Web differ only in how these are
implemented; the paired arms, budget ledger and replay verifier do not.
"""
import hashlib
import re

from .android import Android
from .pilot_device import Device as AndroidDevice
from .ui_nodes import find_target, inspect_checkpoint, matches, state_hash, ui_features


PLATFORMS = ('android', 'web')


def validate_app(app, platform):
    if not isinstance(app, dict):
        raise ValueError('invalid application definition')
    if platform == 'android':
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.]+', app.get('package', '')):
            raise ValueError('invalid Android package')
        if not re.fullmatch(r'[A-Za-z0-9_.$]+', app.get('activity', '')):
            raise ValueError('invalid Android activity')
    elif platform == 'web':
        if not isinstance(app.get('entry'), str) or not app['entry'].strip():
            raise ValueError('Web app requires an entry URL or registered fixture')
        if not isinstance(app.get('steps', []), list):
            raise ValueError('invalid Web checkpoint setup')
    else:
        raise ValueError('unsupported platform')
    if not isinstance(app.get('group'), str) or not re.fullmatch(r'[a-z][a-z0-9-]*', app['group']):
        raise ValueError('invalid application group')
    return app


def app_domain(app, platform):
    return app.get('domain') or (app['package'] if platform == 'android' else app['entry'])


class AndroidCheckpoint:
    """Registered Android checkpoint backed by ADB/UIAutomator."""

    platform = 'android'

    def __init__(self, android, app):
        self.android = Android(android.serial, timeout=android.timeout) if isinstance(android, Android) else android
        self.app = app
        self.device = AndroidDevice(self.android)

    def launch(self):
        self.device.launch(self.app)

    def nodes(self):
        return [node for node in self.device.nodes()
                if node.get('package') == self.app['package']]

    def screenshot(self):
        return self.android.screenshot()

    def locate(self, phrase):
        return find_target(self.nodes(), (phrase,))

    def tap(self, location):
        self.device.tap(location)

    def execute(self, raw, width, height):
        return self.android.execute(raw, width, height)

    def state(self, case):
        return ui_features(self.nodes(), self.app['package'])

    def initial_hash(self):
        return state_hash(self.nodes(), self.app['package'])

    def oracle(self, case):
        return matches(self.nodes(), case)

    def inspect(self, case):
        return inspect_checkpoint(self.nodes(), self.app['package'], case)

    def close(self):
        return None


class BrowserCheckpoint:
    """Playwright-backed Web checkpoint, used only when a protocol selects Web."""

    platform = 'web'

    def __init__(self, target, app):
        from playwright.sync_api import sync_playwright

        self.app = app
        self.entry = app['entry']
        self._pw = sync_playwright().start()
        try:
            self.browser = self._pw.chromium.launch(headless=True)
        except Exception:
            self._pw.stop()
            raise
        self.context = None
        self.console_errors = []

    def launch(self):
        if self.context is not None:
            self.context.close()
        self.context = self.browser.new_context(viewport=self.app.get('viewport', {'width':1280,'height':800}),
                                                 locale='en-US', timezone_id='UTC')
        origin = self.app.get('local_origin')
        if origin:
            self.context.route('**/*', lambda route: route.continue_()
                               if route.request.url.startswith(origin + '/') else route.abort())
        self.page = self.context.new_page()
        self.page.on('console', lambda msg: self.console_errors.append(msg.text) if msg.type == 'error' else None)
        self.page.on('pageerror', lambda error: self.console_errors.append(str(error)))
        self.page.goto(self.entry, wait_until='domcontentloaded', timeout=15000)
        for step in self.app.get('setup', []):
            self._setup_step(step)
        self.page.wait_for_timeout(150)

    def _setup_step(self, step):
        phrase = step if isinstance(step, str) else step['label']
        location = self.locate(phrase)
        if location is None:
            raise RuntimeError('registered Web setup control unavailable: ' + phrase)
        self.page.mouse.click(*location)
        self.page.wait_for_timeout(120)

    def nodes(self):
        raw = self.page.evaluate("""() => {
          const out = [];
          for (const el of document.querySelectorAll('body *')) {
            const box = el.getBoundingClientRect();
            if (box.width <= 0 || box.height <= 0) continue;
            const style = getComputedStyle(el);
            if (style.visibility === 'hidden' || style.display === 'none') continue;
            const text = (el.innerText || '').trim().split('\\n')[0];
            const label = el.getAttribute('aria-label') || el.getAttribute('name') || '';
            const selected = el.getAttribute('aria-selected') || '';
            const hit = document.elementFromPoint(Math.floor((box.left + box.right)/2),
                                                  Math.floor((box.top + box.bottom)/2));
            out.push({
              resource: el.id || '', role: el.getAttribute('role') || el.tagName.toLowerCase(),
              name: label, label: label, text: text, description: label, class: el.tagName,
              'aria-selected': selected, selected: selected,
              clickable: el.matches('a,button,input,select,textarea,[role=button],[onclick]'),
              enabled: el.disabled || el.getAttribute('aria-disabled') === 'true' ? 'false' : 'true',
              center_hit: el.contains(hit),
              bounds: [box.left, box.top, box.right, box.bottom],
              domain: location.origin,
            });
          }
          return out;
        }""")
        return [dict(node, **{'resource-id': node['resource'], 'content-desc': node['description']})
                for node in raw]

    def screenshot(self):
        return self.page.screenshot(type='png')

    def locate(self, phrase):
        return find_target(self.nodes(), (phrase,))

    def tap(self, location):
        self.page.mouse.click(*location)
        self.page.wait_for_timeout(120)

    def execute(self, raw, width, height):
        from .android import parse_action

        action = parse_action(raw)
        if action['action'] == 'terminate':
            return action
        if 'coordinate' in action:
            x, y = action['coordinate']
            self.page.mouse.click(int(x * width / 1000), int(y * height / 1000))
        if action['action'] == 'type':
            self.page.keyboard.insert_text(action['text'])
        elif action['action'] == 'key':
            key = {'Enter': 'Enter', 'Tab': 'Tab', 'Escape': 'Escape',
                   'Backspace': 'Backspace', 'Back': 'Alt+ArrowLeft'}[action['key']]
            self.page.keyboard.press(key)
        elif action['action'] == 'scroll':
            self.page.mouse.wheel(0, -400 if action['direction'] == 'up' else 400)
        self.page.wait_for_timeout(200)
        return action

    def state(self, case):
        return ui_features(self.nodes(), self.page.evaluate('location.origin'))

    def initial_hash(self):
        return state_hash(self.nodes(), self.page.evaluate('location.origin'))

    def oracle(self, case):
        return matches(self.nodes(), case)

    def inspect(self, case):
        nodes = self.nodes()
        return inspect_checkpoint(nodes, nodes[0]['domain'] if nodes else None, case)

    def close(self):
        try:
            self.browser.close()
        finally:
            self._pw.stop()


class FixtureCheckpoint:
    """Deterministic in-memory Web UI: no browser, network or credentials.

    It is an engineering/adaptation fixture, not a research benchmark. It
    exercises the same checkpoint, action and replay contracts as a real page.
    """

    platform = 'web'
    WIDTH, HEIGHT = 1000, 700

    def __init__(self, target, app):
        self.app = app
        self.steps = app.get('steps', [])
        self.transitions = app.get('transitions', {})
        self.initial = app.get('initial', {'text': 'START'})
        self.text = self.initial['text']
        actions = [
            {'text': label, 'resource-id': 'control', 'role': 'button', 'bounds': [40 + 150 * i, 300, 160 + 150 * i, 360],
             'clickable': 'true', 'enabled': 'true'}
            for i, label in enumerate(self.steps)
        ] or [{'text': 'tap', 'resource-id': 'control', 'role': 'button', 'bounds': [40, 300, 160, 360],
               'clickable': 'true', 'enabled': 'true'}]
        self.controls = actions
        self.goal = app.get('goal')

    def launch(self):
        self.text = self.initial['text']

    def nodes(self):
        return [{'text': self.text, 'resource-id': 'result', 'class': 'status',
                 'bounds': [40, 80, 960, 180], 'clickable': 'false',
                 'selected': '', 'content-desc': '', 'domain': 'fixture'}] + self.controls

    def screenshot(self):
        from .android import png_dimensions
        import struct
        import zlib

        def chunk(kind, payload):
            return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind + payload))

        digest = hashlib.sha256((self.text + repr(self.controls)).encode()).hexdigest()
        rgb = bytes(int(digest[i:i + 2], 16) for i in range(0, 6)) * (self.WIDTH * self.HEIGHT)
        raw = b''.join(b'\x00' + rgb[y * self.WIDTH * 3:(y + 1) * self.WIDTH * 3]
                       for y in range(self.HEIGHT))
        png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', self.WIDTH, self.HEIGHT, 8, 2, 0, 0, 0))
               + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))
        png_dimensions(png)
        return png

    def locate(self, phrase):
        return find_target(self.nodes(), (phrase,))

    def tap(self, location):
        self._tap(location)

    def _tap(self, location):
        for control in self.controls:
            left, top, right, bottom = control['bounds']
            if left <= location[0] <= right and top <= location[1] <= bottom:
                self.text = control['text']
                return
        self.text = self.initial['text']

    def execute(self, raw, width, height):
        from .android import parse_action

        action = parse_action(raw)
        if action['action'] == 'terminate':
            return action
        if 'coordinate' in action:
            x, y = action['coordinate']
            self._tap((int(x * width / 1000), int(y * height / 1000)))
        return action

    def state(self, case):
        return ui_features(self.nodes(), 'fixture')

    def initial_hash(self):
        return state_hash(self.nodes(), 'fixture')

    def oracle(self, case):
        return matches([node for node in self.nodes()
                        if node.get('resource-id') == 'result'], case)

    def inspect(self, case):
        nodes = self.nodes()
        return inspect_checkpoint(nodes, 'fixture', case,
                                  oracle_nodes=[node for node in nodes if node.get('resource-id') == 'result'])

    def close(self):
        return None


def environment_for(platform, target, app):
    if platform == 'android':
        return AndroidCheckpoint(target, app)
    if target == 'fixture' or (isinstance(target, dict) and target.get('backend') == 'fixture'):
        return FixtureCheckpoint(target, app)
    if app.get('backend') == 'miniwob':
        from .miniwob_probe import NativeEpisode
        return NativeEpisode(app)
    return BrowserCheckpoint(target, app)
