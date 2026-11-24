"""Bounded ADB transport with a structured, non-programmable action language."""
import json
import math
import re
import shlex
import struct
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET


def png_dimensions(png):
    if len(png) < 24 or png[:8] != b'\x89PNG\r\n\x1a\n' or png[12:16] != b'IHDR':
        raise ValueError('invalid screenshot PNG header')
    width, height = struct.unpack('>II', png[16:24])
    if not 1 <= width <= 16384 or not 1 <= height <= 16384:
        raise ValueError('invalid screenshot dimensions')
    return width, height


def parse_action(text):
    blocks = re.findall(r'<tool_call>\s*(.*?)\s*</tool_call>',text,re.S)
    if len(blocks) != 1:
        raise ValueError('exactly one structured action required')
    obj = json.loads(blocks[0])
    if not isinstance(obj,dict) or obj.get('name') != 'computer_use' or not isinstance(obj.get('arguments'),dict):
        raise ValueError('invalid computer_use object')
    a = obj['arguments']; kind = a.get('action')
    allowed = {'left_click':{'coordinate'}, 'type':{'text','coordinate'},
               'key':{'key','keys'}, 'scroll':{'direction','pixels'}, 'terminate':{'status','coordinate'}}
    if kind not in allowed or set(a)-{'action'}-allowed[kind]:
        raise ValueError('unsupported action fields')
    if 'coordinate' in a:
        xy = a['coordinate']
        if not isinstance(xy,list) or len(xy) != 2 or any(type(v) not in (int,float) or not math.isfinite(v) or not 0 <= v < 1000 for v in xy):
            raise ValueError('invalid normalized coordinates')
    if kind == 'left_click' and 'coordinate' not in a:
        raise ValueError('missing coordinate')
    if kind == 'type':
        text = a.get('text')
        # ADB input text cannot reliably represent Unicode. Fail explicitly.
        if not isinstance(text,str) or not 1 <= len(text) <= 500 or any(not 32 <= ord(c) < 127 for c in text) or '%' in text:
            raise ValueError('ADB typing supports printable ASCII without percent only')
    if kind == 'key':
        key = a.get('key')
        if 'keys' in a:
            if key is not None or not isinstance(a['keys'],list) or len(a['keys']) != 1:
                raise ValueError('one key required')
            key = a['keys'][0]
        if key not in ('Enter','Tab','Escape','Backspace','Back'):
            raise ValueError('unsupported key')
        a = {'action':'key','key':key}
    if kind == 'scroll':
        direction = a.get('direction')
        if 'pixels' in a:
            pixels = a['pixels']
            if type(pixels) not in (int,float) or not math.isfinite(pixels) or pixels == 0:
                raise ValueError('invalid scroll amount')
            derived = 'up' if pixels > 0 else 'down'
            if direction is not None and direction != derived:
                raise ValueError('inconsistent scroll direction')
            direction = derived
        if direction not in ('up','down'):
            raise ValueError('invalid scroll direction')
        a = {'action':'scroll','direction':direction}
    if kind == 'terminate':
        if a.get('status') not in (None,'success','failure'):
            raise ValueError('invalid termination status')
        # Some GUI-Plus snapshots attach coordinates and omit status. A stop
        # request is safe; never turn it into verified task success.
        a = {'action':'terminate','status':a.get('status','unknown')}
    return a


class Android:
    def __init__(self, serial, timeout=20):
        if not isinstance(serial,str) or not re.fullmatch(r'[\w.:-]+',serial) or serial.startswith('-'):
            raise ValueError('explicit device serial required')
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('positive timeout required')
        self.serial, self.timeout = serial, timeout

    def adb(self,*args):
        return subprocess.run(['adb','-s',self.serial,*args],check=True,capture_output=True,timeout=self.timeout).stdout

    def screenshot(self):
        png = self.adb('exec-out','screencap','-p')
        png_dimensions(png)
        return png

    def reset(self, package, activity):
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.]+',package) or not re.fullmatch(r'[A-Za-z0-9_.$]+',activity):
            raise ValueError('invalid app identifier')
        self.adb('shell','am','force-stop',package)
        result = self.adb('shell','pm','clear',package)
        if result.strip() != b'Success':
            raise RuntimeError('app data reset failed')
        self.adb('logcat','-c')
        result = self.adb('shell','am','start','-W','-n',shlex.quote(package+'/'+activity))
        if b'Error:' in result or b'Exception' in result:
            raise RuntimeError('app launch failed')

    def crash_log(self):
        return self.adb('logcat','-d','-b','crash','-v','brief').decode('utf-8',errors='replace')

    def execute(self,text,width,height):
        a = parse_action(text)
        if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
            raise ValueError('invalid screen size')
        if a['action'] == 'terminate':
            return a
        if 'coordinate' in a:
            x,y = a['coordinate']
            self.adb('shell','input','tap',str(int(x*width/1000)),str(int(y*height/1000)))
        if a['action'] == 'type':
            # adb shell may join argv into a remote shell string: quote there too.
            self.adb('shell','input','text',shlex.quote(a['text'].replace(' ','%s')))
        elif a['action'] == 'key':
            keys = {'Enter':'66','Tab':'61','Escape':'111','Backspace':'67','Back':'4'}
            self.adb('shell','input','keyevent',keys[a['key']])
        elif a['action'] == 'scroll':
            top,bottom = int(height*.25),int(height*.75)
            start,end = (top,bottom) if a['direction'] == 'up' else (bottom,top)
            self.adb('shell','input','swipe',str(width//2),str(start),str(width//2),str(end),'300')
        time.sleep(0.3)  # bounded UI settling; not a determinism guarantee
        return a

    def ui_text(self,resource_id):
        if not isinstance(resource_id,str) or not re.fullmatch(r'[A-Za-z0-9_.$:/]+',resource_id):
            raise ValueError('invalid resource id')
        path='/sdcard/budgetmas-'+uuid.uuid4().hex+'.xml'
        try:
            self.adb('shell','uiautomator','dump',path)
            raw=self.adb('exec-out','cat',path)
            root=ET.fromstring(raw)
            return [node.get('text','') for node in root.iter('node') if node.get('resource-id')==resource_id]
        finally:
            self.adb('shell','rm','-f',path)
