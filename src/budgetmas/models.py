"""Full-covariance Bayesian linear heads, using stable Cholesky solves.

Posterior uncertainty is epistemic under the linear Gaussian model. It is not
an empirical coverage guarantee and does not identify counterfactual effects.
"""
import json
import math
from pathlib import Path


def vector(x, dimension):
    x = tuple(float(v) for v in x)
    if len(x) != dimension or not all(math.isfinite(v) for v in x):
        raise ValueError('invalid feature vector')
    return x


def solve_spd(matrix, rhs):
    n = len(rhs)
    lower = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            value = matrix[i][j] - sum(lower[i][k] * lower[j][k] for k in range(j))
            lower[i][j] = math.sqrt(value) if i == j else value / lower[j][j]
    y = [0.0] * n
    for i in range(n):
        y[i] = (rhs[i] - sum(lower[i][j] * y[j] for j in range(i))) / lower[i][i]
    result = [0.0] * n
    for i in reversed(range(n)):
        result[i] = (y[i] - sum(lower[j][i] * result[j] for j in range(i + 1, n))) / lower[i][i]
    return result


class BayesianHead:
    def __init__(self, dimension, prior=1.0, noise=1.0):
        if type(dimension) is not int or dimension < 1 or not math.isfinite(prior) or prior <= 0 or not math.isfinite(noise) or noise <= 0:
            raise ValueError('invalid posterior parameters')
        self.dimension, self.prior, self.noise = dimension, prior, noise
        self.precision = [[prior if i == j else 0.0 for j in range(dimension)] for i in range(dimension)]
        self.rhs = [0.0] * dimension
        self.samples = 0

    def update(self, x, target):
        x = vector(x, self.dimension)
        if not math.isfinite(target):
            raise ValueError('invalid supervision')
        for i in range(self.dimension):
            self.rhs[i] += x[i] * target / self.noise
            for j in range(self.dimension):
                self.precision[i][j] += x[i] * x[j] / self.noise
        self.samples += 1

    def predict(self, x):
        x = vector(x, self.dimension)
        weights = solve_spd(self.precision, self.rhs)
        inverse_x = solve_spd(self.precision, x)
        return sum(a*b for a,b in zip(weights,x)), math.sqrt(max(0, sum(a*b for a,b in zip(x,inverse_x))))


class JointModel:
    """Reward head and log1p resource heads sharing action-conditioned features."""
    def __init__(self, dimension, resources):
        if not resources or 'reward' in resources or len(set(resources)) != len(resources):
            raise ValueError('unique resources required')
        self.reward = BayesianHead(dimension)
        self.costs = {key: BayesianHead(dimension) for key in resources}
        self.metadata = {}

    def update(self, x, reward, actual):
        if set(actual) != set(self.costs) or any(not math.isfinite(v) or v < 0 for v in actual.values()):
            raise ValueError('invalid resource labels')
        vector(x, self.reward.dimension)
        if not math.isfinite(reward):
            raise ValueError('invalid reward')
        self.reward.update(x, reward)
        for key, value in actual.items():
            self.costs[key].update(x, math.log1p(value))

    def predict(self, x, beta=1.0):
        mean, uncertainty = self.reward.predict(x)
        costs = {}
        for key, head in self.costs.items():
            location, sd = head.predict(x)
            # Posterior log-cost interval, not a hard execution bound.
            costs[key] = {'median': math.expm1(max(0, min(40, location))),
                          'upper': math.expm1(max(0, min(40, location + beta*sd)))}
        return {'reward': mean, 'uncertainty': uncertainty, 'costs': costs}

    def save(self, path):
        artifact = {'schema': 1, 'dimension': self.reward.dimension,
                    'resources': list(self.costs), 'metadata': self.metadata,
                    'heads': {k: vars(v) for k,v in {'reward': self.reward, **self.costs}.items()}}
        # Resource name "reward" would alias the reward head: reject at construction.
        with Path(path).open('x') as stream:
            json.dump(artifact, stream, allow_nan=False, indent=2)

    @classmethod
    def load(cls, path):
        raw = json.loads(Path(path).read_text())
        if raw['schema'] != 1:
            raise ValueError('unsupported checkpoint')
        result = cls(raw['dimension'], raw['resources'])
        for key, head in {'reward': result.reward, **result.costs}.items():
            state = raw['heads'][key]
            n = head.dimension
            matrix = state['precision']; rhs = state['rhs']
            if len(matrix) != n or any(len(row) != n for row in matrix) or len(rhs) != n:
                raise ValueError('invalid checkpoint shape')
            if any(not math.isfinite(v) for row in matrix for v in row) or any(not math.isfinite(v) for v in rhs):
                raise ValueError('nonfinite checkpoint')
            if any(abs(matrix[i][j]-matrix[j][i]) > 1e-9 for i in range(n) for j in range(n)):
                raise ValueError('asymmetric precision')
            solve_spd(matrix, rhs)
            head.precision, head.rhs, head.samples = matrix, rhs, state['samples']
        result.metadata = raw['metadata']
        return result
