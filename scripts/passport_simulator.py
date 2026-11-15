#!/usr/bin/env python3
"""Interactive macOS window backed by the C3 firmware's real Runtime and LVGL pixels."""
import argparse
import base64
import io
import os
from pathlib import Path
import queue
import socket
import struct
import subprocess
import threading
import time

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'releases/interactive-simulator'
BUILD = ROOT / 'firmware/passport_suite/build-interactive'
EMU = Path.home() / '.local/share/esp32-agent/bin/esp-emu'


class Simulator:
    def __init__(self):
        OUT.mkdir(parents=True, exist_ok=True)
        self.frames = queue.Queue(maxsize=1)
        self.states = queue.Queue()
        self.error = None
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
        self.connection.sendall(command.encode('ascii'))

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
                        self.states.put(line)
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

        def refresh_(self, timer):
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
        F.NSMakeRect(0, 0, 440, 700), style, A.NSBackingStoreBuffered, False)
    win.setTitle_('AI Passport · 交互仿真器')
    win.setDelegate_(controller)
    view = win.contentView()
    controller.screen = A.NSImageView.alloc().initWithFrame_(F.NSMakeRect(40, 195, 360, 480))
    controller.screen.setImageScaling_(A.NSImageScaleProportionallyUpOrDown)
    view.addSubview_(controller.screen)
    controller.status = A.NSTextField.labelWithString_('正在启动 ESP32-C3 固件…')
    controller.status.setFrame_(F.NSMakeRect(25, 163, 390, 22))
    view.addSubview_(controller.status)
    for tag, title in enumerate(('上', '确认', '下', '长按上', '长按确认 / 返回', '长按下', '刷新画面')):
        button = A.NSButton.buttonWithTitle_target_action_(title, controller, 'press:')
        column, row = tag % 3, tag // 3
        button.setFrame_(F.NSMakeRect(20 + column * 137, 117 - row * 35, 128, 30))
        button.setTag_(tag)
        view.addSubview_(button)
    note = A.NSTextField.labelWithString_('示例天气 · 云端语音、音频及无线硬件需实机验证')
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', action='store_true', help='Rebuild the interactive firmware first')
    parser.add_argument('--smoke', action='store_true', help='Check firmware/keyboard round trip without opening a window')
    args = parser.parse_args()
    if args.build or not (BUILD / 'merged_flash.bin').exists():
        subprocess.run(['bash', str(ROOT / 'scripts/build_passport_suite.sh')],
                       cwd=ROOT, env=dict(os.environ, SIM_INTERACTIVE='1'), check=True)
    sim = Simulator()
    try:
        smoke(sim) if args.smoke else window(sim)
    finally:
        sim.close()


if __name__ == '__main__':
    main()
