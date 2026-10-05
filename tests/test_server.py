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
if __name__=='__main__':unittest.main()
