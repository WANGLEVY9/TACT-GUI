"""Project-group partitioning, with an explicit immutable split manifest."""
import hashlib
import json

def split_manifest(rows, seed=20261123):
    if not rows: raise ValueError("empty manifest")
    ids=[r['id'] for r in rows]
    if len(set(ids)) != len(ids): raise ValueError("duplicate sample id")
    groups=sorted({r['app_group_id'] for r in rows}, key=lambda g: hashlib.sha256(f"{seed}:{g}".encode()).hexdigest())
    if len(groups)<5: raise ValueError("at least five independent application groups required")
    n=len(groups); train=int(n*.6); val=max(1,int(n*.2))
    mapping={g:('train' if i<train else 'validation' if i<train+val else 'test') for i,g in enumerate(groups)}
    # Identical assets across upstream groups indicate unresolved lineage.
    assets={}
    for r in rows:
        for field in ('apk_sha256','source_sha256','bug_lineage_id'):
            if r.get(field):
                key=(field,r[field])
                if key in assets and assets[key]!=r['app_group_id']: raise ValueError("cross-group duplicate asset")
                assets[key]=r['app_group_id']
    result=[dict(r,split=mapping[r['app_group_id']]) for r in rows]
    digest=hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest()
    return {'seed':seed,'sha256':digest,'rows':result}
