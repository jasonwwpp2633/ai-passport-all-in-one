#!/usr/bin/env python3
"""Small authenticated device gateway. Default loopback listener; publish through an HTTPS tunnel."""
import argparse
import fcntl
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import urlsplit
from urllib.request import Request, ProxyHandler, build_opener
from passport_hermes_client import NoRedirect

from passport_hermes import HermesAssistant, MAX_PROMPT

ACTIVE = ('queued', 'running', 'waiting_for_approval')
TERMINAL = ('completed', 'failed', 'cancelled', 'interrupted', 'unknown')
ID = re.compile(r'^[a-f0-9]{32}$')
MAX_OUTPUT = 262144


class GatewayError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


def private_write(path, value):
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as out:
        os.fchmod(out.fileno(), 0o600)
        out.write(value)
    temporary.replace(path)


class RunsBackend:
    """Official API, selected explicitly. Never silently switches to CLI."""
    def __init__(self, base, key):
        parsed = urlsplit(base)
        if parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1', '::1') or parsed.username or parsed.query or parsed.fragment:
            raise ValueError('Hermes API must use a loopback HTTP URL')
        if not key:
            raise ValueError('Set API_SERVER_KEY for the API backend')
        self.base, self.key = base.rstrip('/'), key

    def call(self, path, body=None, request_id=None):
        headers = {'Authorization': 'Bearer '+self.key, 'Content-Type': 'application/json'}
        if request_id:
            headers['Idempotency-Key'] = request_id
        request = Request(self.base+path, data=None if body is None else json.dumps(body).encode(), headers=headers)
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=12) as response:
            data = response.read(2*1024*1024+1)
        if len(data) > 2*1024*1024:
            raise ValueError('Hermes response too large')
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError('Invalid Hermes response')
        return result

    def execute(self, job, session, save, stopped):
        run = job['run_id']
        if not run:
            body = {'input': job['text']}
            if session:
                body['session_id'] = session
            result = self.call('/v1/runs', body, job['id'])
            run = result.get('run_id') or result.get('id')
            if not isinstance(run, str) or not re.fullmatch(r'run_[a-zA-Z0-9_-]{1,120}', run):
                raise ValueError('Missing run ID')
            # Commit the accepted ID before polling. Restart only queries this ID.
            save('running', run=run, session=result.get('session_id') or session)
        while not stopped.is_set():
            result = self.call('/v1/runs/'+run)
            status = result.get('status')
            if status not in ACTIVE+TERMINAL:
                raise ValueError('Unknown run status')
            save(status, run=run, session=result.get('session_id') or session,
                 output=result.get('output') if status == 'completed' else None)
            if status in TERMINAL:
                return
            stopped.wait(2)


class CliBackend:
    """Compatibility backend for an existing local installation, without API configuration changes."""
    def __init__(self, directory, command=None, timeout=180):
        self.directory, self.command, self.timeout = directory, command, timeout

    def execute(self, job, session, save, stopped):
        bridge = HermesAssistant(self.directory/job['session'], command=self.command, timeout=self.timeout)
        done = threading.Event()
        try:
            # One gateway worker owns this directory/session. No shell or approval bypass.
            bridge.submit(job['text'], job['id'], lambda _: done.set())
            while not done.wait(.1):
                if stopped.is_set():
                    bridge.close()
                    save('unknown')
                    return
            if bridge.state == 'Hermes 已回复':
                save('completed', session=bridge.session, output=bridge.answer)
            else:
                save('unknown', session=bridge.session)
        finally:
            bridge.close()
            if hasattr(bridge, 'worker'):
                bridge.worker.join(6)


class TaskStore:
    def __init__(self, directory, backend, start_worker=True):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        self.process_lock = (self.directory/'gateway.lock').open('a')
        try:
            fcntl.flock(self.process_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.process_lock.close()
            raise RuntimeError('This gateway state is already in use')
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.directory/'tasks.sqlite3', check_same_thread=False)
        (self.directory/'tasks.sqlite3').chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, backend_id TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, session TEXT NOT NULL, text TEXT NOT NULL,
              status TEXT NOT NULL, run_id TEXT NOT NULL DEFAULT '', output TEXT NOT NULL DEFAULT '',
              clipped INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        kind = type(backend).__name__ + (':'+backend.base if isinstance(backend, RunsBackend) else '')
        previous = self.db.execute("SELECT value FROM metadata WHERE key='backend'").fetchone()
        if previous and previous[0] != kind:
            self.db.close()
            self.process_lock.close()
            raise ValueError('Use a separate state directory when changing Hermes backends')
        self.db.execute("INSERT OR IGNORE INTO metadata VALUES('backend',?)", (kind,))
        # A CLI observer or a POST without a persisted run_id cannot safely be replayed after a crash.
        self.db.execute("UPDATE tasks SET status='unknown' WHERE status IN ('running','waiting_for_approval') AND run_id='' ")
        self.db.commit()
        self.backend = backend
        self.stopped, self.wake = threading.Event(), threading.Event()
        self.worker = threading.Thread(target=self.work, daemon=True)
        if start_worker:
            self.worker.start()

    def submit(self, body):
        if not isinstance(body, dict) or set(body) != {'request_id', 'session_id', 'text'}:
            raise GatewayError(400, 'Expected request_id, session_id and text')
        identifier, session, text = (body[k] for k in ('request_id', 'session_id', 'text'))
        if not all(isinstance(x, str) for x in (identifier, session, text)) or not ID.fullmatch(identifier) or not ID.fullmatch(session):
            raise GatewayError(400, 'Invalid identifier')
        if not text.strip() or len(text.encode()) > MAX_PROMPT:
            raise GatewayError(400, 'Input must be 1-383 UTF-8 bytes')
        with self.lock:
            existing = self.db.execute('SELECT * FROM tasks WHERE id=?', (identifier,)).fetchone()
            if existing:
                if existing['session'] != session or existing['text'] != text:
                    raise GatewayError(409, 'Request ID already belongs to different input')
                return identifier
            blocked = self.db.execute("SELECT 1 FROM tasks WHERE session=? AND status IN ('queued','running','waiting_for_approval','unknown','interrupted')", (session,)).fetchone()
            if blocked:
                raise GatewayError(409, 'Session has an unfinished or uncertain task; check it on the Mac')
            count = self.db.execute('SELECT count(*) FROM tasks').fetchone()[0]
            if count >= 1000:
                raise GatewayError(429, 'Task storage is full; archive it on the Mac')
            self.db.execute('INSERT OR IGNORE INTO sessions(id) VALUES(?)', (session,))
            self.db.execute("INSERT INTO tasks(id,session,text,status,created) VALUES(?,?,?,'queued',?)", (identifier, session, text, time.time()))
            self.db.commit()
        self.wake.set()
        return identifier

    def save(self, identifier, status, run=None, session=None, output=None):
        if output is not None and not isinstance(output, str):
            raise ValueError('Invalid Hermes output')
        if session is not None and (not isinstance(session, str) or len(session) > 256):
            raise ValueError('Invalid Hermes session')
        with self.lock:
            self.db.execute('UPDATE tasks SET status=? WHERE id=?', (status, identifier))
            if run:
                self.db.execute('UPDATE tasks SET run_id=? WHERE id=?', (run, identifier))
            if output is not None:
                raw = output.encode()
                self.db.execute('UPDATE tasks SET output=?,clipped=? WHERE id=?',
                                (raw[:MAX_OUTPUT].decode(errors='ignore'), int(len(raw)>MAX_OUTPUT), identifier))
            if session:
                self.db.execute('UPDATE sessions SET backend_id=? WHERE id=(SELECT session FROM tasks WHERE id=?)', (session, identifier))
            self.db.commit()

    def work(self):
        while not self.stopped.is_set():
            with self.lock:
                row = self.db.execute("SELECT t.*,s.backend_id FROM tasks t JOIN sessions s ON s.id=t.session WHERE t.status IN ('queued','running','waiting_for_approval') ORDER BY t.created LIMIT 1").fetchone()
                job = dict(row) if row else None
                if job:
                    self.save(job['id'], 'running')
            if not job:
                self.wake.wait(.5)
                self.wake.clear()
                continue
            try:
                self.backend.execute(job, job['backend_id'], lambda status, **kw: self.save(job['id'], status, **kw), self.stopped)
            except Exception:
                # No error bodies/credentials in logs. A lost POST acceptance is uncertain, not a failed task.
                if job['run_id']:
                    self.stopped.wait(2)  # Resume poll on the next pass, without submitting again.
                else:
                    with self.lock:
                        accepted = self.db.execute('SELECT run_id FROM tasks WHERE id=?', (job['id'],)).fetchone()[0]
                    if not accepted:
                        self.save(job['id'], 'unknown')
                    else:
                        self.stopped.wait(2)

    def page(self, identifier, cursor=0):
        with self.lock:
            row = self.db.execute('SELECT * FROM tasks WHERE id=?', (identifier,)).fetchone()
            if not row:
                raise GatewayError(404, 'Unknown task; do not submit it again with a new ID')
            output = row['output']
            if cursor < 0 or cursor > len(output):
                raise GatewayError(400, 'Invalid cursor')
            text = output[cursor:cursor+384]
            payload = dict(task_id=identifier, status=row['status'], text=text, cursor=cursor,
                           next_cursor=None, clipped=bool(row['clipped']))
            while len(json.dumps(payload, ensure_ascii=False).encode()) > 1200:
                text = text[:-16]
                payload['text'] = text
            payload['next_cursor'] = cursor+len(text) if cursor+len(text)<len(output) else None
            return payload

    def resolve(self, identifier, outcome):
        # Operator-only local command, deliberately absent from the device HTTP API.
        if outcome not in ('completed', 'failed', 'cancelled'):
            raise ValueError('Invalid resolution')
        with self.lock:
            row = self.db.execute('SELECT status FROM tasks WHERE id=?', (identifier,)).fetchone()
            if not row or row[0] not in ('unknown', 'interrupted'):
                raise ValueError('Only uncertain/interrupted records can be resolved')
            self.save(identifier, outcome, output='已在电脑核对原任务状态。')

    def close(self):
        self.stopped.set()
        self.wake.set()
        if self.worker.ident is not None:
            self.worker.join(20)
        if self.worker.is_alive():
            raise RuntimeError('Gateway worker has not stopped')
        self.db.close()
        self.process_lock.close()


class DeviceServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, store, token):
        self.store, self.token = store, token
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(address, DeviceHandler)

    def process_request(self, request, address):
        if not self.slots.acquire(False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class DeviceHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *args):
        pass

    def reply(self, code, body):
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(raw)

    def dispatch(self, post=False):
        try:
            expected = 'Bearer '+self.server.token
            supplied = self.headers.get('Authorization', '')
            if not hmac.compare_digest(supplied.encode(), expected.encode()):
                raise GatewayError(401, 'Unauthorized')
            if self.headers.get('Transfer-Encoding'):
                raise GatewayError(400, 'Chunked requests are not supported')
            parsed = urlsplit(self.path)
            if post and parsed.path == '/device/tasks' and not parsed.query:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 4096:
                    raise GatewayError(413, 'Request too large')
                raw = self.rfile.read(length)
                body = json.loads(raw)
                identifier = self.server.store.submit(body)
                self.reply(202, self.server.store.page(identifier))
            elif not post and re.fullmatch(r'/device/tasks/[a-f0-9]{32}', parsed.path):
                from urllib.parse import parse_qs
                query = parse_qs(parsed.query, strict_parsing=True)
                if set(query)-{'cursor'} or len(query.get('cursor', ['0'])) != 1:
                    raise ValueError('Invalid cursor')
                cursor = int(query.get('cursor', ['0'])[0])
                self.reply(200, self.server.store.page(parsed.path.rsplit('/', 1)[1], cursor))
            else:
                raise GatewayError(404, 'Not found')
        except GatewayError as error:
            self.reply(error.code, {'error': error.message})
        except (ValueError, UnicodeError, TypeError):
            self.reply(400, {'error': 'Invalid request'})
        except (BrokenPipeError, ConnectionError, TimeoutError):
            pass

    def do_POST(self):
        self.dispatch(True)

    def do_GET(self):
        self.dispatch()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, default=Path(__file__).resolve().parents[1]/'releases/hermes-remote')
    parser.add_argument('--port', type=int, default=8769)
    parser.add_argument('--backend', choices=('cli', 'api'), default='cli')
    parser.add_argument('--api-url', default='http://127.0.0.1:8642')
    parser.add_argument('--resolve-task', help='After checking Hermes on the Mac, resolve an uncertain task locally (gateway must be stopped)')
    parser.add_argument('--resolution', choices=('completed','failed','cancelled'))
    args = parser.parse_args()
    if bool(args.resolve_task) != bool(args.resolution):
        parser.error('--resolve-task and --resolution must be used together')
    args.state.mkdir(parents=True, exist_ok=True)
    args.state.chmod(0o700)
    token_file = args.state/'device-token'
    if not token_file.exists():
        private_write(token_file, secrets.token_urlsafe(32)+'\n')
    token = token_file.read_text().strip()
    if len(token) < 32:
        parser.error('Device token must contain at least 32 characters')
    backend = RunsBackend(args.api_url, os.environ.get('API_SERVER_KEY')) if args.backend == 'api' else CliBackend(args.state/'sessions')
    store = TaskStore(args.state, backend, start_worker=not args.resolve_task)
    try:
        if args.resolve_task:
            store.resolve(args.resolve_task, args.resolution)
            print('Local task record resolved; no Hermes task was submitted or cancelled.')
            return
        server = DeviceServer(('127.0.0.1', args.port), store, token)
        print(f'Hermes device gateway: http://127.0.0.1:{server.server_port} backend={args.backend}', flush=True)
        print(f'Device credential file: {token_file} (contents are not logged)', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    finally:
        store.close()


if __name__ == '__main__':
    main()
