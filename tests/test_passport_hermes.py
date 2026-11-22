"""Hermes session continuity, literal prompts, bounded wire messages and failure behavior."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from passport_hermes import HermesAssistant, bounded

FAKE='''#!{python}
import sys,json,time
text=sys.stdin.read()
if text=='slow':time.sleep(5)
if text=='fail':sys.exit(7)
continued='--resume' in sys.argv
print(json.dumps({{'type':'system','session_id':'badge-test'}}),flush=True)
print(json.dumps({{'type':'result','session_id':'badge-test','exit_code':0,'text':('continued:' if continued else 'first:')+text}},ensure_ascii=False),flush=True)
'''

class HermesTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        stub=self.root/'fake hermes'
        stub.write_text(FAKE.format(python=sys.executable));stub.chmod(0o700)
        self.bridge=HermesAssistant(self.root/'data',command=str(stub),timeout=3)
    def tearDown(self):
        self.bridge.close()
        if hasattr(self.bridge,'worker'):self.bridge.worker.join(5)
        self.temp.cleanup()
    def ask(self,text,key):
        done=threading.Event()
        self.assertTrue(self.bridge.submit(text,key,lambda _:done.set(),turn=key))
        self.assertTrue(done.wait(6),'worker failed to finish')
        return self.bridge.snapshot()
    def test_literal_prompt_and_session_resume(self):
        text='安排任务 "$(touch /tmp/should-not-exist)" `echo test`\n保留原文'
        first=self.ask(text,1)
        self.assertEqual(first['hermes']['text'],'first:'+text)
        self.assertEqual(self.bridge.session,'badge-test')
        second=self.ask('刚才我说了什么？',2)
        self.assertEqual(second['hermes']['text'],'continued:刚才我说了什么？')
        self.assertFalse(self.bridge.submit('重复请求',2))
        self.assertEqual(json.loads((self.root/'data/session.json').read_text())['session_id'],'badge-test')
    def test_new_chat_and_busy_protection(self):
        self.ask('第一句',1)
        self.assertTrue(self.bridge.new_chat())
        self.assertEqual(self.ask('新消息',2)['hermes']['text'],'first:新消息')
        self.bridge.busy=True
        self.assertFalse(self.bridge.new_chat())
        self.assertFalse(self.bridge.submit('另一项任务',3))
        self.bridge.busy=False
    def test_error_does_not_claim_completion(self):
        reply=self.ask('fail',1)
        self.assertEqual(reply['state'],'Hermes 暂不可用')
        self.assertFalse(reply['hermes']['busy'])
        self.assertFalse(self.bridge.submit('fail',1))
    def test_timeout_persists_uncertainty(self):
        self.bridge.timeout=.1
        reply=self.ask('slow',1)
        self.assertIn('超时',reply['state'])
        reopened=HermesAssistant(self.root/'data',command=self.bridge.command)
        self.assertIn('状态待确认',reopened.snapshot()['state'])
        reopened.close()
    def test_utf8_and_service_packet_limit(self):
        self.assertEqual(bounded('你好',4),'你')
        self.bridge.heard='字'*127
        self.bridge.answer='答案'*1000
        self.bridge.state='Hermes 已回复'
        payload=dict(self.bridge.snapshot(),request=123,app=53,generation=456,phase='ready',epoch=1234567890)
        self.assertTrue(payload['hermes']['truncated'])
        self.assertLess(len(json.dumps(payload,ensure_ascii=False).encode()),2048)
        self.assertEqual(len(payload['hermes']['text'].encode()),1152)
        self.bridge.answer='\\"\n\x01'*1000
        payload=dict(self.bridge.snapshot(),request=123,app=53,generation=456,phase='ready',epoch=1234567890)
        self.assertLess(len(json.dumps(payload,ensure_ascii=False).encode()),2048)
        with self.assertRaises(ValueError):self.bridge.submit('字'*128,1)
    def test_companion_transfers_only_the_current_page(self):
        from passport_services import Services
        from types import SimpleNamespace
        companion=Services.__new__(Services)
        companion.active=(53,7)
        companion.hermes=self.bridge
        companion.sim=SimpleNamespace(request=lambda path:{'app':53,'generation':7,'hermes_turn':1,'hermes_text':'测试任务'})
        request={'app':53,'generation':7,'operation':'send','request':10}
        companion.response(request)
        self.bridge.worker.join(5)
        self.assertEqual(self.bridge.snapshot()['hermes']['text'],'first:测试任务')
        companion.response(request)  # Same turn is not re-executed.
        companion.sim=SimpleNamespace(request=lambda path:{'app':0,'generation':8})
        with self.assertRaises(RuntimeError):companion.response(request)

    def test_native_buffer_and_app_identity(self):
        source=self.root/'hermes.cpp'
        source.write_text('''#include "hermes.hpp"
#include "apps.hpp"
#include <cassert>
using namespace passport;
int main(){static_assert(unsigned(App::Train)==52);static_assert(unsigned(App::Hermes)==53);
static_assert(kFunctions.size()==49);static_assert(sizeof(HermesView)<2048);
char text[5];hermesText(text,sizeof(text),"你好");assert(!strcmp(text,"你"));}
''')
        target=self.root/'hermes-test'
        subprocess.run(['clang++','-std=c++20','-fsanitize=address,undefined',
            '-I'+str(ROOT/'firmware/passport_suite/main'),str(source),'-o',str(target)],check=True)
        subprocess.run([str(target)],check=True)

if __name__=='__main__':unittest.main()
