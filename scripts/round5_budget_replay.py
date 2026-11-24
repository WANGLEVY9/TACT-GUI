"""Replay recorded policy prefixes under tighter admission caps, offline only."""
import argparse
import hashlib
import json
from pathlib import Path

from budgetmas.pilot import action_for, validate_manifest


def replay(manifest, comparison, caps):
    validate_manifest(manifest)
    if comparison['manifest_sha256'] != manifest['sha256']:
        raise ValueError('comparison and manifest differ')
    test = {r['id']: r for r in manifest['split']['rows'] if r['split'] == 'test'}
    resources = set(comparison['resources'])
    trials = []
    for arm in comparison['summaries']:
        recorded = arm['trials']
        if len(recorded) != arm['task_count'] or any(r['success'] not in (0, 1) for r in recorded):
            raise ValueError('replay requires complete, resolved recorded arms')
        sequence = []
        for r in recorded:
            task = test[r['task_id']]
            match = comparison['task_initial_states'][task['id']]
            if task['app_group_id'] != arm['app_group_id'] or not match['hashes_match']:
                raise ValueError('recorded task has an unmatched initial state')
            upper = action_for(task, r['configuration']).upper
            if set(r['actual']) != resources or set(upper) != resources or any(
                    not 0 <= r['actual'][k] <= upper[k] for k in resources):
                raise ValueError('recorded spend and admission bounds disagree')
            sequence.append((r, upper))
        for cap in caps:
            totals = dict.fromkeys(resources, 0)
            admitted = 0
            completed = 0
            first_rejected = None
            for record, upper in sequence:
                if any(totals[key] + upper[key] > cap[key] for key in resources):
                    first_rejected = record['task_id']
                    break
                admitted += 1
                completed += record['success']
                for key in resources:
                    totals[key] += record['actual'][key]
            trials.append({'app': arm['app_group_id'], 'policy': arm['policy'],
                           'cap': cap, 'admitted': admitted, 'completed': completed,
                           'spent': totals, 'first_rejected': first_rejected})
    return trials


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('comparison', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_bytes())
    comparison_bytes = args.comparison.read_bytes()
    comparison = json.loads(comparison_bytes)
    base = comparison['resources']
    caps = [dict(base, agent_calls=calls) for calls in (1, 2, 3, 4)]
    caps += [dict(base, device_steps=steps) for steps in (9, 13, 18, 22, 24)]
    caps.append(base)
    results = replay(manifest, comparison, caps)
    report = {'scope': 'offline fixed-prefix replay; no policy rerouting or new executions',
              'manifest_sha256': manifest['sha256'],
              'comparison_file_sha256': hashlib.sha256(comparison_bytes).hexdigest(),
              'results': results}
    with args.output.open('x') as stream:
        json.dump(report, stream, indent=2)
        stream.write('\n')
    print('caps',len(caps),'app-policy-cap rows',len(results))


if __name__ == '__main__':
    main()
