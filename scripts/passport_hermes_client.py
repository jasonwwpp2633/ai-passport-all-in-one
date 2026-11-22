"""Bounded device-protocol client, used by the simulator's remote mode."""
import json
from pathlib import Path
import threading
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid

from passport_hermes import HermesAssistant, MAX_PROMPT


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the device credential to a redirect destination.


class RemoteHermes:
    def __init__(self, directory, url, token_file, poll_interval=2):
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
            raise ValueError('Use an HTTPS origin without path, query or credentials')
        if parsed.scheme != 'https' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
            raise ValueError('Non-local gateways require HTTPS')
        self.url = url.rstrip('/')
        self.token = Path(token_file).read_text().strip()
        if len(self.token) < 32 or not self.token.isascii() or any(c.isspace() for c in self.token):
            raise ValueError('Invalid device credential file')
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.poll_interval = poll_interval
        self.instance = uuid.uuid4().hex
        self.session = uuid.uuid4().hex
        self.heard = self.answer = ''
        self.state = '远程模式 · 按确认开始'
        self.turn = 0
        self.busy = self.closed = False
        self.last_key = None
        self.pending = None
        self.next_cursor = None
        self.cursor = 0
        self.clipped = False
        self.update = None
        self.worker = None
        try:
            saved = json.loads((self.directory/'remote.json').read_text())
            if saved['origin'] != self.url:
                raise ValueError('Use separate client state for a different gateway')
            self.session = saved['session']
            self.pending = saved['pending']
            self.heard = saved.get('heard', '')
            self.turn = saved.get('turn', 0)
            if self.pending:
                self.state = '正在恢复远程任务'
                self.busy = True
        except FileNotFoundError:
            pass
        self._save()

    def _save(self):
        path = self.directory/'remote.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(dict(origin=self.url, session=self.session, pending=self.pending,
                                             heard=self.heard, turn=self.turn), ensure_ascii=False))
        temporary.chmod(0o600)
        temporary.replace(path)

    def snapshot(self):
        with self.lock:
            payload = HermesAssistant.snapshot(self)
            # Preserve the entire fetched page; shorten only the prompt preview if necessary.
            payload['hermes']['text'] = self.answer
            payload['hermes']['locked'] = bool(self.pending and not self.busy)
            while len(json.dumps(payload, ensure_ascii=False).encode()) > 1700:
                payload['hermes']['heard'] = payload['hermes']['heard'][:-16]
            payload['hermes']['truncated'] |= self.next_cursor is not None or self.clipped
            return payload

    def call(self, path, body=None):
        request = Request(self.url+path, data=None if body is None else json.dumps(body, ensure_ascii=False).encode(),
                          headers={'Authorization':'Bearer '+self.token, 'Content-Type':'application/json'})
        with self.opener.open(request, timeout=12) as response:
            raw = response.read(2049)
        if len(raw) > 2048:
            raise ValueError('Device response exceeds 2048 bytes')
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get('status') not in ('queued','running','waiting_for_approval','completed','failed','cancelled','interrupted','unknown'):
            raise ValueError('Invalid device response')
        if data.get('task_id') != self.pending or not isinstance(data.get('text'), str) or len(data['text'].encode()) > 1152:
            raise ValueError('Invalid task payload')
        cursor = data.get('next_cursor')
        if cursor is not None and (type(cursor) is not int or not self.cursor < cursor <= 262144):
            raise ValueError('Invalid reply cursor')
        return data

    def watch(self, update):
        with self.lock:
            self.update = update
            if self.pending and (self.worker is None or not self.worker.is_alive()):
                self.busy = True
                self.worker = threading.Thread(target=self._poll, daemon=True)
                self.worker.start()

    def _notify(self):
        if self.update and not self.closed:
            self.update(self.snapshot())

    def submit(self, text, key, update=None, turn=0):
        text = str(text).strip()
        if not text or len(text.encode()) > MAX_PROMPT:
            raise ValueError('消息不能为空，且最多 383 字节')
        with self.lock:
            if self.closed or self.busy or self.pending or key == self.last_key:
                if self.pending and not self.busy:
                    self.state = '原任务待确认，未发送新消息'
                return False
            self.heard, self.answer, self.state = text, '', '正在提交远程任务'
            self.pending = uuid.uuid4().hex
            self.next_cursor = None
            self.cursor = 0
            self.turn, self.last_key, self.update = turn, key, update
            self.busy = True
            self._save()  # Commit request identity before any network activity.
            self.worker = threading.Thread(target=self._submit, daemon=True)
            self.worker.start()
        return True

    def _submit(self):
        try:
            self.call('/device/tasks', dict(request_id=self.pending, session_id=self.session, text=self.heard))
        except Exception:
            # Acceptance may have been lost. Query the same identity; never create another task.
            pass
        self._poll()

    def _poll(self):
        while not self.stop.is_set():
            try:
                result = self.call('/device/tasks/'+self.pending+'?cursor='+str(self.cursor))
                status = result['status']
                with self.lock:
                    self.state = {'queued':'远程任务排队中','running':'Hermes 正在处理',
                                  'waiting_for_approval':'等待电脑端批准','completed':'Hermes 已回复',
                                  'failed':'Hermes 任务失败','cancelled':'Hermes 任务已取消',
                                  'interrupted':'任务中断，请在电脑确认','unknown':'任务状态待确认'}[status]
                    self.busy = status in ('queued','running','waiting_for_approval')
                    if not self.busy:
                        self.answer = result['text'] or ('请在电脑确认原任务，避免重复安排。' if status in ('unknown','interrupted') else self.state)
                        self.next_cursor = result['next_cursor']
                        self.clipped = bool(result.get('clipped'))
                        self.task_id = self.pending
                        if status not in ('unknown','interrupted'):
                            self.pending = None
                        self._save()
                self._notify()
                if not self.busy:
                    return
            except HTTPError as error:
                with self.lock:
                    if error.code in (401,403,404):
                        self.state = '远程认证失败' if error.code in (401,403) else '原任务未找到，请在电脑确认'
                        self.busy = False
                        self.answer = '未重新提交任务。请检查远程服务与原任务记录。'
                        self._notify()
                        return
                    self.state = '远程暂不可达，正在查询原任务'
                self._notify()
            except Exception:
                with self.lock:
                    self.state = '连接中断，正在查询原任务'
                self._notify()
            self.stop.wait(self.poll_interval)

    def next_page(self, update=None):
        with self.lock:
            if self.busy or self.next_cursor is None or self.closed:
                return False
            self.pending = self.task_id
            self.cursor = self.next_cursor
            self.busy = True
            self.update = update or self.update
            self.worker = threading.Thread(target=self._poll, daemon=True)
            self.worker.start()
            return True

    def new_chat(self):
        with self.lock:
            if self.busy or self.pending:
                return False
            self.session = uuid.uuid4().hex
            self.heard = self.answer = ''
            self.state = '远程模式 · 已开始新对话'
            self.turn, self.last_key = 0, None
            self.next_cursor = None
            self.cursor = 0
            self._save()
            return True

    def close(self):
        self.closed = True
        self.stop.set()
        if self.worker and self.worker is not threading.current_thread():
            self.worker.join(15)
