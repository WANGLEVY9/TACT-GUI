"""TACT-GUI local console. Bind only to loopback and launch registered protocols."""
import argparse
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
from urllib.parse import parse_qs, urlsplit
import uuid

from . import console_data as data
from .android import Android
from .mechanism import ARMS, GUARDED_DIRECT, caps
from .pilot import validate_manifest, write

STATIC=Path(__file__).with_name('console_static')
PROFILES={
    'smoke': ('Checkpoint smoke', 'android-mechanism-smoke.json'),
    'development': ('Four-app development', 'android-mechanism-development.json'),
    'crossover': ('Stop × budget crossover', 'android-stop-crossover.json'),
    'web-smoke': ('Web adaptation smoke', 'web-mechanism-smoke.json'),
}
SERIAL=re.compile(r'emulator-[0-9]{4,5}')


def integer(value, minimum, maximum, label):
    if type(value)!=int or not minimum<=value<=maximum:
        raise ValueError(f'{label} must be an integer between {minimum} and {maximum}.')
    return value


class Workspace:
    def __init__(self, root, manifest):
        self.root=Path(root).resolve()
        self.results=self.root/'results'
        self.manifest=manifest.resolve()
        self.jobs={}
        self.lock=threading.RLock()

    def protocols(self):
        for key,(name,file) in PROFILES.items():
            protocol=data.json_file(self.root/'configs'/file)
            if protocol:
                yield key,name,protocol

    def profiles(self):
        return [{'id':key,'name':name,'seed':proto['seed'],
                 'platform':proto.get('platform','android'),
                 'guard':GUARDED_DIRECT in proto.get('arms',()),
                 'progress':any(x.get('measure_progress',False) for x in proto['checkpoints']),
                 'cases':[{k:case[k] for k in ('id','app_group','goal','base_checkpoint') if k in case} |
                          {'slots':case.get('slots',2)} for case in proto['checkpoints']]}
                for key,name,proto in self.protocols()]

    def plan(self, payload):
        if not isinstance(payload,dict):
            raise ValueError('Expected a run configuration.')
        chosen=next(((n,p) for k,n,p in self.protocols() if k==payload.get('profile')),None)
        if not chosen:
            raise ValueError('Select a registered protocol.')
        name,proto=chosen
        ids=payload.get('cases')
        if not isinstance(ids,list) or not ids or any(not isinstance(x,str) for x in ids) or len(set(ids))!=len(ids):
            raise ValueError('Select at least one distinct checkpoint.')
        cases=[dict(case) for case in proto['checkpoints'] if case['id'] in ids]
        if len(cases)!=len(ids):
            raise ValueError('Selection contains an unregistered checkpoint.')
        slots=payload.get('slots')
        if slots is not None and (type(slots) is not int or slots not in (2,3)):
            raise ValueError('Use the registered horizon, two calls or three calls.')
        if any('base_checkpoint' in case for case in cases):
            if slots is not None:
                raise ValueError('Crossover runs must preserve their registered horizons.')
            for base in {case['base_checkpoint'] for case in cases}:
                if {case.get('slots') for case in cases if case['base_checkpoint']==base}!={2,3}:
                    raise ValueError('Select both conditions for each crossover checkpoint.')
        guard=payload.get('guard',False)
        progress=payload.get('progress',False)
        if type(guard) is not bool or type(progress) is not bool:
            raise ValueError('Guard and replay measurement must be boolean.')
        for case in cases:
            if slots is not None:
                case['slots']=slots
            case['measure_progress']=progress
        arms=list(ARMS)+([GUARDED_DIRECT] if guard else [])
        totals={key:len(arms)*sum(caps(case)[key] for case in cases) for key in ('model_calls','device_steps')}
        ceiling=integer(payload.get('call_limit'),1,288,'Model call limit')
        if totals['model_calls']>ceiling:
            raise ValueError(f"The selection reserves {totals['model_calls']} calls, above your limit of {ceiling}.")
        seed=integer(payload.get('seed'),0,2**32-1,'Random seed')
        timeout=integer(payload.get('timeout'),5,90,'ADB timeout')
        serial=payload.get('serial','')
        platform=chosen[1].get('platform','android')
        if platform == 'web' and serial not in ('','fixture'):
            raise ValueError('Web runs use the local fixture backend, not an emulator.')
        if not isinstance(serial,str) or (serial and serial != 'fixture' and not SERIAL.fullmatch(serial)):
            raise ValueError('Select an emulator serial such as emulator-5582.')
        config={'schema':1,'seed':seed,'arms':arms,'checkpoints':cases,'scope':'console development run'}
        if platform == 'web':
            config = {**config, 'platform':'web', 'apps':chosen[1]['apps']}
        return {'title':name,'totals':totals,'serial':serial,'timeout':timeout,
                'model':'gui-plus-2026-02-26','config':config,'call_limit':ceiling,
                'platform':platform,'fixture':platform=='web'}

    def start(self,payload):
        plan=self.plan(payload)
        if payload.get('confirmed') is not True:
            raise ValueError('Confirm the run and its model call limit.')
        if plan['fixture']:
            return self.start_fixture(plan, payload)
        if not plan['serial']:
            raise ValueError('Connect and select a dedicated emulator first.')
        with self.lock:
            if any(proc.poll() is None for proc in self.jobs.values()):
                raise ValueError('A console run is active. Stop it or wait for it to finish.')
            try:
                manifest=data.json_file(self.manifest)
                validate_manifest(manifest)
            except (KeyError,TypeError,ValueError) as error:
                raise ValueError('A valid frozen app manifest is required. Use the setup command in the README.') from error
            try:
                avd=Android(plan['serial'],timeout=5).adb('emu','avd','name').decode().splitlines()[0].strip()
            except (OSError,IndexError,subprocess.SubprocessError) as error:
                raise ValueError('The selected emulator is not responding.') from error
            if avd!='BudgetMASPilot':
                raise ValueError('Select a dedicated BudgetMASPilot emulator.')
            job=uuid.uuid4().hex[:12]
            folder=self.results/'console'/job
            folder.mkdir(parents=True)
            plan['created']=datetime.now(timezone.utc).isoformat()
            write(folder/'launch.json',plan)
            write(folder/'requested.json',plan['config'])
            with (folder/'console.log').open('w') as output:
                args=[sys.executable,'-u','-m','budgetmas.mechanism',str(folder/'requested.json'),
                      str(self.manifest),'--serial',plan['serial'],'--adb-timeout',str(plan['timeout']),
                      '--out',str(folder/'run')]
                env=os.environ.copy()
                module_dir=str(Path(__file__).resolve().parents[1])
                env['PYTHONPATH']=os.pathsep.join(filter(None,(module_dir,env.get('PYTHONPATH'))))
                self.jobs[job]=subprocess.Popen(args,cwd=self.root,stdout=output,stderr=subprocess.STDOUT,
                                                start_new_session=True,env=env)
            return {'job':job,'run':f'console/{job}/run'}

    def start_fixture(self, plan, payload):
        from .mechanism import run, web_protocol
        from .offline import provider_factory

        with self.lock:
            if any(proc.poll() is None for proc in self.jobs.values()):
                raise ValueError('A console run is active. Stop it or wait for it to finish.')
            job = uuid.uuid4().hex[:12]
            folder = self.results / 'console' / job
            folder.mkdir(parents=True)
            plan['created'] = datetime.now(timezone.utc).isoformat()
            write(folder/'launch.json', plan)
            write(folder/'requested.json', plan['config'])
            config = plan['config']
            apps = web_protocol(config)
            with (folder/'console.log').open('w') as output:
                try:
                    run(config, apps, 'fixture', folder/'run', provider_factory=provider_factory,
                        platform='web')
                    output.write('Offline Web fixture run completed.\n')
                except Exception as error:
                    output.write('Offline Web fixture run failed: '+type(error).__name__+'\n')
            return {'job':job,'run':f'console/{job}/run'}

    def is_active(self, run_id):
        with self.lock:
            return any(run_id==f'console/{key}/run' and proc.poll() is None
                       for key,proc in self.jobs.items())

    def stop(self, job):
        with self.lock:
            proc=self.jobs.get(job) if isinstance(job,str) else None
            if proc is None or proc.poll() is not None:
                raise ValueError('Run is no longer active in this console.')
            try:
                os.killpg(proc.pid,signal.SIGTERM)
            except ProcessLookupError:
                proc.poll()
            return {'job':job,'state':'stopping'}

    def snapshot(self):
        with self.lock:
            jobs=[]
            for key,proc in reversed(list(self.jobs.items())):
                folder=self.results/'console'/key
                code=proc.poll()
                log=folder/'console.log'
                with log.open('rb') as stream:
                    stream.seek(max(0,log.stat().st_size-6000))
                    lines=stream.read().decode(errors='replace')
                jobs.append({'id':key,'run':f'console/{key}/run','exit':code,'log':lines})
            return {'runs':data.archive_index(self.results),'jobs':jobs,
                    'manifest_ready':self.manifest.is_file()}

    def close(self):
        with self.lock:
            pending=[key for key,proc in self.jobs.items() if proc.poll() is None]
            for key in pending:
                try:
                    self.stop(key)
                except ValueError:
                    pass


class Handler(BaseHTTPRequestHandler):
    server_version='TACT-Console'
    sys_version=''

    def log_message(self,format,*args):
        # Query strings may include artifact names. Operator UI already shows errors.
        return

    def permitted(self):
        return self.headers.get('Host')==f'127.0.0.1:{self.server.server_port}'

    def send(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',
                         "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def json(self, body, status=200):
        self.send(json.dumps(body,ensure_ascii=False,allow_nan=False).encode(),'application/json; charset=utf-8',status)

    def do_GET(self):
        if not self.permitted():
            self.json({'error':'Access this console at its advertised loopback URL.'},403)
            return
        ws=self.server.workspace
        url=urlsplit(self.path)
        params=parse_qs(url.query)
        def get(key):
            return params.get(key,[''])[0]
        try:
            static={'/':('index.html','text/html'),'/style.css':('style.css','text/css'),
                    '/app.js':('app.js','text/javascript')}
            if url.path in static:
                name,kind=static[url.path]
                self.send((STATIC/name).read_bytes(),kind+'; charset=utf-8')
            elif url.path=='/api/state':
                self.json(ws.snapshot())
            elif url.path=='/api/profiles':
                self.json(ws.profiles())
            elif url.path=='/api/devices':
                try:
                    result=subprocess.run(['adb','devices'],capture_output=True,text=True,timeout=4,check=True)
                    serials=[row.split()[0] for row in result.stdout.splitlines()
                             if row.endswith('\tdevice') and SERIAL.fullmatch(row.split()[0])]
                except (OSError,subprocess.SubprocessError):
                    serials=[]
                self.json([*serials, 'fixture'])
            elif url.path=='/api/run':
                self.json(data.run_detail(ws.results,get('run'),active=ws.is_active(get('run'))))
            elif url.path=='/api/trace':
                run_id=get('run')
                self.json(data.trace(ws.results,run_id,get('case'),get('role'),active=ws.is_active(run_id)))
            elif url.path=='/api/image':
                path=data.image_path(ws.results,get('run'),get('case'),get('role'),get('frame'))
                self.send(path.read_bytes(),'image/png')
            elif url.path=='/api/artifact':
                root=data.run_directory(ws.results,get('run'))
                match=next((a for a in data.artifacts(ws.results,root) if a['path']==get('path')),None)
                if match is None:
                    raise FileNotFoundError('Unknown artifact.')
                path=data.confined(ws.results,ws.results/match['path'])
                self.send(path.read_bytes(),'text/plain; charset=utf-8')
            else:
                self.json({'error':'Not found.'},404)
        except (FileNotFoundError,ValueError,TypeError,KeyError):
            self.json({'error':'Requested artifact is missing or incomplete.'},404)
        except OSError:
            self.json({'error':'The local artifact could not be read.'},503)

    def do_POST(self):
        if not self.permitted() or self.headers.get('Origin')!=f'http://127.0.0.1:{self.server.server_port}':
            self.json({'error':'Run control requires the local console origin.'},403)
            return
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 1<=size<=32768 or self.headers.get_content_type()!='application/json':
                raise ValueError('Expected a small JSON request.')
            payload=json.loads(self.rfile.read(size))
            ws=self.server.workspace
            if self.path=='/api/plan':
                plan=ws.plan(payload)
                self.json({k:v for k,v in plan.items() if k!='config'})
            elif self.path=='/api/start':
                self.json(ws.start(payload),HTTPStatus.CREATED)
            elif self.path=='/api/stop':
                self.json(ws.stop(payload.get('job')))
            else:
                self.json({'error':'Not found.'},404)
        except (ValueError,TypeError,AttributeError) as error:
            self.json({'error':str(error)},400)
        except OSError:
            self.json({'error':'Could not start or stop the local runner.'},503)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path.cwd())
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--manifest',type=Path)
    args=parser.parse_args()
    root=args.root.resolve()
    workspace=Workspace(root,args.manifest or root/'results/round5-android/manifest-v2.json')
    with ThreadingHTTPServer(('127.0.0.1',args.port),Handler) as server:
        server.workspace=workspace
        print(f'TACT-GUI console: http://127.0.0.1:{server.server_port}',flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            workspace.close()


if __name__=='__main__':
    main()
