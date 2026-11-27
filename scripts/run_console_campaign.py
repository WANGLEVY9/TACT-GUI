"""Collect repeated, budgeted Android batches through the loopback console."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import signal
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def console_request(base, path, payload=None):
    request = Request(base + path)
    if payload is not None:
        request = Request(base + path, json.dumps(payload).encode(), {
            'Origin': base, 'Content-Type': 'application/json',
        })
    try:
        with urlopen(request, timeout=18) as response:
            return json.load(response)
    except HTTPError as error:
        detail = json.load(error).get('error', error.reason)
        raise RuntimeError(detail) from error


def collect(args):
    base = f'http://127.0.0.1:{args.port}'
    request = lambda path, payload=None: console_request(base, path, payload)
    profile = next((p for p in request('/api/profiles') if p['id'] == 'development'), None)
    if not profile or args.serial not in request('/api/devices'):
        raise RuntimeError('The development profile and dedicated emulator must be available.')
    if any(job['exit'] is None for job in request('/api/state')['jobs']):
        raise RuntimeError('Stop or finish the active console run before starting a campaign.')
    cases = profile['cases']
    args.out.mkdir(parents=True, exist_ok=False)
    with (args.out / 'events.jsonl').open('x', buffering=1) as journal:
        def record(event, **values):
            row = {'event': event, 'at': timestamp(), **values}
            journal.write(json.dumps(row, ensure_ascii=False) + '\n')
            print(json.dumps(row, ensure_ascii=False), flush=True)

        started = time.monotonic()
        deadline = started + args.seconds
        admitted = 0
        jobs = []
        active = None
        reason = 'deadline'
        interrupted = False

        def interrupt(*_):
            nonlocal interrupted
            interrupted = True

        signal.signal(signal.SIGTERM, interrupt)
        signal.signal(signal.SIGINT, interrupt)
        record('campaign_started', seconds=args.seconds, model_call_limit=args.call_limit,
               serial=args.serial, seed=args.seed, cases=[c['id'] for c in cases])
        try:
            for batch in range(1, args.call_limit + 1):
                if interrupted or time.monotonic() >= deadline:
                    break
                case = cases[(batch - 1) % len(cases)]
                upper = 4 * case['slots']
                if admitted + upper > args.call_limit:
                    reason = 'call_limit'
                    break
                payload = {'profile': 'development', 'cases': [case['id']],
                           'serial': args.serial, 'guard': True, 'progress': True,
                           'timeout': 40, 'seed': (args.seed + batch - 1) % 2**32,
                           'call_limit': upper, 'confirmed': True}
                active = request('/api/start', payload)
                admitted += upper
                row = {'batch': batch, 'case': case['id'], 'upper_calls': upper,
                       'seed': payload['seed'], **active}
                jobs.append(row)
                record('batch_started', **row, campaign_admitted_calls=admitted)
                while not interrupted and time.monotonic() < deadline:
                    job = next((j for j in request('/api/state')['jobs'] if j['id'] == active['job']), None)
                    if job is None:
                        raise RuntimeError('Console no longer owns the active batch.')
                    if job['exit'] is not None:
                        row['exit'] = job['exit']
                        active = None
                        if job['exit'] != 0:
                            reason = 'batch_stopped' if job['exit'] < 0 else 'batch_failed'
                            record('batch_ended', **row)
                            break
                        detail = request('/api/run?' + urlencode({'run': row['run']}))
                        row.update(activity=detail['activity'], counts=detail['counts'],
                                   budget=detail['budget'])
                        record('batch_ended', **row)
                        if (detail['activity'].get('responses', 0) == 0 or
                                detail['counts'].get('unresolved', 0) == 4):
                            reason = 'no_usable_batch'
                        break
                    time.sleep(min(2, max(0, deadline - time.monotonic())))
                if reason != 'deadline' or active is not None:
                    break
            if interrupted:
                reason = 'operator_interrupt'
        except Exception as error:
            reason = 'supervisor_error'
            record('supervisor_error', error=type(error).__name__, detail=str(error))
        finally:
            if active:
                try:
                    request('/api/stop', {'job': active['job']})
                    record('stop_requested', **active)
                except (RuntimeError, OSError) as error:
                    record('stop_status', detail=str(error), **active)
            summary = {'reason': reason, 'elapsed_seconds': round(time.monotonic() - started, 2),
                       'model_call_limit': args.call_limit, 'admitted_calls': admitted, 'batches': jobs}
            (args.out / 'summary.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False) + '\n')
            record('campaign_ended', reason=reason, elapsed_seconds=summary['elapsed_seconds'],
                   admitted_calls=admitted, batches=len(jobs))
        return reason not in ('deadline', 'operator_interrupt', 'call_limit', 'batch_stopped')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--serial', required=True)
    parser.add_argument('--seconds', type=int, default=3600)
    parser.add_argument('--call-limit', type=int, required=True)
    parser.add_argument('--seed', type=int, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.seconds < 1 or args.call_limit < 8 or not 0 <= args.seed < 2**32:
        parser.error('provide positive duration, at least eight calls, and a 32-bit seed')
    raise SystemExit(collect(args))


if __name__ == '__main__':
    main()
