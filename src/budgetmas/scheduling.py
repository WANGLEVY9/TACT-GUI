"""Action-conditioned Bayesian linear UCB with resource shadow prices.

Diagonal posterior is a tractable baseline, not a causal effect estimator.
"""
from dataclasses import dataclass
import math
import json

@dataclass(frozen=True)
class Candidate:
    id: str
    features: tuple
    upper: dict
    dependencies: tuple=()

class LinearValue:
    def __init__(self, dimension, prior=1.0):
        if dimension<1 or prior<=0: raise ValueError("invalid prior")
        self.precision=[prior]*dimension
        self.xy=[0.0]*dimension
    def predict(self,x):
        if len(x)!=len(self.xy) or any(not math.isfinite(v) for v in x): raise ValueError("invalid features")
        return sum(a*b/p for a,b,p in zip(x,self.xy,self.precision)), math.sqrt(sum(a*a/p for a,p in zip(x,self.precision)))
    def update(self,x,reward):
        self.predict(x)
        if not math.isfinite(reward): raise ValueError("invalid label")
        for i,a in enumerate(x): self.precision[i]+=a*a; self.xy[i]+=a*reward
    def save(self,path):
        with open(path,'w') as f: json.dump({'schema':1,'precision':self.precision,'xy':self.xy},f)

class Scheduler:
    def __init__(self, model, exploration=0.0, prices=None):
        self.model=model; self.exploration=exploration; self.prices=prices or {}
    def choose(self,candidates,available,completed=()):
        scored=[]
        for c in candidates:
            if not set(c.dependencies)<=set(completed): continue
            if set(c.upper)!=set(available) or any(not math.isfinite(v) or v<0 or v>available[k] for k,v in c.upper.items()): continue
            mean,sd=self.model.predict(c.features)
            score=mean+self.exploration*sd-sum(self.prices.get(k,0)*v for k,v in c.upper.items())
            scored.append((score,c.id,c))
        return max(scored,key=lambda t:(t[0],t[1]))[2] if scored else None

def train(rows, dimension):
    model=LinearValue(dimension)
    for row in rows:
        if row['split']!='train': raise ValueError("training input contains held-out data")
        model.update(row['features'],row['verified_reward'])
    return model
