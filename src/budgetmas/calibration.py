"""Finite-sample split conformal residual calibration for exchangeable data.

Calibration samples must be independent application groups (or a defined group
aggregate). Row-wise dependence invalidates the usual coverage interpretation.
"""
import math


def binomial_upper(failures, trials, delta):
    """One-sided exact upper endpoint for independent Bernoulli groups."""
    if (type(failures) is not int or type(trials) is not int or not 0 <= failures <= trials
            or not 0 < delta < 1):
        raise ValueError('invalid binomial calibration sample')
    if trials == 0:
        return None
    if failures == trials:
        return 1.
    if failures == 0:
        return -math.expm1(math.log(delta)/trials)
    terms = [math.lgamma(trials+1)-math.lgamma(i+1)-math.lgamma(trials-i+1)
             for i in range(failures+1)]
    lo,hi = 0.,1.
    for _ in range(52):
        mid = (lo+hi)/2
        tail = sum(math.exp(base+i*math.log(mid)+(trials-i)*math.log1p(-mid))
                   for i,base in enumerate(terms))
        if tail > delta:
            lo = mid
        else:
            hi = mid
    return hi


def residual_radius(residuals, alpha=0.1):
    if not residuals or not 0 < alpha < 1 or any(not math.isfinite(r) or r < 0 for r in residuals):
        raise ValueError('invalid calibration sample')
    values = sorted(residuals)
    rank = math.ceil((len(values)+1)*(1-alpha))
    return values[rank-1] if rank <= len(values) else math.inf


class CalibratedModel:
    def __init__(self, base, reward_radius, cost_radii):
        self.base = base
        if reward_radius < 0 or math.isnan(reward_radius) or any(r < 0 or math.isnan(r) for r in cost_radii.values()):
            raise ValueError('invalid calibration radius')
        self.reward_radius, self.cost_radii = reward_radius,cost_radii

    def predict(self,x,beta=1):
        pred = self.base.predict(x,beta)
        if set(self.cost_radii) != set(pred['costs']):
            raise ValueError('calibration resources mismatch')
        # Keep epistemic sd and explicit calibration interval separate.
        pred['reward_lower'] = pred['reward']-self.reward_radius
        pred['calibration_radius'] = self.reward_radius
        for key,radius in self.cost_radii.items():
            pred['costs'][key]['upper'] = pred['costs'][key]['median']+radius
        return pred


def calibrate_by_application(model, rows, alpha=0.1):
    """Use a reserved validation subpartition, never tuning or training groups."""
    if not rows:
        raise ValueError('nonempty calibration partition required')
    seen_groups = set(model.metadata.get('train_groups',[])) | set(model.metadata.get('validation_groups',[]))
    groups = {}
    ids = set()
    for row in rows:
        if row['split'] != 'calibration' or row['app_group_id'] in seen_groups:
            raise ValueError('calibration overlaps training/tuning or wrong partition')
        if row['id'] in ids or row['verification_status'] not in ('confirmed','not_confirmed'):
            raise ValueError('duplicate or unresolved calibration outcome')
        ids.add(row['id'])
        if not math.isfinite(row['verified_reward']) or set(row['actual']) != set(model.costs):
            raise ValueError('invalid calibration label schema')
        if any(not math.isfinite(v) or v < 0 for v in row['actual'].values()):
            raise ValueError('invalid calibration cost')
        pred = model.predict(row['features'])
        residuals = {'reward':abs(row['verified_reward']-pred['reward'])}
        residuals.update({k:max(0,row['actual'][k]-pred['costs'][k]['median']) for k in model.costs})
        group = groups.setdefault(row['app_group_id'],dict.fromkeys(residuals,0.0))
        for k,value in residuals.items():
            group[k] = max(group[k],value)
    radii = {key:residual_radius([v[key] for v in groups.values()],alpha) for key in ['reward',*model.costs]}
    return CalibratedModel(model,radii['reward'],{k:radii[k] for k in model.costs}), {
        'alpha':alpha,'application_groups':len(groups),'aggregate':'maximum residual per application',
        'informative':all(math.isfinite(v) for v in radii.values())}
