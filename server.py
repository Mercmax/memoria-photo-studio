"""Memoria local studio, standard-library web server with an optional AI runtime."""
import argparse
import base64
import csv
import io
import json
import mimetypes
import os
import re
import secrets
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import job_queue
import local_runtime
from instance_lock import InstanceLock
from storage import ROOT,DATA,FILES,WORK,db,settings,amount,image_path,save_image,add_version

TOKEN=os.environ.get('STUDIO_ACCESS_TOKEN','')
SESSIONS={}

def photo(c,ident):
    row=c.execute('SELECT * FROM photos WHERE id=?',(ident,)).fetchone()
    if not row:raise ValueError('Фотография не найдена')
    return row

def idle(c,ident):
    if c.execute("SELECT 1 FROM jobs WHERE photo_id=? AND status IN ('queued','running')",(ident,)).fetchone():
        raise ValueError('Сначала дождитесь обработки или отмените задание в очереди')

def safe_name(name):
    return re.sub(r'[^\w .-]','_',name,flags=re.UNICODE).strip(' .')[:120] or 'photo'

def convert(source,target,format_name,dpi):
    if format_name not in ('jpg','png','tiff'):raise ValueError('Форматы: JPG, PNG, TIFF')
    if not local_runtime.probe().get('pillow'):raise ValueError('Для конвертации установите локальный обработчик. ZIP исходных результатов доступен без него')
    run=subprocess.run([local_runtime.interpreter(),str(ROOT/'ai_runner.py'),'--export-source',str(source),
                        '--export-target',str(target),'--dpi',str(dpi)],capture_output=True,timeout=90)
    if run.returncode:raise ValueError('Не удалось преобразовать изображение')

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass

    def send_bytes(self,data,content_type='application/json; charset=utf-8',status=200,filename=None):
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        if filename:self.send_header('Content-Disposition',"attachment; filename*=UTF-8''"+urllib.parse.quote(filename))
        self.end_headers();self.wfile.write(data)

    def reply(self,value,status=200):
        self.send_bytes(json.dumps(value,ensure_ascii=False).encode(),status=status)

    def authorized(self):
        if not TOKEN:
            host=urllib.parse.urlparse('http://'+self.headers.get('Host','')).hostname
            return host in ('127.0.0.1','localhost','::1')
        cookie=SimpleCookie()
        try:cookie.load(self.headers.get('Cookie',''))
        except Exception:return False
        sid=cookie.get('memoria_session')
        return bool(sid and SESSIONS.get(sid.value,0)>time.monotonic())

    def do_GET(self):
        parsed=urllib.parse.urlparse(self.path);path=urllib.parse.unquote(parsed.path)
        query=urllib.parse.parse_qs(parsed.query)
        if (path.startswith('/api/') or path.startswith('/photos/')) and path!='/api/health' and not self.authorized():
            self.reply({'error':'Войдите в локальную студию'},401);return
        try:
            if path=='/api/health':self.reply({'ok':True,'version':'2.0','login_required':bool(TOKEN)});return
            if path=='/api/photos':
                with db() as c:result=[dict(r) for r in c.execute('SELECT p.*,(SELECT COUNT(*) FROM versions v WHERE v.photo_id=p.id) AS version_count FROM photos p ORDER BY p.created DESC,p.rowid DESC')]
                self.reply(result);return
            if path=='/api/settings':self.reply(settings(public=True));return
            if path=='/api/jobs':
                with db() as c:result=[dict(r) for r in c.execute('''SELECT j.id,j.photo_id,j.kind,j.preset,j.engine,j.status,j.progress,j.stage,j.error,
                    j.device,j.priority,j.attempts,j.cancel,j.created,j.updated,p.name AS photo_name
                    FROM jobs j LEFT JOIN photos p ON p.id=j.photo_id ORDER BY j.created DESC,j.rowid DESC LIMIT 200''')]
                self.reply(result);return
            if path=='/api/models':
                self.reply({'models':local_runtime.catalog_status(),'runtime':local_runtime.probe()});return
            if path=='/api/orders':
                with db() as c:
                    result=[]
                    for r in c.execute('SELECT * FROM orders ORDER BY created DESC,rowid DESC').fetchall():
                        item=dict(r);item['photos']=[dict(p) for p in c.execute('SELECT id,name,result,status FROM photos WHERE order_id=?',(r['id'],))]
                        item['remaining']=round(max(0,r['total']-r['deposit']),2);result.append(item)
                self.reply(result);return
            bits=path.strip('/').split('/')
            if len(bits)==4 and bits[:2]==['api','photos'] and bits[3]=='versions':
                with db() as c:
                    p=photo(c,bits[2]);versions=[{'id':'original','image':p['original'],'label':'Оригинал','created':p['created']}]
                    versions += [dict(r) for r in c.execute('SELECT * FROM versions WHERE photo_id=? ORDER BY created,rowid',(p['id'],))]
                self.reply(versions);return
            if path=='/api/export':self.export_zip(query);return
            if len(bits)==4 and bits[:2]==['api','photos'] and bits[3]=='export':
                with db() as c:p=dict(photo(c,bits[2]))
                source=image_path(p['original'] if query.get('source',['result'])[0]=='original' else p['result'] or p['original'])
                format_name=query.get('format',['native'])[0]
                if format_name=='native':self.send_bytes(source.read_bytes(),mimetypes.guess_type(source.name)[0] or 'application/octet-stream',filename=safe_name(Path(p['name']).stem)+source.suffix);return
                dpi=int(query.get('dpi',['300'])[0])
                if dpi not in (300,600):raise ValueError('DPI: 300 или 600')
                with tempfile.TemporaryDirectory(dir=WORK) as directory:
                    dest=Path(directory)/('export.'+format_name);convert(source,dest,format_name,dpi)
                    self.send_bytes(dest.read_bytes(),mimetypes.guess_type(dest.name)[0] or 'application/octet-stream',filename=safe_name(Path(p['name']).stem)+dest.suffix)
                return
            base,relative=(FILES,path[8:]) if path.startswith('/photos/') else (ROOT/'static',path.lstrip('/') or 'index.html')
            file=(base/relative).resolve()
            if not file.is_relative_to(base.resolve()) or not file.is_file():self.reply({'error':'Не найдено'},404);return
            mime=mimetypes.guess_type(file.name)[0] or 'application/octet-stream'
            if file.suffix=='.html':mime='text/html; charset=utf-8'
            self.send_bytes(file.read_bytes(),mime)
        except (ValueError,KeyError) as error:self.reply({'error':str(error)},400)
        except Exception:self.reply({'error':'Не удалось выполнить запрос'},500)

    def export_zip(self,query):
        requested=query.get('ids',[''])[0].split(',') if query.get('ids') else None
        order_id=query.get('order',[''])[0]
        source=query.get('source',['result'])[0]
        if source not in ('result','original'):raise ValueError('Неизвестный источник экспорта')
        with db() as c:
            rows=[dict(r) for r in c.execute('SELECT * FROM photos ORDER BY created,rowid')]
            if requested:rows=[r for r in rows if r['id'] in requested]
            if order_id:rows=[r for r in rows if r['order_id']==order_id]
        rows=[r for r in rows if r[source]]
        if not rows:raise ValueError('Нет фотографий для экспорта. Для результатов дождитесь обработки')
        with tempfile.TemporaryDirectory(dir=WORK) as directory:
            target=Path(directory)/'memoria-export.zip'
            with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as archive:
                manifest=io.StringIO();writer=csv.writer(manifest);writer.writerow(['Файл','Фото','Клиент','Стоимость','Статус'])
                for row in rows:
                    file=image_path(row[source]);name=row['id'][:8]+'-'+safe_name(Path(row['name']).stem)+file.suffix
                    archive.write(file,name);writer.writerow([name,row['name'],row['client'],row['price'],row['status']])
                archive.writestr('manifest.csv','\ufeff'+manifest.getvalue())
            self.send_response(200);self.send_header('Content-Type','application/zip');self.send_header('Content-Length',str(target.stat().st_size))
            self.send_header('Content-Disposition','attachment; filename="memoria-export.zip"');self.send_header('Cache-Control','no-store');self.end_headers()
            with target.open('rb') as stream:
                while chunk:=stream.read(1024*1024):self.wfile.write(chunk)

    def do_POST(self):
        try:
            origin=self.headers.get('Origin')
            if origin and urllib.parse.urlparse(origin).netloc != self.headers.get('Host'):
                self.reply({'error':'Запрос из другого сайта запрещён'},403);return
            if self.headers.get('Content-Type','').split(';')[0]!='application/json':
                self.reply({'error':'Ожидается JSON'},415);return
            length=int(self.headers.get('Content-Length',0))
            if not 0<length<=72*1024*1024:self.reply({'error':'Пустой или слишком большой запрос'},413);return
            body=json.loads(self.rfile.read(length))
            if not isinstance(body,dict):raise ValueError('Ожидается JSON объект')
            path=urllib.parse.urlparse(self.path).path
            if path=='/api/login':
                if TOKEN and not secrets.compare_digest(str(body.get('token','')),TOKEN):self.reply({'error':'Неверный код доступа'},401);return
                sid=secrets.token_urlsafe(32);SESSIONS[sid]=time.monotonic()+12*3600
                self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Set-Cookie','memoria_session='+sid+'; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200');self.end_headers();self.wfile.write(b'{"ok":true}');return
            if not self.authorized():self.reply({'error':'Войдите в локальную студию'},401);return
            if 'price' in body:body['price']=amount(body['price'])
            result,status=self.mutate(path,body)
            self.reply(result,status)
        except (ValueError,KeyError) as error:self.reply({'error':str(error)},400)
        except Exception:self.reply({'error':'Не удалось выполнить запрос. Проверьте данные и подключение'},400)

    def mutate(self,path,body):
        if path=='/api/jobs':
            ids=body.get('photo_ids',[])
            if not isinstance(ids,list) or not 0<len(ids)<=100:raise ValueError('Выберите от 1 до 100 фотографий')
            result=[];errors=[]
            for ident in ids:
                try:result.append(job_queue.enqueue_photo(ident,body.get('preset','gentle'),body.get('engine','local'),body.get('options',{}),body.get('priority',0)))
                except ValueError as error:errors.append({'photo_id':ident,'error':str(error)})
            if not result:raise ValueError(errors[0]['error'])
            return {'jobs':result,'errors':errors},201
        bits=path.strip('/').split('/')
        if len(bits)==4 and bits[:2]==['api','jobs']:
            if bits[3]=='cancel':job_queue.cancel(bits[2])
            elif bits[3]=='retry':job_queue.retry(bits[2])
            else:raise ValueError('Неизвестная команда')
            return {'ok':True},200
        if path=='/api/models/download':return job_queue.enqueue_download(body.get('model')),201
        if path=='/api/settings':
            config=settings();endpoint=str(body.get('endpoint',config.get('endpoint',''))).strip()
            if endpoint and (urllib.parse.urlparse(endpoint).scheme not in ('http','https') or not urllib.parse.urlparse(endpoint).hostname):raise ValueError('Укажите HTTP или HTTPS адрес обработчика')
            devices=body.get('devices',config['devices'])
            if not isinstance(devices,list) or not 1<=len(devices)<=2 or len(set(devices))!=len(devices) or any(not re.fullmatch(r'auto|cpu|mps|cuda:\d+',str(d)) for d in devices) or ('auto' in devices and len(devices)>1):raise ValueError('Выберите до двух разных устройств, либо автоматический режим')
            color=body.get('color_model',config['color_model']);tile=int(body.get('tile',config['tile']))
            if color not in ('ddcolor','ddcolor_tiny') or tile not in (128,256,512):raise ValueError('Некорректные настройки моделей')
            config.update(endpoint=endpoint,name=str(body.get('name',config['name']))[:100],devices=devices,color_model=color,tile=tile)
            if body.get('clear_key'):config['key']=''
            elif body.get('key'):config['key']=str(body['key'])
            with db() as c:c.execute('INSERT OR REPLACE INTO settings VALUES(1,?)',(json.dumps(config),))
            local_runtime._probe_cache=(0,None)
            return {'ok':True,'restart_required':True},200
        if path=='/api/photos':
            ident=uuid.uuid4().hex
            with db() as c:
                order_id=body.get('order_id') or None
                order=c.execute('SELECT * FROM orders WHERE id=?',(order_id,)).fetchone() if order_id else None
                if order_id and not order:raise ValueError('Заказ не найден')
                original=save_image(body['image'],ident+'-original')
                c.execute('INSERT INTO photos(id,name,original,client,price,order_id) VALUES(?,?,?,?,?,?)',
                    (ident,str(body.get('name','Фотография'))[:200],original,order['client'] if order else str(body.get('client',''))[:200],body.get('price',0),order_id))
                if order and order['status'] in ('ready','delivered'):
                    c.execute("UPDATE orders SET status='working' WHERE id=?",(order_id,))
                return dict(photo(c,ident)),201
        if path=='/api/orders' or (len(bits)==3 and bits[:2]==['api','orders']):
            ident=bits[2] if len(bits)==3 else uuid.uuid4().hex
            with db() as c:
                existing=c.execute('SELECT * FROM orders WHERE id=?',(ident,)).fetchone()
                if len(bits)==3 and not existing:raise ValueError('Заказ не найден')
                value={'name':'Новый заказ','client':'','total':0,'deposit':0,'due':'','notes':'','status':'new',**(dict(existing) if existing else {}),**body}
                total,deposit=amount(value['total']),amount(value['deposit'])
                if deposit>total:raise ValueError('Предоплата не может превышать стоимость')
                if value['status'] not in ('new','working','ready','delivered'):raise ValueError('Неизвестный статус')
                if value['due']:
                    import datetime
                    datetime.date.fromisoformat(str(value['due']))
                c.execute('''INSERT INTO orders(id,name,client,total,deposit,due,notes,status) VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET name=excluded.name,client=excluded.client,total=excluded.total,deposit=excluded.deposit,due=excluded.due,notes=excluded.notes,status=excluded.status''',
                    (ident,str(value['name'])[:200],str(value['client'])[:200],total,deposit,str(value['due']),str(value['notes'])[:5000],value['status']))
                ids=body.get('photo_ids')
                if ids is not None:
                    if not isinstance(ids,list):raise ValueError('Неверный список фотографий')
                    for pid in ids:
                        p=photo(c,pid)
                        if p['order_id'] and p['order_id']!=ident:raise ValueError('Фотография уже принадлежит другому заказу')
                    c.execute('UPDATE photos SET order_id=NULL WHERE order_id=?',(ident,))
                    for pid in ids:c.execute('UPDATE photos SET order_id=?,client=? WHERE id=?',(ident,str(value['client'])[:200],pid))
                else:c.execute('UPDATE photos SET client=? WHERE order_id=?',(str(value['client'])[:200],ident))
                if value['status']=='delivered':
                    attached=c.execute('SELECT * FROM photos WHERE order_id=?',(ident,)).fetchall()
                    if not attached or any(not p['result'] for p in attached):raise ValueError('Для выдачи все фотографии заказа должны быть обработаны')
                    c.execute("UPDATE photos SET status='delivered' WHERE order_id=?",(ident,))
                return dict(c.execute('SELECT * FROM orders WHERE id=?',(ident,)).fetchone()),200
        if len(bits)>=3 and bits[:2]==['api','photos']:
            ident=bits[2];action=bits[3] if len(bits)==4 else ''
            if action=='external':
                # Compatibility for the v1 API. New UI uses the durable queue.
                job=job_queue.enqueue_photo(ident,'color','external',{})
                with db() as c:
                    changed=c.execute("UPDATE jobs SET status='running',device='external',attempts=attempts+1 WHERE id=? AND status='queued'",(job['id'],)).rowcount
                if changed:job_queue.execute(job,'external')
                else:
                    deadline=time.monotonic()+125
                    while time.monotonic()<deadline:
                        with db() as c: state=c.execute('SELECT status FROM jobs WHERE id=?',(job['id'],)).fetchone()['status']
                        if state not in ('queued','running'):break
                        time.sleep(.2)
                with db() as c:
                    row=c.execute('SELECT * FROM jobs WHERE id=?',(job['id'],)).fetchone()
                    if row['status']!='completed':raise ValueError(row['error'])
                    return {'result':photo(c,ident)['result']},200
            with db() as c:
                p=photo(c,ident)
                if action=='delete':
                    idle(c,ident)
                    paths={p['original'],p['result']}|{v['image'] for v in c.execute('SELECT image FROM versions WHERE photo_id=?',(ident,))}
                    for job in c.execute('SELECT options FROM jobs WHERE photo_id=?',(ident,)):
                        mask_path=json.loads(job['options']).get('mask')
                        if mask_path and Path(mask_path).resolve().is_relative_to(FILES.resolve()):paths.add('/photos/'+Path(mask_path).name)
                    c.execute('DELETE FROM photos WHERE id=?',(ident,))
                    for path in paths:
                        if path:(FILES/path[8:]).unlink(missing_ok=True)
                    return {'ok':True},200
                if action=='restore':
                    idle(c,ident);version_id=body.get('version_id')
                    if version_id=='original':c.execute("UPDATE photos SET result=NULL,status='new' WHERE id=?",(ident,))
                    else:
                        version=c.execute('SELECT * FROM versions WHERE id=? AND photo_id=?',(version_id,ident)).fetchone()
                        if not version:raise ValueError('Версия не найдена')
                        c.execute("UPDATE photos SET result=?,status='ready' WHERE id=?",(version['image'],ident))
                    c.execute("UPDATE orders SET status='working' WHERE status='delivered' AND id=?",(p['order_id'],))
                    return dict(photo(c,ident)),200
                if action:raise ValueError('Неизвестная команда')
                if body.get('image'):
                    idle(c,ident);result=save_image(body['image'],max_bytes=64*1024*1024);add_version(c,ident,result,body.get('label','Ручная коррекция'))
                if 'client' in body:c.execute('UPDATE photos SET client=?,price=? WHERE id=?',(str(body['client'])[:200],body.get('price',p['price']),ident))
                if 'order_id' in body:
                    order_id=body['order_id'] or None
                    if order_id and not c.execute('SELECT 1 FROM orders WHERE id=?',(order_id,)).fetchone():raise ValueError('Заказ не найден')
                    c.execute('UPDATE photos SET order_id=? WHERE id=?',(order_id,ident))
                    if order_id:
                        order_client=c.execute('SELECT client FROM orders WHERE id=?',(order_id,)).fetchone()['client']
                        c.execute('UPDATE photos SET client=? WHERE id=?',(order_client,ident))
                if 'status' in body:
                    if body['status'] not in ('new','ready','delivered'):raise ValueError('Неизвестный статус')
                    if body['status']!='new' and not photo(c,ident)['result']:raise ValueError('Сначала сохраните результат обработки')
                    c.execute('UPDATE photos SET status=? WHERE id=?',(body['status'],ident))
                return dict(photo(c,ident)),200
        raise ValueError('Неизвестный запрос')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8000);parser.add_argument('--host',default='127.0.0.1');args=parser.parse_args()
    if args.host not in ('127.0.0.1','localhost','::1') and not TOKEN:
        parser.error('Для доступа по сети задайте STUDIO_ACCESS_TOKEN — код входа в студию')
    try:
        with InstanceLock(DATA/'studio.lock'):
            httpd=ThreadingHTTPServer((args.host,args.port),Handler);job_queue.start()
            print(f'Memoria 2.0: http://{args.host}:{args.port}',flush=True)
            try:httpd.serve_forever()
            except KeyboardInterrupt:pass
            finally:job_queue.stop();httpd.server_close()
    except RuntimeError as error:parser.error(str(error))
