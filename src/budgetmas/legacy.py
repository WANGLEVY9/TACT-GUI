"""Compatibility adapter for the original Android Device contract.

Existing mechanism and trajectory-audit tests inject a small Device double
whose launch(app), nodes(), android.screenshot() and android.execute() methods
predate the platform-neutral CheckpointEnvironment interface. This wrapper
keeps those contracts working without changing the real Android backend.
"""
from .android import Android
from .pilot_device import find_target
from .ui_nodes import inspect_checkpoint, matches, state_hash, ui_features


def android_environment(target, app):
    """Return the real adapter, or a wrapper for an injected Device double."""
    from .environments import environment_for
    if isinstance(target, Android) or hasattr(target, 'adb'):
        return environment_for('android', target, app)
    return LegacyCheckpoint(target, app)


class LegacyCheckpoint:
    platform = 'android'

    def __init__(self, device, app):
        self.device = device
        self.app = app

    def launch(self):
        self.device.launch(self.app)

    def nodes(self):
        return [n for n in self.device.nodes() if n.get('package') == self.app['package']]

    def screenshot(self):
        return self.device.android.screenshot()

    def locate(self, phrase):
        return find_target(self.nodes(), (phrase,))

    def tap(self, location):
        self.device.tap(location)

    def execute(self, raw, width, height):
        return self.device.android.execute(raw, width, height)

    def state(self, case):
        return ui_features(self.nodes(), self.app['package'])

    def initial_hash(self):
        return state_hash(self.nodes(), self.app['package'])

    def oracle(self, case):
        return matches(self.nodes(), case)

    def inspect(self, case):
        return inspect_checkpoint(self.nodes(), self.app['package'], case)

    def close(self):
        pass
