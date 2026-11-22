#!/usr/bin/env python3
"""Interactive macOS window backed by the C3 firmware's real Runtime and LVGL pixels."""
import argparse
import base64
import io
import json
import os
from pathlib import Path
import queue
import socket
import struct
import subprocess
import threading
import time

from PIL import Image
from passport_hermes import HermesAssistant

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'releases/interactive-simulator'
BUILD = ROOT / 'firmware/passport_suite/build-interactive'
EMU = Path.home() / '.local/share/esp32-agent/bin/esp-emu'


class Simulator:
    def __init__(self, remote_url=None, token_file=None):
        OUT.mkdir(parents=True, exist_ok=True)
        OUT.chmod(0o700)
        self.frames = queue.Queue(maxsize=1)
        self.states = queue.Queue()
        self.error = None
        self.send_lock = threading.Lock()
        self.active_app, self.generation = '', 0
        self.send_pending = False
        self.hermes_input = threading.Event()
        self.hermes_applied = queue.Queue()
        self.remote = bool(remote_url)
        if remote_url:
            from passport_hermes_client import RemoteHermes
            self.hermes = RemoteHermes(OUT/'hermes-remote-state', remote_url, token_file)
        else:
            self.hermes = HermesAssistant(OUT/'hermes-state')
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        self.emulator_log = (OUT / 'emulator.log').open('wb')
        self.process = subprocess.Popen(
            [str(EMU), '--chip', 'esp32c3', '--firmware', str(BUILD / 'merged_flash.bin'),
             '--wifi-auth', 'wpa2-psk', '--net', 'user', '--log-color', 'never',
             '--uart-tcp', '127.0.0.1:' + str(port)],
            stdin=subprocess.DEVNULL, stdout=self.emulator_log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 15
        while True:
            try:
                self.connection = socket.create_connection(('127.0.0.1', port), timeout=1)
                self.connection.settimeout(None)
                break
            except OSError:
                if self.process.poll() is not None or time.monotonic() > deadline:
                    self.process.terminate()
                    self.process.wait()
                    self.emulator_log.close()
                    raise RuntimeError('Cannot connect to emulator UART; see emulator.log')
                time.sleep(.1)
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()

    def send(self, command):
        if self.process.poll() is not None:
            raise RuntimeError('仿真器已退出，请查看 ' + str(OUT / 'serial.log'))
        with self.send_lock:
            self.connection.sendall(command.encode('utf-8'))

    def send_text(self, text):
        if self.active_app != 'hermes':
            raise ValueError('请先进入 Hermes助手')
        if self.hermes.busy or self.send_pending:
            raise ValueError('上一条任务正在处理，请稍候')
        text = str(text).strip()
        if not text or len(text.encode('utf-8')) > 383:
            raise ValueError('消息不能为空，且最多 383 字节')
        self.send_pending = True
        self.send('@'+json.dumps({'kind':'say','generation':self.generation,'text':text},ensure_ascii=False)+'\n')

    def hermes_reply(self, payload, generation, reset=False):
        packet = dict(payload['hermes'],kind='reply',generation=generation,state=payload['state'],reset=reset,remote=self.remote)
        try:
            self.send('@'+json.dumps(packet,ensure_ascii=False)+'\n')
        except (OSError, RuntimeError):
            pass

    def read(self):
        colors = [bytes((((p >> 11) & 31) * 255 // 31,
                         ((p >> 5) & 63) * 255 // 63, (p & 31) * 255 // 31)) for p in range(65536)]
        pixels = seen = None
        try:
            with (OUT / 'serial.log').open('wb') as log, self.connection.makefile('rb') as uart:
                for raw in uart:
                    log.write(raw)
                    line = raw.decode(errors='replace').strip()
                    if line.startswith('SIM_STATE '):
                        fields=dict(part.split('=',1) for part in line.split()[1:])
                        if (self.active_app,self.generation)!=(fields['app'],int(fields.get('generation',0))):self.send_pending=False
                        self.active_app=fields['app'];self.generation=int(fields.get('generation',0))
                        self.states.put(line)
                    if line.startswith('SIM_HERMES_APPLIED '):
                        self.hermes_applied.put(line)
                    if line.startswith('SIM_HERMES_INPUT'):
                        self.hermes_input.set()
                    if line.startswith(('SIM_HERMES_ENTER ', 'SIM_HERMES_REQUEST ', 'SIM_HERMES_NEW ', 'SIM_HERMES_NEXT ')):
                        event, data=line.split(' ',1);data=json.loads(data);generation=data['generation']
                        if event=='SIM_HERMES_ENTER' and self.remote:
                            self.hermes.watch(lambda payload,g=generation:self.hermes_reply(payload,g))
                        if event=='SIM_HERMES_NEXT' and self.remote:
                            self.hermes.next_page(lambda payload,g=generation:self.hermes_reply(payload,g))
                        if event=='SIM_HERMES_REQUEST':
                            self.send_pending=False
                            self.hermes.submit(data['text'],(self.hermes.instance,generation,data['turn']),
                                turn=data['turn'],update=lambda payload,g=generation:self.hermes_reply(payload,g))
                        reset=event=='SIM_HERMES_NEW' and self.hermes.new_chat()
                        self.hermes_reply(self.hermes.snapshot(),generation,reset=reset)
                    if line.startswith('FRAME_BEGIN '):
                        _, _, w, h = line.split()
                        if (int(w), int(h)) != (240, 320):
                            raise ValueError('Unexpected firmware display size')
                        pixels, seen = bytearray(240 * 320 * 3), bytearray(240 * 320)
                    elif line.startswith('ROW ') and pixels is not None:
                        _, x, y, data = line.split()
                        x, y = int(x), int(y)
                        row = base64.b64decode(data, validate=True)
                        count = len(row) // 2
                        if len(row) % 2 or not (0 <= y < 320 and 0 <= x <= 240 - count):
                            raise ValueError('Invalid firmware pixel row')
                        at = y * 240 + x
                        pixels[at * 3:(at + count) * 3] = b''.join(colors[p[0]] for p in struct.iter_unpack('<H', row))
                        seen[at:at + count] = b'\1' * count
                    elif line.startswith('FRAME_END ') and pixels is not None:
                        if not all(seen):
                            raise ValueError('Incomplete firmware frame')
                        buffer = io.BytesIO()
                        Image.frombytes('RGB', (240, 320), bytes(pixels)).save(buffer, format='PNG')
                        frame = buffer.getvalue()
                        temporary = OUT / 'screen.tmp'
                        temporary.write_bytes(frame)
                        temporary.replace(OUT / 'screen.png')
                        if self.frames.full():
                            self.frames.get_nowait()
                        self.frames.put_nowait(frame)
                        pixels = seen = None
                    elif any(marker in line for marker in ('Guru Meditation Error', 'assert failed', 'abort() was called')):
                        raise RuntimeError(line)
                if self.process.poll() not in (None, 0):
                    self.error = '仿真器已退出，请查看 serial.log'
        except Exception as error:
            self.error = str(error)

    def close(self):
        self.hermes.close()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.reader.join(timeout=2)
        self.connection.close()
        self.emulator_log.close()


def smoke(sim):
    def frame():
        try:
            return sim.frames.get(timeout=45)
        except queue.Empty:
            raise RuntimeError(sim.error or 'Timed out waiting for firmware frame')

    def expect(app):
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            state = sim.states.get(timeout=max(.1, deadline - time.monotonic()))
            if 'app=' + app + ' ' in state:
                frame()
                print(state, flush=True)
                return
        raise AssertionError('Expected app=' + app)

    expect('home')
    sim.send('c')
    expect('menu')
    sim.send('c')
    expect('chat')
    sim.send('b')
    expect('menu')
    sim.send('D')
    expect('menu')
    sim.send('b')
    expect('home')
    if sim.error:
        raise RuntimeError(sim.error)
    print('INTERACTIVE_SMOKE_PASS: firmware pixels, confirm, back and long-down', flush=True)


def window(sim):
    import AppKit as A
    import Foundation as F

    class Controller(A.NSObject):
        def press_(self, sender):
            try:
                sim.send(('u', 'c', 'd', 'U', 'b', 'D', 'r')[sender.tag()])
            except (OSError, RuntimeError) as error:
                self.status.setStringValue_(str(error))

        def sendText_(self, sender):
            try:
                sim.send_text(self.input.stringValue())
                self.input.setStringValue_('')
            except (ValueError, OSError, RuntimeError) as error:
                self.status.setStringValue_(str(error))

        def nextPage_(self, sender):
            if sim.active_app == 'hermes' and sim.remote:
                sim.send('D')

        def refresh_(self, timer):
            if sim.hermes_input.is_set():
                sim.hermes_input.clear()
                win.makeFirstResponder_(self.input)
                self.status.setStringValue_('模拟器请在输入框输入消息，再点发送')
            latest = None
            try:
                while True:
                    latest = sim.frames.get_nowait()
            except queue.Empty:
                pass
            if latest and latest != getattr(self, 'last_frame', None):
                self.last_frame = latest
                data = F.NSData.dataWithBytes_length_(latest, len(latest))
                self.screen.setImage_(A.NSImage.alloc().initWithData_(data))
                self.status.setStringValue_('运行中 · 可点击下方按键')
            if sim.error:
                self.status.setStringValue_(sim.error)

        def windowWillClose_(self, notification):
            self.timer.invalidate()
            sim.close()
            A.NSApplication.sharedApplication().terminate_(None)

    app = A.NSApplication.sharedApplication()
    app.setActivationPolicy_(A.NSApplicationActivationPolicyRegular)
    controller = Controller.alloc().init()
    style = A.NSWindowStyleMaskTitled | A.NSWindowStyleMaskClosable | A.NSWindowStyleMaskMiniaturizable
    win = A.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        F.NSMakeRect(0, 0, 440, 790), style, A.NSBackingStoreBuffered, False)
    win.setTitle_('AI Passport · Hermes助手' if sim.active_app=='hermes' else 'AI Passport · 交互仿真器')
    win.setDelegate_(controller)
    view = win.contentView()
    controller.screen = A.NSImageView.alloc().initWithFrame_(F.NSMakeRect(40, 285, 360, 480))
    controller.screen.setImageScaling_(A.NSImageScaleProportionallyUpOrDown)
    view.addSubview_(controller.screen)
    controller.status = A.NSTextField.labelWithString_('正在启动 ESP32-C3 固件…')
    controller.status.setFrame_(F.NSMakeRect(25, 253, 390, 22))
    view.addSubview_(controller.status)
    controller.input = A.NSTextField.alloc().initWithFrame_(F.NSMakeRect(20, 204, 280, 30))
    controller.input.setPlaceholderString_('给 Hermes 发消息或安排任务')
    controller.input.setTarget_(controller)
    controller.input.setAction_('sendText:')
    view.addSubview_(controller.input)
    send = A.NSButton.buttonWithTitle_target_action_('发送到 Hermes', controller, 'sendText:')
    send.setFrame_(F.NSMakeRect(307, 204, 115, 30))
    view.addSubview_(send)
    for tag, title in enumerate(('上', '确认', '下', '长按上', '长按确认 / 返回', '长按下', '刷新画面')):
        button = A.NSButton.buttonWithTitle_target_action_(title, controller, 'press:')
        column, row = tag % 3, tag // 3
        button.setFrame_(F.NSMakeRect(20 + column * 137, 147 - row * 35, 128, 30))
        button.setTag_(tag)
        view.addSubview_(button)
    if sim.remote:
        page = A.NSButton.buttonWithTitle_target_action_('下一页回复', controller, 'nextPage:')
        page.setFrame_(F.NSMakeRect(157, 42, 128, 30))
        view.addSubview_(page)
    note = A.NSTextField.labelWithString_('示例天气 · Hermes 为真实会话 · 语音输入需实机')
    note.setFont_(A.NSFont.systemFontOfSize_(11))
    note.setFrame_(F.NSMakeRect(20, 12, 410, 20))
    view.addSubview_(note)
    controller.timer = F.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        .1, controller, 'refresh:', None, True)
    win.center()
    win.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)
    app.run()


def main():
    global OUT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', action='store_true', help='Rebuild the interactive firmware first')
    parser.add_argument('--hermes', action='store_true', help='Open the Hermes assistant page directly')
    parser.add_argument('--smoke', action='store_true', help='Check firmware/keyboard round trip without opening a window')
    parser.add_argument('--output-dir', type=Path, default=OUT, help='Separate simulator state/log directory')
    parser.add_argument('--hermes-url', help='Remote HTTPS origin (loopback HTTP allowed for development)')
    parser.add_argument('--hermes-token-file', type=Path, help='Private device credential file')
    args = parser.parse_args()
    OUT = args.output_dir.resolve()
    if bool(args.hermes_url) != bool(args.hermes_token_file):
        parser.error('--hermes-url and --hermes-token-file must be used together')
    if args.build or not (BUILD / 'merged_flash.bin').exists():
        subprocess.run(['bash', str(ROOT / 'scripts/build_passport_suite.sh')],
                       cwd=ROOT, env=dict(os.environ, SIM_INTERACTIVE='1'), check=True)
    sim = Simulator(args.hermes_url, args.hermes_token_file)
    try:
        if (args.hermes or args.hermes_url) and not args.smoke:
            deadline=time.monotonic()+45
            while sim.active_app!='home' and time.monotonic()<deadline:time.sleep(.05)
            if sim.active_app!='home':raise RuntimeError('Firmware did not reach home')
            sim.send('cuuc')
            deadline=time.monotonic()+45
            while sim.active_app!='hermes' and time.monotonic()<deadline:time.sleep(.05)
            if sim.active_app!='hermes':raise RuntimeError('Firmware did not reach Hermes')
        smoke(sim) if args.smoke else window(sim)
    finally:
        sim.close()


if __name__ == '__main__':
    main()
