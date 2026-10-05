"""HTTP request/response tests using an in-memory transport, no network permissions."""
import unittest, tempfile, os, json, io, base64, shutil
from unittest.mock import patch
from email.message import Message
os.environ['STUDIO_DATA']=tempfile.mkdtemp(prefix='memoria-test-')
import server
PNG='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII='
class Transport:
    def __init__(self,request):self.incoming=io.BytesIO(request);self.outgoing=io.BytesIO()
    def makefile(self,*args,**kwargs):return self.incoming
    def sendall(self,data):self.outgoing.write(data)
class StudioTest(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):shutil.rmtree(server.DATA)
    def raw(self,path,data=None,extra_headers=None):
        payload=json.dumps(data).encode() if data is not None else b''
        headers={'Host':'127.0.0.1:8000','Content-Type':'application/json','Content-Length':str(len(payload)),**(extra_headers or {})}
        raw=(('POST' if data is not None else 'GET')+' '+path+' HTTP/1.0\r\n'+''.join(k+': '+v+'\r\n' for k,v in headers.items())+'\r\n').encode()+payload
        t=Transport(raw);server.Handler(t,('127.0.0.1',9999),None)
        head,body=t.outgoing.getvalue().split(b'\r\n\r\n',1)
        self.last_headers=head
        return int(head.split(b' ')[1]),body
    def request(self,path,data=None):
        status,body=self.raw(path,data);self.assertLess(status,400,body.decode());return json.loads(body)
    def test_full_photo_lifecycle(self):
        p=self.request('/api/photos',{'image':PNG,'name':'Архив.png'});original=p['original']
        p=self.request('/api/photos/'+p['id'],{'image':PNG,'client':'Анна','price':1500})
        self.assertEqual(p['status'],'ready');self.assertEqual(p['original'],original);self.assertEqual(p['price'],1500)
        p=self.request('/api/photos/'+p['id'],{'status':'delivered'});self.assertEqual(p['status'],'delivered')
        status,content=self.raw(original);self.assertEqual(status,200);self.assertTrue(content.startswith(b'\x89PNG'))
        self.request('/api/photos/'+p['id']+'/delete',{})
        self.assertNotIn(p['id'],[x['id'] for x in self.request('/api/photos')]);self.assertFalse((server.FILES/original.split('/')[-1]).exists())
    def test_invalid_image_rejected(self):
        status,_=self.raw('/api/photos',{'image':'data:image/png;base64,'+base64.b64encode(b'not image').decode()});self.assertEqual(status,400)
    def test_invalid_price_has_no_orphan(self):
        before=set(server.FILES.iterdir());status,_=self.raw('/api/photos',{'image':PNG,'price':'invalid'})
        self.assertEqual(status,400);self.assertEqual(before,set(server.FILES.iterdir()))
    def test_settings_key_not_exposed(self):
        self.request('/api/settings',{'endpoint':'http://127.0.0.1:8189/process','key':'test-secret'})
        s=self.request('/api/settings');self.assertTrue(s['has_key']);self.assertNotIn('key',s)
        self.request('/api/settings',{'endpoint':'http://127.0.0.1:8189/process','key':''});self.assertTrue(self.request('/api/settings')['has_key'])
    def test_external_contract(self):
        self.request('/api/settings',{'endpoint':'http://127.0.0.1:8189/process','key':'test-secret'})
        p=self.request('/api/photos',{'image':PNG,'name':'AI.png'})
        def mock_open(req,timeout):
            self.assertEqual(timeout,120);self.assertEqual(req.full_url,'http://127.0.0.1:8189/process')
            payload=json.loads(req.data);self.assertEqual(payload['operations'],['restore','colorize']);self.assertEqual(payload['image'],PNG)
            self.assertEqual(req.get_header('Authorization'),'Bearer test-secret')
            return io.BytesIO(json.dumps({'image':PNG}).encode())
        with patch('server.urllib.request.urlopen',side_effect=mock_open):result=self.request('/api/photos/'+p['id']+'/external',{'operations':['restore','colorize']})
        self.assertTrue(result['result'].endswith('.png'))
        photo=next(x for x in self.request('/api/photos') if x['id']==p['id']);self.assertEqual(photo['status'],'ready')
        self.request('/api/photos/'+p['id']+'/delete',{})
    def test_external_failure_preserves_original(self):
        self.request('/api/settings',{'endpoint':'http://127.0.0.1:8189/process'})
        p=self.request('/api/photos',{'image':PNG,'name':'AI.png'})
        with patch('server.urllib.request.urlopen',side_effect=TimeoutError):status,_=self.raw('/api/photos/'+p['id']+'/external',{})
        self.assertEqual(status,400);self.assertEqual(self.raw(p['original'])[0],200)
        photo=next(x for x in self.request('/api/photos') if x['id']==p['id']);self.assertEqual(photo['status'],'new');self.assertIsNone(photo['result'])
        self.request('/api/photos/'+p['id']+'/delete',{})
    def test_path_traversal_rejected(self):self.assertEqual(self.raw('/photos/../../server.py')[0],404)
    def test_static_application_available(self):
        for path in ['/','/app.js','/style.css','/demo-0.svg']:self.assertEqual(self.raw(path)[0],200)
    def test_cross_origin_write_rejected(self):
        self.assertEqual(self.raw('/api/settings',{'endpoint':''},{'Origin':'https://unrelated.example'})[0],403)
    def setUp(self):
        import job_queue
        job_queue._stop.clear()
        with server.db() as c:
            for table in ('jobs','versions','photos','orders','settings'):c.execute('DELETE FROM '+table)
        for file in server.FILES.iterdir():
            if file.is_file():file.unlink()

    def make_photo(self,name='Photo.png'):
        return self.request('/api/photos',{'image':PNG,'name':name})

    def configure_external(self):
        self.request('/api/settings',{'endpoint':'http://127.0.0.1:8189/process'})

    def test_versions_are_immutable_and_restorable(self):
        p=self.make_photo();original=server.image_path(p['original']).read_bytes()
        first=self.request('/api/photos/'+p['id'],{'image':PNG,'label':'Первая'})
        second=self.request('/api/photos/'+p['id'],{'image':PNG,'label':'Вторая'})
        self.assertNotEqual(first['result'],second['result'])
        versions=self.request('/api/photos/'+p['id']+'/versions');self.assertEqual(len(versions),3)
        restored=self.request('/api/photos/'+p['id']+'/restore',{'version_id':versions[1]['id']})
        self.assertEqual(restored['result'],first['result'])
        self.assertTrue(server.image_path(second['result']).is_file())
        self.request('/api/photos/'+p['id']+'/restore',{'version_id':'original'})
        self.assertEqual(server.image_path(p['original']).read_bytes(),original)
        self.assertEqual(len(self.request('/api/photos/'+p['id']+'/versions')),3)

    def test_grouped_order_and_delivery_validation(self):
        a,b=self.make_photo('A.png'),self.make_photo('B.png')
        order=self.request('/api/orders',{'name':'Архив','client':'Анна','total':5000,'deposit':1500,'photo_ids':[a['id'],b['id']]})
        self.assertEqual(self.request('/api/orders')[0]['remaining'],3500)
        self.assertEqual(len(self.request('/api/orders')[0]['photos']),2)
        self.assertEqual(self.raw('/api/orders/'+order['id'],{'status':'delivered'})[0],400)
        self.assertEqual(self.request('/api/orders')[0]['status'],'new')
        for p in (a,b):self.request('/api/photos/'+p['id'],{'image':PNG})
        self.request('/api/orders/'+order['id'],{'status':'delivered'})
        self.assertTrue(all(p['status']=='delivered' for p in self.request('/api/photos')))

    def test_invalid_order_assignment_is_atomic(self):
        p=self.make_photo();one=self.request('/api/orders',{'name':'One','photo_ids':[p['id']]})
        self.assertEqual(self.raw('/api/orders',{'name':'Two','photo_ids':[p['id']]})[0],400)
        orders=self.request('/api/orders');self.assertEqual(len(orders),1);self.assertEqual(orders[0]['id'],one['id'])

    def test_zip_selection_and_originals(self):
        import zipfile
        a,b=self.make_photo('A.png'),self.make_photo('B.png')
        self.request('/api/photos/'+a['id'],{'image':PNG})
        status,content=self.raw('/api/export?ids='+a['id']+','+b['id']);self.assertEqual(status,200)
        with zipfile.ZipFile(io.BytesIO(content)) as bundle:
            self.assertEqual(len(bundle.namelist()),2);self.assertIn('manifest.csv',bundle.namelist())
        status,content=self.raw('/api/export?source=original')
        with zipfile.ZipFile(io.BytesIO(content)) as bundle:self.assertEqual(len(bundle.namelist()),3)

    def test_queue_cancel_retry_and_duplicate(self):
        p=self.make_photo();self.configure_external()
        job=self.request('/api/jobs',{'photo_ids':[p['id']],'engine':'external','preset':'color'})['jobs'][0]
        self.assertEqual(self.raw('/api/jobs',{'photo_ids':[p['id']],'engine':'external'})[0],400)
        self.assertEqual(self.raw('/api/photos/'+p['id'],{'image':PNG})[0],400)
        self.request('/api/jobs/'+job['id']+'/cancel',{})
        self.assertEqual(self.request('/api/jobs')[0]['status'],'cancelled')
        self.request('/api/jobs/'+job['id']+'/retry',{})
        self.assertEqual(self.request('/api/jobs')[0]['status'],'queued')

    def test_claim_is_atomic_between_devices(self):
        import job_queue
        from concurrent.futures import ThreadPoolExecutor
        self.configure_external()
        ids=[self.make_photo(str(i)+'.png')['id'] for i in range(8)]
        self.request('/api/jobs',{'photo_ids':ids,'engine':'external'})
        with ThreadPoolExecutor(max_workers=4) as pool:
            claimed=list(pool.map(lambda i:job_queue.claim('cuda:'+str(i%2)),range(8)))
        self.assertEqual(len({j['id'] for j in claimed}),8)
        self.assertIsNone(job_queue.claim('cpu'))

    def test_restart_recovers_only_interrupted_jobs(self):
        import job_queue
        self.configure_external();a,b=self.make_photo(),self.make_photo()
        jobs=self.request('/api/jobs',{'photo_ids':[a['id'],b['id']],'engine':'external'})['jobs']
        with server.db() as c:
            c.execute("UPDATE jobs SET status='running',cancel=0 WHERE id=?",(jobs[0]['id'],))
            c.execute("UPDATE jobs SET status='running',cancel=1 WHERE id=?",(jobs[1]['id'],))
        job_queue.recover();states={j['id']:j['status'] for j in self.request('/api/jobs')}
        self.assertEqual(states[jobs[0]['id']],'queued');self.assertEqual(states[jobs[1]['id']],'cancelled')

    def mock_local_job(self):
        import job_queue
        p=self.make_photo()
        models=[{'id':key,'downloaded':True} for key in job_queue.CATALOG]
        with patch('local_runtime.probe',return_value={'pillow':True,'installed':True}),patch('local_runtime.catalog_status',return_value=models):
            job=self.request('/api/jobs',{'photo_ids':[p['id']],'engine':'local','preset':'color'})['jobs'][0]
        return p,job_queue.claim('cuda:0')

    def mock_outputs(self,job,*args):
        import job_queue
        path=job_queue.WORK/job['id'];path.mkdir(exist_ok=True)
        result=[]
        for i in range(2):
            file=path/(str(i)+'.png');file.write_bytes(base64.b64decode(PNG.split(',')[1]))
            result.append({'file':str(file),'label':'Stage '+str(i)})
        return result

    def test_queue_saves_each_stage_and_preserves_original(self):
        import job_queue
        p,job=self.mock_local_job();original=server.image_path(p['original']).read_bytes()
        with patch('local_runtime.run_job',side_effect=self.mock_outputs):job_queue.execute(job,'cuda:0')
        versions=self.request('/api/photos/'+p['id']+'/versions');self.assertEqual(len(versions),3)
        self.assertEqual(self.request('/api/jobs')[0]['status'],'completed')
        self.assertEqual(self.request('/api/photos')[0]['result'],versions[-1]['image'])
        self.assertEqual(server.image_path(p['original']).read_bytes(),original)

    def test_cancelled_job_never_publishes_result(self):
        import job_queue
        p,job=self.mock_local_job()
        def cancelled_output(*args):
            job_queue.cancel(job['id']);return self.mock_outputs(*args)
        with patch('local_runtime.run_job',side_effect=cancelled_output):job_queue.execute(job,'cuda:0')
        self.assertIsNone(self.request('/api/photos')[0]['result'])
        self.assertEqual(len(self.request('/api/photos/'+p['id']+'/versions')),1)
        self.assertEqual(self.request('/api/jobs')[0]['status'],'cancelled')

    def test_local_models_are_required_before_enqueue(self):
        import job_queue
        p=self.make_photo()
        with patch('local_runtime.probe',return_value={'pillow':True,'installed':True}),patch('local_runtime.catalog_status',return_value=[{'id':key,'downloaded':False} for key in job_queue.CATALOG]):
            status,_=self.raw('/api/jobs',{'photo_ids':[p['id']],'preset':'color','engine':'local'})
        self.assertEqual(status,400);self.assertEqual(self.request('/api/jobs'),[])

    def test_login_protects_data_and_photos(self):
        p=self.make_photo()
        with patch('server.TOKEN','test-only-token'):
            self.assertEqual(self.raw('/api/photos')[0],401)
            self.assertEqual(self.raw(p['original'])[0],401)
            self.assertEqual(self.raw('/api/login',{'token':'wrong'})[0],401)
            status,_=self.raw('/api/login',{'token':'test-only-token'});self.assertEqual(status,200)
            cookie=next(line.split(b': ',1)[1].split(b';',1)[0] for line in self.last_headers.split(b'\r\n') if line.startswith(b'Set-Cookie:')).decode()
            self.assertEqual(self.raw('/api/photos',extra_headers={'Cookie':cookie})[0],200)

    def test_old_database_migration_is_additive_and_idempotent(self):
        import sqlite3,storage
        with tempfile.TemporaryDirectory() as directory:
            path=server.Path(directory)
            with sqlite3.connect(path/'studio.db') as c:
                c.execute('CREATE TABLE photos(id TEXT PRIMARY KEY,name TEXT,client TEXT DEFAULT "",price REAL DEFAULT 0,status TEXT DEFAULT "new",original TEXT,result TEXT,created TEXT DEFAULT CURRENT_TIMESTAMP)')
                c.execute("INSERT INTO photos(id,name,original,result) VALUES('old','Old','/photos/original.png','/photos/result.png')")
            with patch('storage.DATA',path):
                storage.initialize();storage.initialize()
                with storage.db() as c:
                    self.assertEqual(c.execute('SELECT COUNT(*) FROM versions').fetchone()[0],1)
                    self.assertIn('order_id',[r['name'] for r in c.execute('PRAGMA table_info(photos)')])
                    self.assertEqual(c.execute('SELECT result FROM photos').fetchone()[0],'/photos/result.png')

    def test_device_configuration_rejects_duplicates(self):
        self.assertEqual(self.raw('/api/settings',{'devices':['cuda:0','cuda:0']})[0],400)
        self.assertEqual(self.raw('/api/settings',{'devices':['auto','cpu']})[0],400)
        self.assertEqual(self.request('/api/settings')['devices'],['auto'])

    def test_cancelled_model_download_preserves_previous_file(self):
        import local_runtime,storage
        dest=storage.MODELS/'RealESRGAN_x2plus.pth';dest.write_bytes(b'previous')
        class Response(io.BytesIO):
            headers={'Content-Length':str(2*1024*1024)}
        stop=[False]
        def progress(*args):stop[0]=True
        with patch('local_runtime.urllib.request.urlopen',return_value=Response(b'x'*(2*1024*1024))):
            with self.assertRaises(local_runtime.Cancelled):local_runtime.download_model('esrgan2',progress,lambda:stop[0])
        self.assertEqual(dest.read_bytes(),b'previous');self.assertFalse(dest.with_suffix('.pth.part').exists())

if __name__=='__main__':unittest.main()

