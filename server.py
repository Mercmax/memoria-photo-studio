"""Local photo restoration studio. Python 3.10+, no third-party dependencies."""
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import json, sqlite3, uuid, base64, urllib.request, urllib.parse, argparse, os, re, math
from contextlib import contextmanager
ROOT=Path(__file__).resolve().parent
DATA=Path(os.environ.get('STUDIO_DATA',ROOT/'data')); DATA.mkdir(exist_ok=True,parents=True)
FILES=DATA/'photos'; FILES.mkdir(exist_ok=True)

@contextmanager
def db():
    c=sqlite3.connect(DATA/'studio.db'); c.row_factory=sqlite3.Row
    c.execute('CREATE TABLE IF NOT EXISTS photos(id TEXT PRIMARY KEY,name TEXT,client TEXT DEFAULT "",price REAL DEFAULT 0,status TEXT DEFAULT "new",original TEXT,result TEXT,created TEXT DEFAULT CURRENT_TIMESTAMP)')
    c.execute('CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY, value TEXT)')
    try:
        with c:
            yield c
    finally:
        c.close()

def price(value):
    amount=float(value)
    if not math.isfinite(amount) or amount<0: raise ValueError('Стоимость должна быть положительным числом')
    return round(amount,2)

def save_image(value, name):
    if not isinstance(value,str) or not re.match(r'^data:image/(jpeg|png|webp);base64,',value): raise ValueError('Поддерживаются JPG, PNG и WebP')
    header,raw=value.split(',',1); content=base64.b64decode(raw,validate=True)
    if len(content)>25*1024*1024: raise ValueError('Максимальный размер — 25 МБ')
    ext={'image/jpeg':'jpg','image/png':'png','image/webp':'webp'}[header[5:].split(';')[0]]
    signature=content.startswith(b'\xff\xd8\xff') if ext=='jpg' else content.startswith(b'\x89PNG\r\n\x1a\n') if ext=='png' else content[:4]==b'RIFF' and content[8:12]==b'WEBP'
    if not signature: raise ValueError('Некорректный файл изображения')
    path=f'{name}.{ext}'; (FILES/path).write_bytes(content); return '/photos/'+path

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def reply(self,value,status=200):
        data=json.dumps(value,ensure_ascii=False).encode(); self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(data))); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        path=urllib.parse.urlparse(self.path).path
        if path=='/api/photos':
            with db() as c: self.reply([dict(r) for r in c.execute('SELECT * FROM photos ORDER BY created DESC,rowid DESC')]); return
        if path=='/api/settings':
            with db() as c:
                r=c.execute('SELECT value FROM settings WHERE id=1').fetchone(); s=json.loads(r[0]) if r else {}
            s['has_key']=bool(s.pop('key','')); self.reply(s); return
        if path=='/api/health': self.reply({'ok':True}); return
        if path.startswith('/photos/'): base=FILES; relative=path[8:]
        else: base=ROOT/'static'; relative=path.lstrip('/') or 'index.html'
        file=(base/relative).resolve()
        if not file.is_relative_to(base.resolve()) or not file.is_file(): self.reply({'error':'Не найдено'},404); return
        mime={'.html':'text/html; charset=utf-8','.css':'text/css','.js':'application/javascript','.png':'image/png','.jpg':'image/jpeg','.webp':'image/webp','.svg':'image/svg+xml'}.get(file.suffix,'application/octet-stream')
        data=file.read_bytes(); self.send_response(200); self.send_header('Content-Type',mime); self.send_header('Content-Length',str(len(data))); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(data)
    def do_POST(self):
        try:
            origin=self.headers.get('Origin')
            if origin and urllib.parse.urlparse(origin).netloc != self.headers.get('Host'):
                self.reply({'error':'Запрос из другого сайта запрещён'},403); return
            if self.headers.get('Content-Type','').split(';')[0] != 'application/json':
                self.reply({'error':'Ожидается JSON'},415); return
            length=int(self.headers.get('Content-Length',0))
            if length<=0: raise ValueError('Пустой запрос')
            if length>72*1024*1024: self.reply({'error':'Запрос слишком большой'},413); return
            body=json.loads(self.rfile.read(length)); path=self.path
            if not isinstance(body,dict): raise ValueError('Ожидается JSON объект')
            if 'price' in body: body['price']=price(body['price'])
            with db() as c:
                if path=='/api/photos':
                    ident=uuid.uuid4().hex; original=save_image(body['image'],ident+'-original')
                    c.execute('INSERT INTO photos(id,name,original,client,price) VALUES(?,?,?,?,?)',(ident,str(body.get('name','Фотография'))[:200],original,str(body.get('client',''))[:200],body.get('price',0)))
                    self.reply(dict(c.execute('SELECT * FROM photos WHERE id=?',(ident,)).fetchone()),201)
                elif path=='/api/settings':
                    old=c.execute('SELECT value FROM settings WHERE id=1').fetchone(); old=json.loads(old[0]) if old else {}
                    endpoint=str(body.get('endpoint','')).strip()
                    if endpoint and urllib.parse.urlparse(endpoint).scheme not in ('http','https'): raise ValueError('Укажите HTTP или HTTPS адрес обработчика')
                    settings={'endpoint':endpoint,'key':body.get('key') or old.get('key',''),'name':str(body.get('name','AI-обработчик'))[:100]}
                    c.execute('INSERT OR REPLACE INTO settings VALUES(1,?)',(json.dumps(settings),)); self.reply({'ok':True})
                elif path.startswith('/api/photos/'):
                    bits=path.split('/'); ident=bits[3]; row=c.execute('SELECT * FROM photos WHERE id=?',(ident,)).fetchone()
                    if not row: self.reply({'error':'Фото не найдено'},404); return
                    if len(bits)>4 and bits[4]=='external':
                        r=c.execute('SELECT value FROM settings WHERE id=1').fetchone(); s=json.loads(r[0]) if r else {}
                        if not s.get('endpoint'): raise ValueError('Сначала подключите AI-обработчик в настройках')
                        source=FILES/row['original'].split('/')[-1]; mime={'.jpg':'jpeg','.png':'png','.webp':'webp'}[source.suffix]
                        payload={'image':'data:image/'+mime+';base64,'+base64.b64encode(source.read_bytes()).decode(),'operations':body.get('operations',['restore','colorize']),'photo_id':ident}
                        headers={'Content-Type':'application/json'}
                        if s.get('key'): headers['Authorization']='Bearer '+s['key']
                        req=urllib.request.Request(s['endpoint'],data=json.dumps(payload).encode(),headers=headers)
                        with urllib.request.urlopen(req,timeout=120) as response:
                            raw=response.read(40*1024*1024+1)
                        if len(raw)>40*1024*1024: raise ValueError('Ответ AI-обработчика слишком большой')
                        result=save_image(json.loads(raw)['image'],ident+'-result'); c.execute('UPDATE photos SET result=?,status="ready" WHERE id=?',(result,ident)); self.reply({'result':result})
                    elif len(bits)>4 and bits[4]=='delete':
                        c.execute('DELETE FROM photos WHERE id=?',(ident,))
                        for key in ('original','result'):
                            if row[key]: (FILES/row[key].split('/')[-1]).unlink(missing_ok=True)
                        self.reply({'ok':True})
                    else:
                        if body.get('image'):
                            result=save_image(body['image'],ident+'-result'); c.execute('UPDATE photos SET result=?,status="ready" WHERE id=?',(result,ident))
                        if 'client' in body: c.execute('UPDATE photos SET client=?,price=? WHERE id=?',(str(body['client'])[:200],body.get('price',0),ident))
                        if body.get('status') in ('new','ready','delivered'): c.execute('UPDATE photos SET status=? WHERE id=?',(body['status'],ident))
                        self.reply(dict(c.execute('SELECT * FROM photos WHERE id=?',(ident,)).fetchone()))
                else: self.reply({'error':'Не найдено'},404)
        except Exception as e:
            self.reply({'error':str(e) if isinstance(e,(ValueError,KeyError)) else 'Обработка не завершилась. Проверьте подключение и формат ответа сервиса.'},400)

if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--port',type=int,default=8000); parser.add_argument('--host',default='127.0.0.1'); args=parser.parse_args()
    with db(): pass
    httpd=ThreadingHTTPServer((args.host,args.port),Handler)
    print(f'Photo Studio: http://{args.host}:{args.port}',flush=True)
    try: httpd.serve_forever()
    except KeyboardInterrupt: pass
    finally: httpd.server_close()
