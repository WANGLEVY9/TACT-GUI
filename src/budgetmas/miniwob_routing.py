"""Run frozen routing protocols on native MiniWoB++ Web tasks."""
import argparse
from importlib.metadata import version
import json
from pathlib import Path

from .mechanism import run, web_protocol
from .miniwob_probe import REVISION, serve_assets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('protocol', type=Path)
    parser.add_argument('routing_protocol', type=Path, nargs='?')
    parser.add_argument('asset_root', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    config = json.loads(args.protocol.read_text())
    matrix = json.loads(args.routing_protocol.read_text()) if args.routing_protocol else config
    arms = config.get('arms', sorted(matrix['variants']))
    config['variants'] = {arm:matrix['variants'][arm] for arm in arms}
    playwright = version('playwright')
    if playwright != '1.49.1':
        raise ValueError('use the registered Playwright 1.49.1 environment')
    config['environment'] = {'source_revision':REVISION,'playwright':playwright,
                             'browser':'131.0.6778.33','viewport':[320,320],
                             'locale':'en-US','timezone':'UTC','network':'local benchmark origin'}
    with serve_assets(args.asset_root.resolve()) as origin:
        config['apps'] = {group:{**app,'entry':origin} for group,app in config['apps'].items()}
        apps = web_protocol(config)
        run(config, apps, {group:app.get('task',group) for group,app in apps.items()}, args.output)


if __name__ == '__main__':
    main()
