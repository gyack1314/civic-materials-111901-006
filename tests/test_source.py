"""本地来源契约检查，不包含尚未实现的采集逻辑。"""
import gzip
import http.client
import json
from pathlib import Path
import sys
import unittest
from urllib.parse import urlsplit
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from source.server import running_source

class SourceContract(unittest.TestCase):
    def request(self,url,payload=None,headers=None):
        parts=urlsplit(url);conn=http.client.HTTPConnection(parts.hostname,parts.port,timeout=10)
        body=json.dumps(payload).encode() if payload is not None else None
        conn.request('POST' if payload is not None else 'GET',parts.path+('?' + parts.query if parts.query else ''),body,headers or {})
        response=conn.getresponse();result=(response.status,dict(response.getheaders()),response.read());conn.close();return result
    def get(self,url,headers=None):return self.request(url,headers=headers)
    def post(self,url,payload):
        status,headers,body=self.request(url,payload)
        self.assertLess(status,300)
        return json.loads(body)
    def test_observable_source(self):
        with running_source() as (base,source):
            self.assertEqual(self.get(base+'/health')[0],200)
            self.post(base+'/__admin/advance',{'seconds':2})
            self.assertEqual(json.loads(self.get(base+'/health')[2])['time'],2)
            self.assertGreaterEqual(len(json.loads(self.get(base+'/__admin/log')[2])),3)

    def test_generation_and_raw_mime(self):
        from email import policy
        from email.parser import BytesParser
        with running_source() as (base,s):
            page=json.loads(self.get(base+'/mailbox?generation=20')[2]);self.assertGreater(len(page['messages']),1)
            raw=self.get(base+page['messages'][0]['raw'])[2];msg=BytesParser(policy=policy.default).parsebytes(raw);self.assertTrue(msg.is_multipart())
            self.post(base+'/__admin/advance',{'phase':1});self.assertEqual(self.get(base+'/mailbox?generation=20')[0],409)
            self.assertTrue(json.loads(self.get(base+'/mailbox?generation=21')[2])['messages'])

if __name__=='__main__':unittest.main()
