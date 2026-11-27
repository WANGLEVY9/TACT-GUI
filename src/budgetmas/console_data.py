"""Read local mechanism artifacts without granting arbitrary filesystem access."""
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlencode

from .android import parse_action
from .mechanism import call_schedule, registered_arms

ARTIFACTS = {'report.json': 'Run report', 'protocol.json': 'Frozen protocol',
             'audit-summary.json': 'Audit summary', 'routing-summary.json': 'Routing contrasts',
             'console.log': 'Process output'}
RELATED = {'primary-interaction.json': 'Budget interaction analysis',
           'recovery/sensitivity.json': 'Recovery sensitivity',
           'development-fit-audited.json': 'Cross-fitted model and replay',
           'trajectory-audit/report.json': 'Trajectory audit'}
NAME = re.compile(r'[a-z0-9][a-z0-9-]*')
FRAME = re.compile(r'screen-(\d+)\.png')


def json_file(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def confined(root, path):
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError('path is outside the artifact workspace')
    return resolved


def timestamp(path):
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


def read_budget(path):
    """Read a consistent SQLite snapshot without taking a writer lock."""
    try:
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=.3)) as db:
            db.execute('BEGIN')
            limits = dict(db.execute('SELECT resource,amount FROM limits'))
            spent, reserved = (dict.fromkeys(limits,0) for _ in range(2))
            pending = []
            for rid, upper, actual, status in db.execute('SELECT id,upper_json,actual_json,status FROM reservations'):
                dest = spent if status=='settled' else reserved
                for key,amount in json.loads(actual if status=='settled' else upper).items():
                    dest[key] += amount
                if status!='settled':
                    pending.append(rid)
            return dict(limits=limits,spent=spent,reserved=reserved,pending_reservations=pending,
                        available={k:limits[k]-spent[k]-reserved[k] for k in limits})
    except (OSError,ValueError,sqlite3.Error):
        return {}


def archive_paths(root):
    """Only protocol-backed mechanism runs, including incomplete durable runs."""
    def visit(directory, depth):
        if depth > 3:
            return
        protocol = directory/'protocol.json'
        if protocol.is_file() and not protocol.is_symlink():
            proto = json_file(protocol)
            if isinstance(proto,dict) and isinstance(proto.get('checkpoints'),list):
                yield directory
                return
        for child in sorted(directory.iterdir()):
            if child.is_dir() and not child.is_symlink() and NAME.fullmatch(child.name):
                yield from visit(child,depth+1)
    if root.is_dir():
        yield from visit(root,0)


def run_directory(root, run_id):
    if not isinstance(run_id,str) or not 1<=len(run_id.split('/'))<=3 or not all(NAME.fullmatch(x) for x in run_id.split('/')):
        raise ValueError('invalid run identifier')
    path = confined(root,root/run_id)
    protocol = json_file(confined(root,path/'protocol.json'))
    if not isinstance(protocol,dict) or not isinstance(protocol.get('checkpoints'),list):
        raise FileNotFoundError('run protocol not found')
    return path


def archive_index(root):
    rows=[]
    for folder in archive_paths(root):
        report=json_file(confined(root,folder/'report.json'))
        protocol=folder/'protocol.json'
        updated=folder/'report.json' if report else protocol
        launch=json_file(confined(root,folder.parent/'launch.json')) if folder.name=='run' else None
        rows.append({'id':folder.relative_to(root).as_posix(),
                     'name':(launch or {}).get('title') or ' / '.join(folder.relative_to(root).parts),
                     'updated':timestamp(updated),'complete':bool(report)})
    return sorted(rows,key=lambda row:row['updated'],reverse=True)


def count_frames(folder):
    return sum(1 for path in folder.glob('screen-*.png') if FRAME.fullmatch(path.name))


def artifacts(root, folder):
    sources = {label:folder/name for name,label in ARTIFACTS.items() if name!='console.log'}
    if folder.name=='run':
        sources[ARTIFACTS['console.log']]=folder.parent/'console.log'
    for name,label in RELATED.items():
        sources[label]=folder.parent/name
    return [{'label':label,'path':path.relative_to(root).as_posix(),
             'bytes':path.stat().st_size,'name':path.name}
            for label,path in sources.items()
            if path.is_file() and path.resolve().is_relative_to(root.resolve())]


def telemetry(root, folder):
    records={}
    path=confined(root,folder/'events.jsonl')
    try:
        lines=path.read_text().splitlines()
    except OSError:
        return records
    for line in lines:
        try:
            row=json.loads(line)
            event=row['event']
            payload=row['data']
            if event in ('model_response','action_applied','progress_measured','intent_decision'):
                records.setdefault(payload['index'],{})[event]=payload
        except (ValueError,KeyError,TypeError):
            continue  # A live writer can leave a partial trailing line.
    return records


def run_detail(root, run_id, active=False):
    folder=run_directory(root,run_id)
    proto=json_file(folder/'protocol.json')
    report=json_file(confined(root,folder/'report.json')) or {}
    roles=registered_arms(proto)
    cases=[]
    totals=Counter()
    calls=Counter()
    role_counts={role:Counter() for role in roles}
    for case in proto['checkpoints']:
        arms=[]
        for role in roles:
            arm_folder=confined(root,folder/case['id']/role)
            arm=json_file(confined(root,arm_folder/'report.json'))
            state=('verified' if arm['success']==1 else 'incomplete' if arm['success']==0 else 'unresolved') if arm else (
                ('running' if arm_folder.is_dir() else 'queued') if active else
                ('interrupted' if arm_folder.is_dir() else 'unrecorded'))
            if state=='incomplete':
                decision=arm.get('intent',{}).get('decision')
                state={'clarify':'clarification','defer':'deferred'}.get(decision,state)
            frames=count_frames(arm_folder)
            calls['frames']+=frames
            totals[state]+=1
            role_counts[role][state]+=1
            tokens=0
            if arm:
                calls['responses']+=len(arm.get('calls',[]))
                tokens=sum(c['usage']['total_tokens'] for c in arm.get('calls',[]) if c.get('usage_complete'))
                calls['tokens']+=tokens
            elif arm_folder.is_dir():
                live=[record['model_response'] for record in telemetry(root,arm_folder).values()
                      if 'model_response' in record]
                calls['responses']+=len(live)
                tokens=sum(record.get('tokens') or 0 for record in live)
                calls['tokens']+=tokens
            arms.append({'role':role,'state':state,'frames':frames,'tokens':tokens,
                         'lost_goal':(arm or {}).get('progress',{}).get('lost_goal')})
        cases.append({'id':case['id'],'group':case['app_group'],'slots':case.get('slots',2),
                      'goal':case['goal'],'arms':arms})
    return {'id':run_id,'created':timestamp(folder/'protocol.json'),'complete':bool(report),'active':active,
            'counts':dict(totals),'activity':dict(calls),'roles':role_counts,'cases':cases,
            'budget':report.get('budget') or read_budget(confined(root,folder/'budget.sqlite')),
            'artifacts':artifacts(root,folder)}


def proposed(text):
    try:
        return parse_action(text)
    except (KeyError, TypeError, ValueError):
        return None


def trace(root, run_id, case_id, role, active=False):
    folder=run_directory(root,run_id)
    protocol=json_file(folder/'protocol.json')
    case=next((case for case in protocol['checkpoints'] if case['id']==case_id),None)
    if case is None or role not in registered_arms(protocol):
        raise FileNotFoundError('unknown case or arm')
    arm_folder=confined(root,folder/case_id/role)
    report=json_file(confined(root,arm_folder/'report.json'))
    data=report or {}
    actions=data.get('actions',[])
    calls={call.get('frame',i):call for i,call in enumerate(data.get('calls',[]))}
    events=telemetry(root,arm_folder)
    schedule=call_schedule(role,case.get('slots',2),protocol.get('variants'))
    planned=[]
    action_index=0
    for i,step_role in enumerate(schedule):
        picture=confined(root,arm_folder/f'screen-{i}.png')
        if not picture.is_file():
            continue
        output=arm_folder/f'model-{i}.txt'
        text=confined(root,output).read_text(errors='replace')[:12000] if output.is_file() else ''
        event=events.get(i,{})
        live_action=event.get('action_applied',{})
        live_response=event.get('model_response',{})
        call=calls.get(i,{})
        step_role=call.get('role',live_response.get('role',step_role))
        finish=call.get('finish_reason',live_response.get('finish_reason'))
        action=proposed(text) if step_role=='execute' and text and finish!='length' else None
        executed=bool(live_action or (action and action_index<len(actions) and actions[action_index]==action))
        after=None
        if executed:
            action_index=live_action.get('action_index',action_index+1)
            progress=data.get('progress',{})
            values=progress.get('observed_after_actions',[])
            if progress.get('stable') and 0<action_index<=len(values):
                after=values[action_index-1]
        planned.append({'index':i,'role':step_role,'action':live_action.get('action',action),
                        'executed':executed or bool(live_action),
                        'action_index':live_action.get('action_index',action_index if executed else None),
                        'goal_after':after,'live_goal':event.get('progress_measured',{}).get('goal_after'),
                        'text':text,'tokens':(call.get('usage') or {}).get('total_tokens',live_response.get('tokens')),
                        'model':call.get('returned_model',live_response.get('model')),
                        'finish_reason':finish,
                        'max_tokens':call.get('max_tokens',live_response.get('max_tokens')),
                        'input_view':call.get('input_view',live_response.get('input_view')),
                        'estimated_list_cny':call.get('estimated_list_cny',live_response.get('estimated_list_cny')),
                        'intent_decision':event.get('intent_decision'),
                        'image':'/api/image?'+urlencode({
                            'run':run_id,'case':case_id,'role':role,'frame':i})})
    return {'case':case_id,'group':case['app_group'],'goal':case['goal'],'role':role,
            'slots':case.get('slots',2),'frames':planned,'complete':report is not None,'active':active and report is None,
            'success':data.get('success'),'verification':data.get('verification'),
            'progress':data.get('progress'),'intent':data.get('intent'),'elapsed_seconds':data.get('elapsed_seconds'),
            'actual':data.get('actual'),'error':data.get('error_type'),'error_phase':data.get('error_phase')}


def image_path(root, run_id, case_id, role, frame):
    folder=run_directory(root,run_id)
    proto=json_file(folder/'protocol.json')
    case=next((c for c in proto['checkpoints'] if c['id']==case_id),None)
    if case is None or role not in registered_arms(proto):
        raise FileNotFoundError('unknown frame')
    schedule=call_schedule(role,case.get('slots',2),proto.get('variants'))
    if frame not in {str(i) for i in range(len(schedule))}:
        raise FileNotFoundError('unknown frame')
    return confined(root,folder/case_id/role/f'screen-{frame}.png')
