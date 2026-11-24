"""Unified real-device campaign entry point (fixed-order baseline)."""
import hashlib
import json
from pathlib import Path
from .android import Android
from .campaign import Campaign
from .decision import Action, FixedOrderPlanner
from .gui_team import GUIEnvironment, InstructionProposer, PolicyCritic, CrashReplayVerifier
from .ledger import Ledger
from .runtime import Journal, TeamExecutor


def run_gui_team(serial,package,activity,instruction,output,*,max_steps=4,known_signatures=None,result_id=None,expected_text=None):
    if type(max_steps) is not int or not 1 <= max_steps <= 20:
        raise ValueError('max_steps must be 1..20')
    if (result_id is None) != (expected_text is None): raise ValueError('result_id and expected_text must be specified together')
    out = Path(output); out.mkdir(parents=True,exist_ok=False)
    journal = Journal(out/'events.jsonl')
    quotas = {'proposer':{'model_calls':0,'device_steps':0},
              'critic':{'model_calls':0,'device_steps':0},
              'execute':{'model_calls':max_steps,'device_steps':1+3*max_steps},
              'verify':{'model_calls':0,'device_steps':2*(max_steps+2)+(3 if result_id else 0)}}
    total = {k:sum(q[k] for q in quotas.values()) for k in ('model_calls','device_steps')}
    ledger = Ledger(out/'budget.sqlite',total)
    device = Android(serial)
    environment = GUIEnvironment(device,package,activity,out/'execution',journal,max_steps=max_steps)
    verifier = CrashReplayVerifier(device,package,activity,out/'verification',known_signatures=known_signatures,outcome_probe=(lambda:device.ui_text(result_id)==[expected_text]) if result_id else None)
    executor = TeamExecutor(ledger,journal,environment,InstructionProposer(instruction),PolicyCritic(),verifier)
    action = Action('gui-team','gui-task','instruction-critic-gui-replay',(1,),total,workers=(serial,))
    # Budget and selection use the same contract as learned planning; no model
    # trained on synthetic data is used to choose real-device actions here.
    try:
        report = Campaign(FixedOrderPlanner(),executor).run([action],{action.id:quotas},initial_states={action.task:'clean-app-data'},split='test',seed=0,max_trials=1)
        report['execution_status'] = 'completed'
    except Exception as error:
        report = {'execution_status':'unresolved','error_type':type(error).__name__}
        journal.append('campaign_failed',error_type=type(error).__name__)
    report.update(schema=1,scope='real GUI integration acceptance; not benchmark effectiveness',
                  policy='fixed-order',role_implementations={'proposer':'deterministic instruction','critic':'structural policy check','execute':'GUI-Plus','verify':'independent ADB replay'},
                  budget=ledger.audit(),package=package,serial=serial)
    artifact_hashes = {str(p.relative_to(out)):hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in out.rglob('*') if p.is_file() and p.suffix not in ('.sqlite','-wal','-shm')}
    report['artifact_sha256'] = artifact_hashes
    (out/'report.json').write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
    return report
