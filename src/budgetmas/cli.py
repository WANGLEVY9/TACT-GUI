import argparse
import json
from pathlib import Path
from .ledger import Ledger
from .scheduling import Candidate, LinearValue, Scheduler, train
from .data import split_manifest

def main():
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest='command',required=True)
    demo=sub.add_parser('smoke'); demo.add_argument('--out',required=True)
    split=sub.add_parser('split'); split.add_argument('input'); split.add_argument('output')
    fit=sub.add_parser('train'); fit.add_argument('input'); fit.add_argument('output'); fit.add_argument('--dimension',type=int,required=True)
    team=sub.add_parser('team-fixture'); team.add_argument('--out',required=True)
    joint=sub.add_parser('fit-joint'); joint.add_argument('train'); joint.add_argument('validation'); joint.add_argument('output'); joint.add_argument('--dimension',type=int,required=True); joint.add_argument('--resources',nargs='+',required=True)
    gui=sub.add_parser('gui-android'); gui.add_argument('--serial',required=True); gui.add_argument('--package',required=True); gui.add_argument('--activity',required=True); gui.add_argument('--instruction',required=True); gui.add_argument('--out',required=True); gui.add_argument('--max-steps',type=int,default=8); gui.add_argument('--model-calls',type=int,default=8); gui.add_argument('--device-steps',type=int,default=60)
    audit=sub.add_parser('audit-budget'); audit.add_argument('database')
    unified=sub.add_parser('gui-team'); unified.add_argument('--serial',required=True); unified.add_argument('--package',required=True); unified.add_argument('--activity',required=True); unified.add_argument('--instruction',required=True); unified.add_argument('--out',required=True); unified.add_argument('--max-steps',type=int,default=4)
    unified.add_argument('--result-id'); unified.add_argument('--expected-text')
    pilot=sub.add_parser('android-pilot')
    stages=pilot.add_subparsers(dest='stage',required=True)
    freeze_pilot=stages.add_parser('freeze')
    freeze_pilot.add_argument('config')
    freeze_pilot.add_argument('manifest')
    freeze_pilot.add_argument('--serial',required=True)
    collect_pilot=stages.add_parser('collect')
    collect_pilot.add_argument('manifest')
    collect_pilot.add_argument('--serial',required=True)
    collect_pilot.add_argument('--out',required=True)
    fit_pilot=stages.add_parser('fit')
    fit_pilot.add_argument('manifest')
    fit_pilot.add_argument('collection')
    fit_pilot.add_argument('--out',required=True)
    eval_pilot=stages.add_parser('evaluate')
    eval_pilot.add_argument('manifest')
    eval_pilot.add_argument('model')
    eval_pilot.add_argument('--serial',required=True)
    eval_pilot.add_argument('--out',required=True)
    args=p.parse_args()
    if args.command=='android-pilot':
        from .pilot import cli
        cli(args)
        return
    if args.command=='gui-team':
        from .live import run_gui_team
        report=run_gui_team(args.serial,args.package,args.activity,args.instruction,args.out,max_steps=args.max_steps,result_id=args.result_id,expected_text=args.expected_text)
        print(json.dumps({'execution_status':report['execution_status'],'budget':report['budget']}))
        if report['execution_status']!='completed': raise SystemExit(2)
        return
    if args.command=='audit-budget':
        if not Path(args.database).is_file(): raise ValueError('budget database does not exist')
        print(json.dumps(Ledger(args.database).audit(),indent=2)); return
    if args.command=='gui-android':
        from .android import Android
        from .providers import GUIPlus
        from .runtime import Journal
        from .gui_session import GUISession
        out=Path(args.out); out.mkdir(parents=True,exist_ok=False)
        ledger=Ledger(out/'budget.sqlite',{'model_calls':args.model_calls,'device_steps':args.device_steps})
        report=GUISession(Android(args.serial),GUIPlus(ledger),ledger,Journal(out/'events.jsonl')).run(args.package,args.activity,args.instruction,out/'session',max_steps=args.max_steps)
        print(json.dumps({'status':report['status'],'error_type':report['error_type'],'available':report['available']}))
        if report['status'] in ('unresolved','budget_exhausted'): raise SystemExit(2)
        return
    if args.command=='fit-joint':
        from .training import fit
        read=lambda p:[json.loads(line) for line in Path(p).read_text().splitlines() if line.strip()]
        model,report=fit(read(args.train),read(args.validation),args.dimension,args.resources)
        model.save(args.output); print(json.dumps(report)); return
    if args.command=='team-fixture':
        from .runtime import TeamExecutor,Journal
        from .fixture import Proposer,Critic,Cart,DifferentialVerifier
        from .decision import Action
        out=Path(args.out); out.mkdir(parents=True,exist_ok=False)
        ledger=Ledger(out/'budget.sqlite',{'operations':8})
        runner=TeamExecutor(ledger,Journal(out/'events.jsonl'),Cart(),Proposer(),Critic(),DifferentialVerifier())
        action=Action('fixture','cart','proposer-critic-replay',(1,),{'operations':4})
        quotas={r:{'operations':1} for r in ('proposer','critic','execute','verify')}
        results=[runner.run(action,quotas,initial_state='empty-cart',seed=i,split='train') for i in range(2)]
        report={'scope':'local injected-fault integration fixture; not benchmark','results':results,'available':ledger.available()}
        (out/'report.json').write_text(json.dumps(report,indent=2)+'\n'); print(json.dumps(report)); return
    if args.command=='split':
        result=split_manifest(json.loads(Path(args.input).read_text()))
        with open(args.output,'x') as f: json.dump(result,f,indent=2)
    elif args.command=='train':
        rows=[json.loads(line) for line in Path(args.input).read_text().splitlines() if line.strip()]
        train(rows,args.dimension).save(args.output)
    else:
        out=Path(args.out); out.mkdir(parents=True,exist_ok=False)
        ledger=Ledger(out/'budget.sqlite',{'actions':5})
        policy=Scheduler(LinearValue(2),exploration=1)
        events=[]
        for i in range(8):
            candidates=[Candidate('single',(1,0),{'actions':1}),Candidate('pair',(1,1),{'actions':2})]
            choice=policy.choose(candidates,ledger.available())
            if choice is None: break
            ledger.reserve(str(i),choice.upper)
            # Deterministic fixture only; no real app or discovered defect.
            ledger.settle(str(i),choice.upper)
            events.append({'step':i,'action':choice.id,'resources':choice.upper,'reward':None})
        report={'scope':'synthetic accounting and scheduling smoke only','events':events,'available':ledger.available(),'real_benchmark_runs':0}
        (out/'report.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report))

if __name__=='__main__': main()
