"""Group-disjoint train/validation pipeline with data provenance."""
import hashlib
import json
import math
from pathlib import Path
from .models import JointModel


def fit(train_rows, validation_rows, dimension, resources):
    if not train_rows or not validation_rows:
        raise ValueError('nonempty train and validation sets required')
    if any(r['split'] != 'train' for r in train_rows) or any(r['split'] != 'validation' for r in validation_rows):
        raise ValueError('held-out data in wrong phase')
    if {r['app_group_id'] for r in train_rows} & {r['app_group_id'] for r in validation_rows}:
        raise ValueError('application leakage')
    ids = [r['id'] for r in train_rows + validation_rows]
    if len(set(ids)) != len(ids):
        raise ValueError('duplicate trajectory id')
    model = JointModel(dimension, resources)
    for row in train_rows:
        if row['verification_status'] not in ('confirmed','not_confirmed'):
            raise ValueError('unresolved observations are not negative labels')
        model.update(row['features'], row['verified_reward'], row['actual'])
    errors = {'reward':[] , **{key:[] for key in resources}}
    for row in validation_rows:
        if row['verification_status'] not in ('confirmed','not_confirmed'):
            raise ValueError('unresolved validation outcome')
        if set(row['actual']) != set(resources): raise ValueError('resource schema mismatch')
        if not math.isfinite(row['verified_reward']): raise ValueError('nonfinite validation reward')
        pred = model.predict(row['features'])
        errors['reward'].append(abs(pred['reward']-row['verified_reward']))
        for key in resources:
            if not math.isfinite(row['actual'][key]) or row['actual'][key] < 0: raise ValueError('invalid validation cost')
            errors[key].append(abs(pred['costs'][key]['median']-row['actual'][key]))
    report = {'mae':{key:sum(vals)/len(vals) for key,vals in errors.items()},
              'train_rows':len(train_rows), 'validation_rows':len(validation_rows)}
    model.metadata = {'feature_dimension':dimension, 'report':report,
                      'data_sha256':hashlib.sha256(json.dumps(train_rows+validation_rows,sort_keys=True,allow_nan=False).encode()).hexdigest(),
                      'train_groups':sorted({r['app_group_id'] for r in train_rows}),
                      'validation_groups':sorted({r['app_group_id'] for r in validation_rows})}
    return model, report
