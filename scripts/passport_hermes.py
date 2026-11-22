"""Local Hermes CLI bridge shared by the companion and the interactive simulator."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import uuid

MAX_PROMPT = 383
MAX_REPLY = 1152  # Keep the complete service envelope below the badge's 2048-byte queue item.


def bounded(text, size):
    return str(text).encode('utf-8')[:size].decode('utf-8', errors='ignore')


class HermesAssistant:
    def __init__(self, directory, command=None, timeout=180):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        self.command = command or shutil.which('hermes') or str(Path.home()/'.local/bin/hermes')
        self.timeout = timeout
        self.lock = threading.RLock()
        self.process = None
        self.busy = False
        self.closed = False
        self.session = ''
        self.heard = self.answer = ''
        self.state = '按确认开始对话'
        self.turn = 0
        self.last_key = None
        self.instance = uuid.uuid4().hex
        try:
            saved = json.loads((self.directory/'session.json').read_text())
            self.session = str(saved.get('session_id') or '')
            if saved.get('pending'):
                self.state = '上次任务状态待确认'
                self.answer = '助手曾退出，请在电脑 Hermes 确认任务状态；不会自动重复执行。'
        except (OSError, ValueError, TypeError):
            pass

    def _save(self, pending=False):
        path = self.directory/'session.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'session_id':self.session, 'pending':pending}))
        temporary.chmod(0o600)
        temporary.replace(path)

    def snapshot(self):
        with self.lock:
            payload = {'state':self.state, 'value':'', 'lines':[],
                       'hermes':{'heard':bounded(self.heard, MAX_PROMPT), 'text':bounded(self.answer, MAX_REPLY),
                                 'busy':self.busy, 'truncated':len(self.answer.encode('utf-8'))>MAX_REPLY,
                                 'turn':self.turn}}
            # Escaped quotes, newlines and tool output also count towards the firmware queue's byte limit.
            while len(json.dumps(payload,ensure_ascii=False).encode('utf-8'))>1700:
                field = 'text' if payload['hermes']['text'] else 'heard'
                value = payload['hermes'][field]
                payload['hermes'][field] = bounded(value,max(0,len(value.encode('utf-8'))-128))
                payload['hermes']['truncated'] = True
            return payload

    def new_chat(self):
        with self.lock:
            if self.busy:
                return False
            self.session = self.heard = self.answer = ''
            self.state = '已开始新对话'
            self.last_key = None
            self.turn = 0
            self._save()
            return True

    def submit(self, text, key, update=None, turn=0):
        text = str(text).strip()
        if not text or len(text.encode('utf-8')) > MAX_PROMPT:
            raise ValueError('消息不能为空，且最多 383 字节')
        with self.lock:
            if self.closed or self.busy or key == self.last_key:
                return False
            self.busy = True
            self.last_key = key
            self.turn = turn
            self.heard, self.answer, self.state = text, '', 'Hermes 正在处理'
            self._save(pending=True)
            self.worker = threading.Thread(target=self._run, args=(text, update), daemon=True)
            self.worker.start()
        return True

    def _run(self, text, update):
        timed_out = threading.Event()
        timer = None
        stderr = None
        try:
            args = [self.command, 'chat', '--query-file', '-', '--oneshot', '--format', 'stream-json', '--source', 'cli']
            if self.session:
                args += ['--resume', self.session]
            # No shell, permission bypass flags, provider overrides, or credential copying.
            environment = dict(os.environ)
            for key in ('HERMES_YOLO_MODE', 'HERMES_ACCEPT_HOOKS'):
                environment.pop(key, None)
            stderr = (self.directory/'last-error.log').open('w')
            os.chmod(stderr.name, 0o600)
            process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                                       text=True, encoding='utf-8', cwd=self.directory, env=environment)
            with self.lock:
                self.process = process
                if self.closed:
                    process.terminate()
            def timeout():
                timed_out.set()
                if process.poll() is None:
                    process.terminate()
            timer = threading.Timer(self.timeout, timeout)
            timer.daemon = True
            timer.start()
            process.stdin.write(text)
            process.stdin.close()
            result = None
            for raw in process.stdout:
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                with self.lock:
                    if event.get('session_id'):
                        self.session = str(event['session_id'])
                        self._save(pending=True)
                    if event.get('type') == 'result':
                        result = event
            code = process.wait(timeout=5)
            if timed_out.is_set():
                raise TimeoutError('timeout')
            if code or result is None or result.get('exit_code', 0) or result.get('error'):
                raise RuntimeError('Hermes did not complete this turn')
            with self.lock:
                self.answer = str(result.get('text') or 'Hermes 未返回文字，请在电脑查看。')
                self.state = 'Hermes 已回复'
        except Exception as error:
            with self.lock:
                self.state = '响应超时，请在电脑确认' if isinstance(error, TimeoutError) else 'Hermes 暂不可用'
                self.answer = ('任务可能仍在 Hermes 中运行，请先在电脑确认，避免重复安排。' if isinstance(error, TimeoutError)
                               else '请检查电脑上的 Hermes 登录、网关和待批准操作，然后发送新消息。')
        finally:
            if timer:
                timer.cancel()
            with self.lock:
                if self.process and self.process.poll() is None:
                    self.process.kill()
                    self.process.wait()
                if self.process:
                    self.process.stdout.close()
                    if not self.process.stdin.closed:self.process.stdin.close()
                self.process = None
                self.busy = False
                # A timeout/interruption must remain visible after restarting the companion.
                self._save(pending=timed_out.is_set() or self.closed)
            if stderr:
                stderr.close()
            if update and not self.closed:
                update(self.snapshot())

    def close(self):
        with self.lock:
            self.closed = True
            if self.process and self.process.poll() is None:
                self.process.terminate()
