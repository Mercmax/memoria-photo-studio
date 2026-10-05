"""Persistent queue. One isolated subprocess per configured device, plus a download lane."""
import base64
import json
import shutil
import threading
import uuid
import urllib.request
from pathlib import Path
import local_runtime as runtime
from model_catalog import CATALOG,PRESETS
from storage import db,settings,image_path,save_image,add_version,WORK

_lock=threading.Lock()
_stop=threading.Event()
_threads=[]


def recover():
    with db() as c:
        c.execute("UPDATE jobs SET status='cancelled',stage='Отменено после перезапуска',updated=CURRENT_TIMESTAMP WHERE status='running' AND cancel=1")
        c.execute("UPDATE jobs SET status='queued',progress=0,stage='Возобновлено после перезапуска',device=NULL,updated=CURRENT_TIMESTAMP WHERE status='running' AND cancel=0")


def enqueue_photo(photo_id,preset='gentle',engine='local',options=None,priority=0):
    if preset not in PRESETS:raise ValueError('Неизвестный режим обработки')
    if engine not in ('local','external'):raise ValueError('Неизвестный обработчик')
    options={**(options or {})}
    options['color_model']=options.get('color_model',settings()['color_model'])
    if options['color_model'] not in ('ddcolor','ddcolor_tiny'):raise ValueError('Неизвестная модель раскраски')
    options['scale']=int(options.get('scale',2))
    if options['scale'] not in (2,4):raise ValueError('Увеличение может быть ×2 или ×4')
    options['tile']=int(options.get('tile',settings()['tile']))
    if options['tile'] not in (128,256,512):raise ValueError('Размер фрагмента: 128, 256 или 512')
    options['face_strength']=max(0,min(1,float(options.get('face_strength',.35))))
    operations,models=runtime.required_models(preset,options)
    if engine=='local':
        status=runtime.probe()
        if not status.get('pillow'):raise ValueError('Установите локальный обработчик: setup-ai-windows.bat')
        if models and not status.get('installed'):raise ValueError('AI-зависимости не установлены')
        downloaded={x['id']:x['downloaded'] for x in runtime.catalog_status()}
        missing=[CATALOG[m]['name'] for m in models if not downloaded[m]]
        if missing:raise ValueError('Скачайте модели в разделе Инструменты: '+', '.join(missing))
    elif not settings().get('endpoint'):raise ValueError('Внешний обработчик не подключён')
    ident=uuid.uuid4().hex
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        photo=c.execute('SELECT * FROM photos WHERE id=?',(photo_id,)).fetchone()
        if not photo:raise ValueError('Фотография не найдена')
        if c.execute("SELECT 1 FROM jobs WHERE photo_id=? AND status IN ('queued','running')",(photo_id,)).fetchone():raise ValueError('Эта фотография уже находится в очереди')
        source=photo['result'] or photo['original']
        if options.pop('from_original',False):source=photo['original']
        if preset=='retouch':
            if not options.get('mask_image'):raise ValueError('Не передана маска ретуши')
            mask=save_image(options.pop('mask_image'),ident+'-mask')
            options['mask']=str(image_path(mask))
        c.execute('INSERT INTO jobs(id,photo_id,engine,preset,options,source,priority) VALUES(?,?,?,?,?,?,?)',
                  (ident,photo_id,engine,preset,json.dumps(options),str(image_path(source)),int(priority)))
        return dict(c.execute('SELECT * FROM jobs WHERE id=?',(ident,)).fetchone())


def enqueue_download(model):
    if model not in CATALOG:raise ValueError('Неизвестная модель')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute("SELECT * FROM jobs WHERE kind='download' AND preset=? AND status IN ('queued','running')",(model,)).fetchone()
        if row:return dict(row)
        ident=uuid.uuid4().hex
        c.execute("INSERT INTO jobs(id,kind,engine,preset,options) VALUES(?,'download','download',?,'{}')",(ident,model))
        return dict(c.execute('SELECT * FROM jobs WHERE id=?',(ident,)).fetchone())


def claim(device,kind='process'):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute("SELECT * FROM jobs WHERE status='queued' AND cancel=0 AND kind=? ORDER BY priority DESC,created,rowid LIMIT 1",(kind,)).fetchone()
        if not row:return None
        c.execute("UPDATE jobs SET status='running',device=?,attempts=attempts+1,stage='Запуск обработчика',updated=CURRENT_TIMESTAMP WHERE id=?",(device,row['id']))
        return dict(c.execute('SELECT * FROM jobs WHERE id=?',(row['id'],)).fetchone())


def cancel(ident):
    with db() as c:
        row=c.execute('SELECT * FROM jobs WHERE id=?',(ident,)).fetchone()
        if not row:raise ValueError('Задание не найдено')
        if row['status'] in ('queued','running'):
            c.execute("UPDATE jobs SET cancel=1,status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END,stage='Отмена запрошена',updated=CURRENT_TIMESTAMP WHERE id=?",(ident,))


def retry(ident):
    with db() as c:
        row=c.execute('SELECT * FROM jobs WHERE id=?',(ident,)).fetchone()
        if not row or row['status'] not in ('failed','cancelled'):raise ValueError('Можно повторить только остановленное задание')
        if row['kind']=='process' and not c.execute('SELECT 1 FROM photos WHERE id=?',(row['photo_id'],)).fetchone():raise ValueError('Фотография удалена')
        if row['kind']=='process' and c.execute("SELECT 1 FROM jobs WHERE photo_id=? AND status IN ('queued','running')",(row['photo_id'],)).fetchone():raise ValueError('Фото уже находится в очереди')
        c.execute("UPDATE jobs SET status='queued',cancel=0,error='',progress=0,stage='Повторная обработка',device=NULL,updated=CURRENT_TIMESTAMP WHERE id=?",(ident,))


def execute(job,device):
    def cancelled():
        if _stop.is_set():return True
        with db() as c:
            row=c.execute('SELECT cancel FROM jobs WHERE id=?',(job['id'],)).fetchone()
            return not row or bool(row['cancel'])
    def progress(value,stage):
        with db() as c:c.execute('UPDATE jobs SET progress=?,stage=?,updated=CURRENT_TIMESTAMP WHERE id=?',(max(0,min(100,int(value))),stage,job['id']))
    try:
        if cancelled():raise runtime.Cancelled()
        if job['kind']=='download':runtime.download_model(job['preset'],progress,cancelled)
        else:
            if job['engine']=='external':
                source=Path(job['source']);mime={'.jpg':'jpeg','.png':'png','.webp':'webp'}[source.suffix]
                operations,_=runtime.required_models(job['preset'],json.loads(job['options']))
                remote_options=json.loads(job['options'])
                if remote_options.get('mask'):
                    remote_options['mask_image']='data:image/png;base64,'+base64.b64encode(Path(remote_options.pop('mask')).read_bytes()).decode()
                payload={'image':'data:image/'+mime+';base64,'+base64.b64encode(source.read_bytes()).decode(),
                         'operations':['restore' if x=='basic' else x for x in operations],'photo_id':job['photo_id'],'options':remote_options}
                config=settings();headers={'Content-Type':'application/json'}
                if config.get('key'):headers['Authorization']='Bearer '+config['key']
                progress(10,'Ожидание внешнего обработчика')
                req=urllib.request.Request(config['endpoint'],data=json.dumps(payload).encode(),headers=headers)
                with urllib.request.urlopen(req,timeout=120) as response:raw=response.read(40*1024*1024+1)
                if len(raw)>40*1024*1024:raise ValueError('Ответ обработчика слишком большой')
                outputs=[{'image':json.loads(raw)['image'],'label':'Внешний AI-обработчик'}]
            else:
                outputs=runtime.run_job(job,device,progress,cancelled)
                for item in outputs:
                    path=Path(item['file']).resolve()
                    if not path.is_relative_to((WORK/job['id']).resolve()):raise ValueError('Неверный путь результата')
                    item['image']='data:image/png;base64,'+base64.b64encode(path.read_bytes()).decode()
            if cancelled():raise runtime.Cancelled()
            saved=[]
            try:
                for item in outputs:
                    item['url']=save_image(item['image'],max_bytes=64*1024*1024);saved.append(item['url'])
                with db() as c:
                    if not c.execute('SELECT 1 FROM photos WHERE id=?',(job['photo_id'],)).fetchone():raise runtime.Cancelled()
                    if c.execute('SELECT cancel FROM jobs WHERE id=?',(job['id'],)).fetchone()['cancel']:raise runtime.Cancelled()
                    for index,item in enumerate(outputs):
                        add_version(c,job['photo_id'],item['url'],item['label'],job['id'],active=index==len(outputs)-1)
                    c.execute("UPDATE jobs SET status='completed',progress=100,stage='Готово',updated=CURRENT_TIMESTAMP WHERE id=?",(job['id'],))
            except Exception:
                for url in saved:image_path(url).unlink(missing_ok=True)
                raise
        if job['kind']=='download':
            with db() as c:
                if c.execute('SELECT cancel FROM jobs WHERE id=?',(job['id'],)).fetchone()['cancel']:raise runtime.Cancelled()
                c.execute("UPDATE jobs SET status='completed',progress=100,stage='Готово',updated=CURRENT_TIMESTAMP WHERE id=?",(job['id'],))
    except runtime.Cancelled:
        with db() as c:
            row=c.execute('SELECT cancel FROM jobs WHERE id=?',(job['id'],)).fetchone()
            interrupted=_stop.is_set() and row and not row['cancel']
            c.execute("UPDATE jobs SET status=?,stage=?,progress=0,updated=CURRENT_TIMESTAMP WHERE id=?",('queued' if interrupted else 'cancelled','Возобновится после запуска' if interrupted else 'Отменено',job['id']))
    except Exception as error:
        detail=str(error) if isinstance(error,ValueError) else 'Обработка не завершилась. Проверьте подключение, модели и зависимости.'
        with db() as c:c.execute("UPDATE jobs SET status='failed',error=?,stage='Ошибка обработки',updated=CURRENT_TIMESTAMP WHERE id=?",(detail[:400],job['id']))
    finally:
        shutil.rmtree(WORK/job['id'],ignore_errors=True)


def worker(device,kind):
    while not _stop.is_set():
        job=claim(device,kind)
        if job:execute(job,device)
        else:_stop.wait(.7)


def start():
    with _lock:
        if _threads:return
        recover();_stop.clear()
        devices=settings()['devices']
        if devices==['auto']:
            # CPU is the dependable Mac default; explicit MPS is available after testing.
            gpus=runtime.hardware();devices=[gpu['id'] for gpu in gpus[:2]] or ['cpu']
        for device,kind in [(d,'process') for d in devices]+[('download','download')]:
            thread=threading.Thread(target=worker,args=(device,kind),daemon=True,name='memoria-'+device)
            _threads.append(thread);thread.start()


def stop():
    _stop.set()
    for thread in _threads:thread.join(timeout=6)
    _threads.clear()
