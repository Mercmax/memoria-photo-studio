"""Optional AI runtime lives separately from the dependency-free web server."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error
import ssl
from pathlib import Path
from model_catalog import CATALOG, PRESETS
from storage import ROOT, MODELS, WORK, settings
from download_support import https_context, download_error

class Cancelled(Exception):
    pass

def interpreter():
    binary = ROOT / '.ai-env' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    return str(binary if binary.is_file() else Path(sys.executable))

def hardware():
    gpus = []
    executable = shutil.which('nvidia-smi')
    if executable:
        try:
            out = subprocess.run([executable,'--query-gpu=index,name,memory.total,memory.free',
                                  '--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=8)
            if out.returncode == 0:
                for line in out.stdout.strip().splitlines():
                    index,name,total,free = [x.strip() for x in line.split(',')]
                    gpus.append({'id':'cuda:'+index,'name':name,'total_mb':int(total),'free_mb':int(free)})
        except (OSError,ValueError,subprocess.TimeoutExpired):
            pass
    return gpus

_probe_cache = (0, None)
def probe():
    global _probe_cache
    if time.monotonic()-_probe_cache[0] < 20 and _probe_cache[1] is not None:
        return _probe_cache[1]
    result = {'installed':False,'pillow':False,'devices':[],'message':'Запустите установку локального обработчика'}
    try:
        run = subprocess.run([interpreter(),str(ROOT/'ai_runner.py'),'--probe'],
                             capture_output=True,text=True,timeout=20)
        if run.returncode == 0:
            result.update(json.loads(run.stdout.strip().splitlines()[-1]))
    except (OSError,ValueError,subprocess.TimeoutExpired):
        pass
    result['gpus'] = hardware()
    _probe_cache = (time.monotonic(), result)
    return result

def catalog_status():
    return [{'id':key,**{k:v for k,v in spec.items() if k!='files'},
             'downloaded':all((MODELS/path).is_file() for path,_ in spec['files'])}
            for key,spec in CATALOG.items()]

def download_model(model_id, progress, cancelled):
    spec = CATALOG[model_id]
    hashes = {}
    for i,(name,url) in enumerate(spec['files']):
        if cancelled(): raise Cancelled()
        dest = MODELS/name
        temporary = dest.with_suffix(dest.suffix+'.part')
        try:
            dest.parent.mkdir(parents=True,exist_ok=True)
            progress(int(100*i/len(spec['files'])), 'Подключение: '+spec['name'])
            request = urllib.request.Request(url,headers={'User-Agent':'MemoriaStudio/2.0'})
            with urllib.request.urlopen(request,timeout=60,context=https_context()) as response, temporary.open('wb') as stream:
                total = int(response.headers.get('Content-Length',0)); received = 0
                digest = hashlib.sha256()
                while True:
                    if cancelled(): raise Cancelled()
                    chunk = response.read(1024*1024)
                    if not chunk: break
                    received += len(chunk)
                    if received > 2*1024*1024*1024: raise ValueError('Файл модели слишком большой')
                    stream.write(chunk); digest.update(chunk)
                    percent = min(99,int(100*(i+(received/total if total else .1))/len(spec['files'])))
                    progress(percent,'Скачивание '+spec['name']+f' · {received/1024/1024:.1f} МБ'+(f' из {total/1024/1024:.1f} МБ' if total else ''))
                if not received or (total and received != total): raise ValueError('Файл скачан не полностью')
            temporary.replace(dest)
            hashes[name] = digest.hexdigest()
        except (urllib.error.URLError, OSError, ssl.SSLError) as error:
            raise ValueError(download_error(error,url)) from error
        finally:
            temporary.unlink(missing_ok=True)
    (MODELS/(model_id+'-manifest.json')).write_text(json.dumps({'model':model_id,'sha256':hashes},indent=2))

def required_models(preset, options):
    operations = list(PRESETS[preset]['operations'])
    if options.get('upscale') and 'upscale' not in operations: operations.append('upscale')
    if options.get('faces') and 'faces' not in operations: operations.append('faces')
    models = []
    if 'colorize' in operations: models.append(options.get('color_model',settings()['color_model']))
    if 'upscale' in operations: models.append('esrgan4' if options.get('scale',2)==4 else 'esrgan2')
    if 'faces' in operations: models.append('gfpgan')
    if 'inpaint' in operations: models.append('lama')
    return operations,models

def run_job(job, device, progress, cancelled):
    options = json.loads(job['options'])
    operations,_ = required_models(job['preset'],options)
    directory = WORK/job['id'];directory.mkdir(exist_ok=True)
    request = directory/'request.json'
    request.write_text(json.dumps({'source':job['source'],'operations':operations,'options':options,
                                  'models':str(MODELS),'output':str(directory),'device':device}),encoding='utf-8')
    with (directory/'runner.log').open('w',encoding='utf-8') as log:
        child = subprocess.Popen([interpreter(),str(ROOT/'ai_runner.py'),'--request',str(request)],
                                 stdout=log,stderr=log,cwd=str(MODELS))
        start = time.monotonic()
        try:
            while child.poll() is None:
                if cancelled():
                    child.terminate()
                    try: child.wait(timeout=5)
                    except subprocess.TimeoutExpired: child.kill();child.wait()
                    raise Cancelled()
                if time.monotonic()-start > 30*60:
                    child.kill();child.wait();raise ValueError('Обработка превысила 30 минут')
                status = directory/'progress.json'
                if status.exists():
                    try:
                        data=json.loads(status.read_text(encoding='utf-8'));progress(data['progress'],data['stage'])
                    except (ValueError,OSError): pass
                time.sleep(.4)
            if child.returncode != 0:
                detail=(directory/'error.txt').read_text(encoding='utf-8') if (directory/'error.txt').exists() else 'Проверьте установку AI-обработчика'
                raise ValueError(detail[:400])
            return json.loads((directory/'result.json').read_text(encoding='utf-8'))
        finally:
            if child.poll() is None: child.kill();child.wait()
