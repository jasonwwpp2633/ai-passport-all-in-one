"""Device gateway contracts: durable identity, auth, bounded paging and recovery without reexecution."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from passport_hermes_remote import TaskStore, DeviceServer, GatewayError, RunsBackend
from passport_hermes_client import RemoteHermes

TOKEN = 'x'*43


def wait(predicate, seconds=6):
    until = time.monotonic()+seconds
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('Condition timed out')


class Backend:
    def __init__(self):
        self.calls = []
        self.release = threading.Event()
        self.release.set()
        self.answer = '远程回复'

    def execute(self, job, session, save, stopped):
        self.calls.append((job, session))
        self.release.wait(3)
        save('completed', session='dedicated-api-session', output=self.answer)


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.backend = Backend()
        self.store = TaskStore(self.path/'server', self.backend)
        self.server = DeviceServer(('127.0.0.1', 0), self.store, TOKEN)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:'+str(self.server.server_port)
        self.token = self.path/'token'
        self.token.write_text(TOKEN)
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.backend.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.store.close()
        self.temp.cleanup()

    def client(self, suffix='client'):
        value = RemoteHermes(self.path/suffix, self.url, self.token, poll_interval=.02)
        self.clients.append(value)
        return value

    def body(self, text='消息', session=None):
        return dict(request_id=uuid.uuid4().hex, session_id=session or uuid.uuid4().hex, text=text)

    def request(self, path, body=None, token=TOKEN):
        return urlopen(Request(self.url+path, data=json.dumps(body).encode() if body else None,
                               headers={'Authorization':'Bearer '+token}), timeout=3)

    def test_auth_length_and_conflict(self):
        body = self.body()
        with self.assertRaises(HTTPError) as error:
            self.request('/device/tasks', body, 'wrong')
        self.assertEqual(error.exception.code, 401)
        self.assertEqual(len(self.backend.calls), 0)
        for _ in range(2):
            with self.request('/device/tasks', body) as response:
                self.assertEqual(response.status, 202)
        wait(lambda: len(self.backend.calls)==1)
        with self.assertRaises(HTTPError) as error:
            self.request('/device/tasks', dict(body, text='不同的任务'))
        self.assertEqual(error.exception.code, 409)
        with self.assertRaises(HTTPError) as error:
            self.request('/device/tasks', self.body('字'*128))
        self.assertEqual(error.exception.code, 400)

    def test_client_literal_input_continuity_and_new_chat(self):
        client = self.client()
        text = '任务 "$(echo unsafe)" `echo literal`\n只作为文字'
        self.assertTrue(client.submit(text, 1, turn=1))
        wait(lambda: not client.busy)
        self.assertEqual(client.snapshot()['hermes']['text'], '远程回复')
        self.assertEqual(self.backend.calls[0][0]['text'], text)
        self.assertFalse(client.submit(text, 1))
        client.submit('继续', 2)
        wait(lambda: len(self.backend.calls)==2 and not client.busy)
        self.assertEqual(self.backend.calls[1][1], 'dedicated-api-session')
        self.assertTrue(client.new_chat())
        client.submit('新会话', 3)
        wait(lambda: len(self.backend.calls)==3 and not client.busy)
        self.assertEqual(self.backend.calls[2][1], '')

    def test_restart_deduplicates_and_queued_runs_once(self):
        body = self.body()
        self.store.submit(body)
        wait(lambda: self.store.page(body['request_id'])['status']=='completed')
        self.store.close()
        self.store = TaskStore(self.path/'server', self.backend)
        self.server.store = self.store
        self.store.submit(body)
        time.sleep(.1)
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(self.store.page(body['request_id'])['text'], self.backend.answer)

    def test_unknown_blocks_session_and_client_does_not_resubmit(self):
        body = self.body()
        self.store.submit(body)
        wait(lambda: self.store.page(body['request_id'])['status']=='completed')
        self.store.save(body['request_id'], 'unknown')
        with self.assertRaises(GatewayError):
            self.store.submit(self.body(session=body['session_id']))
        client = self.client()
        client.pending, client.session = body['request_id'], body['session_id']
        client._save()
        client.close()
        reopened = self.client()
        reopened.watch(lambda _:None)
        wait(lambda: not reopened.busy)
        self.assertIsNotNone(reopened.pending)
        self.assertFalse(reopened.new_chat())
        self.assertFalse(reopened.submit('不要重复', 5))
        self.assertEqual(len(self.backend.calls), 1)

    def test_missing_task_and_insecure_origin(self):
        client = self.client()
        client.pending = uuid.uuid4().hex
        client.watch(lambda _:None)
        wait(lambda: not client.busy)
        self.assertIn('未找到', client.state)
        self.assertEqual(len(self.backend.calls), 0)
        for bad in ('http://example.com','https://user:secret@example.com','https://example.com/path','https://example.com?key=abc'):
            with self.assertRaises(ValueError):
                RemoteHermes(self.path/'bad', bad, self.token)

    def test_paging_escaped_json_no_missing_characters(self):
        self.backend.answer = ('中文"\\\n\x01'*500)
        client = self.client()
        client.submit('\x01'*383, 1)
        wait(lambda: not client.busy)
        pages = [client.snapshot()['hermes']['text']]
        while client.next_cursor is not None:
            self.assertLessEqual(len(json.dumps(client.snapshot(),ensure_ascii=False).encode()), 1700)
            self.assertTrue(client.next_page())
            wait(lambda: not client.busy)
            pages.append(client.snapshot()['hermes']['text'])
        self.assertEqual(''.join(pages), self.backend.answer)
        self.assertEqual(len(self.backend.calls), 1)

    def test_exclusive_store_lock_and_backend_change(self):
        with self.assertRaises(RuntimeError):
            TaskStore(self.path/'server', Backend())
        self.store.close()
        class OtherBackend(Backend):pass
        with self.assertRaises(ValueError):
            TaskStore(self.path/'server', OtherBackend())
        self.store = TaskStore(self.path/'server', self.backend)
        self.server.store = self.store

    def test_local_resolution_never_reexecutes(self):
        body = self.body()
        self.store.submit(body)
        wait(lambda: self.store.page(body['request_id'])['status']=='completed')
        self.store.save(body['request_id'], 'unknown')
        self.store.resolve(body['request_id'], 'completed')
        self.assertEqual(self.store.page(body['request_id'])['status'], 'completed')
        with self.assertRaises(ValueError):
            self.store.resolve(body['request_id'], 'failed')
        self.assertEqual(len(self.backend.calls), 1)

    def test_api_run_recovery_only_polls(self):
        backend = RunsBackend('http://127.0.0.1:8642', 'test-key')
        calls, saved = [], []
        def call(path, body=None, request_id=None):
            calls.append((path, body, request_id))
            if body:
                return {'run_id':'run_test'}
            return {'status':'completed','session_id':'api-session','output':'API reply'}
        backend.call = call
        backend.execute(dict(id='a'*32,text='query',run_id=''), '', lambda status,**kw:saved.append((status,kw)), threading.Event())
        self.assertEqual(calls[0][2], 'a'*32)
        self.assertEqual(saved[-1][1]['session'], 'api-session')
        calls.clear()
        backend.execute(dict(id='a'*32,text='query',run_id='run_test'), '', lambda *a,**kw:None, threading.Event())
        self.assertEqual(calls, [('/v1/runs/run_test',None,None)])


if __name__ == '__main__':
    unittest.main()
