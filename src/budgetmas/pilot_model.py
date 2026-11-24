"""CPU model fit with application-group cross-validation and portable artifacts."""
import hashlib
import math
from collections import Counter
from pathlib import Path
import json

from .models import BayesianHead, solve_spd
from .pilot import action_for, digest, validate_manifest, write
from .pilot_device import FEATURE_NAMES, MODES


COSTS = ('agent_calls', 'device_steps', 'elapsed_seconds')


def group_metric(rows, values):
    groups = Counter(row['app_group_id'] for row in rows)
    return sum(value/groups[row['app_group_id']] for row,value in zip(rows,values))/len(groups)


def splits(rows):
    groups = sorted({row['app_group_id'] for row in rows})
    if len(groups) < 2:
        raise ValueError('grouped model selection requires at least two training apps')
    return [([row for row in rows if row['app_group_id'] != group],
             [row for row in rows if row['app_group_id'] == group]) for group in groups]


def reward_fit(rows, prior):
    head = BayesianHead(len(FEATURE_NAMES),prior=prior)
    for row in rows:
        head.update(row['features'],row['success'])
    return solve_spd(head.precision,head.rhs)


def reward_predict(weights,x):
    return max(0.0,min(1.0,sum(w*v for w,v in zip(weights,x))))


def cost_fit(rows, target, depth, seed):
    from catboost import CatBoostRegressor
    estimator = CatBoostRegressor(depth=depth, iterations=60, learning_rate=.08, l2_leaf_reg=5,
                                  loss_function='RMSE', task_type='CPU', thread_count=1,
                                  random_seed=seed, verbose=False, allow_writing_files=False)
    values = [row['actual'][target] if target != 'elapsed_seconds' else row[target] for row in rows]
    if len(set(values)) == 1:
        return float(values[0])
    estimator.fit([row['features'] for row in rows],[math.log1p(v) for v in values])
    return estimator


def cost_predict(estimator,x):
    if isinstance(estimator,float):
        return estimator
    return max(0.0,math.expm1(float(estimator.predict([list(x)])[0])))


class PilotModel:
    def __init__(self, weights, costs, metadata):
        self.weights, self.costs, self.metadata = weights,costs,metadata

    def predict(self,x,beta=0):
        if len(x) != len(FEATURE_NAMES):
            raise ValueError('feature contract mismatch')
        predictions = {name:cost_predict(estimator,x) for name,estimator in self.costs.items()}
        return {'reward':reward_predict(self.weights,x),'uncertainty':0.0,
                'costs':{name:{'median':value,'upper':value} for name,value in predictions.items()}}

    @classmethod
    def load(cls, path):
        from catboost import CatBoostRegressor
        path = Path(path)
        raw = json.loads((path/'model.json').read_text())
        if raw['schema'] != 1 or tuple(raw['features']) != FEATURE_NAMES:
            raise ValueError('incompatible model artifact')
        costs = {}
        for target in COSTS:
            record = raw['cost_models'][target]
            if 'constant' in record:
                costs[target] = float(record['constant'])
            else:
                artifact = path/(target+'.cbm')
                if hashlib.sha256(artifact.read_bytes()).hexdigest() != record['sha256']:
                    raise ValueError('cost model artifact digest mismatch')
                model = CatBoostRegressor()
                model.load_model(str(artifact))
                costs[target] = model
        return cls(raw['weights'],costs,raw)


def fit_pilot(manifest, collection, output):
    validate_manifest(manifest)
    if collection['manifest_sha256'] != manifest['sha256'] or tuple(collection['feature_names']) != FEATURE_NAMES:
        raise ValueError('collection does not match manifest and encoder')
    expected = {action_for(row,mode).id:(row,mode) for row in manifest['split']['rows']
                if row['split'] != 'test' for mode in MODES}
    records = collection['records']
    ids = [row['id'] for row in records]
    if len(ids) != len(set(ids)) or set(ids) != set(expected):
        raise ValueError('incomplete, duplicate, or held-out records in development collection')
    invalid_tasks = {
        record['task_id'] for record in records
        if not record['paired_initial_state_matches'] or record['success'] is None
        or record['ledger']['pending_reservations']
    }
    admitted, exclusions = [], []
    for record in records:
        row,mode = expected[record['id']]
        action = action_for(row,mode)
        if (record['split'],record['app_group_id'],record['task_id'],record['configuration'],tuple(record['features'])) != (
                row['split'],row['app_group_id'],row['id'],mode,action.features):
            raise ValueError('record does not match frozen input')
        if row['id'] in invalid_tasks:
            exclusions.append(record['id'])
            continue
        if (record['success'] != int(record['outcome']=='task_completed')
                or record['outcome'] not in ('task_completed','task_incomplete')
                or record['actual'] != record['ledger']['spent']
                or set(record['actual']) != set(action.upper)
                or any(not math.isfinite(v) or not 0 <= v <= action.upper[k] for k,v in record['actual'].items())
                or not math.isfinite(record['elapsed_seconds']) or record['elapsed_seconds'] < 0):
            raise ValueError('invalid labels or accounting in collection')
        admitted.append(record)
    train = [row for row in admitted if row['split']=='train']
    validation = [row for row in admitted if row['split']=='validation']
    if len({r['success'] for r in train}) != 2 or not validation:
        raise ValueError('training needs both outcomes and a disjoint validation partition')
    folds = splits(train)
    seed = manifest['config']['seed']
    reward_scores = []
    for prior in (.1,1.,10.):
        rows, errors = [], []
        for source,held in folds:
            weights = reward_fit(source,prior)
            rows.extend(held)
            errors.extend((reward_predict(weights,r['features'])-r['success'])**2 for r in held)
        reward_scores.append({'prior':prior,'group_mean_brier':group_metric(rows,errors)})
    prior = min(reward_scores,key=lambda x:x['group_mean_brier'])['prior']
    weights = reward_fit(train,prior)
    costs = {}; cost_scores = {}
    for target in COSTS:
        candidates = []
        for depth in (2,3):
            rows, errors = [], []
            for source,held in folds:
                estimator = cost_fit(source,target,depth,seed)
                rows.extend(held)
                errors.extend(abs(cost_predict(estimator,r['features'])-(r['actual'][target] if target != 'elapsed_seconds' else r[target])) for r in held)
            candidates.append({'depth':depth,'group_mean_mae':group_metric(rows,errors)})
        depth = min(candidates,key=lambda x:x['group_mean_mae'])['depth']
        cost_scores[target] = {'selected_depth':depth,'candidates':candidates}
        costs[target] = cost_fit(train,target,depth,seed)
    out = Path(output); out.mkdir(parents=True,exist_ok=False)
    models = {}
    for name,estimator in costs.items():
        if isinstance(estimator,float):
            models[name] = {'constant':estimator}
        else:
            artifact_path = out/(name+'.cbm')
            estimator.save_model(str(artifact_path))
            models[name] = {'artifact':artifact_path.name,
                            'sha256':hashlib.sha256(artifact_path.read_bytes()).hexdigest()}
    validation_scores = {'group_mean_brier':group_metric(validation,[(reward_predict(weights,r['features'])-r['success'])**2 for r in validation])}
    for name,estimator in costs.items():
        validation_scores[name+'_mae'] = group_metric(validation,[abs(cost_predict(estimator,r['features'])-(r['actual'][name] if name != 'elapsed_seconds' else r[name])) for r in validation])
    artifact = {'schema':1,'manifest_sha256':manifest['sha256'],'data_sha256':digest(collection),
                'scope':'functional-task development pilot','features':FEATURE_NAMES,'weights':weights,
                'policy_parameters':{'beta':0,'prices':{'agent_calls':.045,'device_steps':.018}},
                'cost_models':models,'train_groups':sorted({r['app_group_id'] for r in train}),
                'validation_groups':sorted({r['app_group_id'] for r in validation}),
                'train_rows':len(train),'validation_rows':len(validation),'excluded_ids':exclusions,
                'cv':{'held_out_groups':[f[1][0]['app_group_id'] for f in folds],
                      'reward_candidates':reward_scores,'selected_prior':prior,'cost_candidates':cost_scores},
                'validation_metrics':validation_scores,'seed':seed,'thread_count':1}
    write(out/'model.json',artifact)
    return artifact
