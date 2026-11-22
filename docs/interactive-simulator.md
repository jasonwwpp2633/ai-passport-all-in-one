# 本地交互仿真器（macOS）

在项目根目录运行：

```bash
.venv/bin/python scripts/passport_simulator.py
```

首次启动自动构建；修改固件逻辑或界面后，运行下面的命令重新构建并打开：

```bash
.venv/bin/python scripts/passport_simulator.py --build
```

窗口中的“上 / 确认 / 下”和三个长按按钮，通过本地 UART socket 传给 ESP32-C3 虚拟固件；固件调用现有 `Runtime` 按键方法，用实际 LVGL 像素刷新窗口。长按确认对应返回，菜单中长按上/下切换分类页。它验证功能逻辑和界面，不模拟物理按键的消抖和按压时长。

交互固件使用独立入口 `interactive_main.cpp` 和独立目录 `firmware/passport_suite/build-interactive/`；默认自动回归仍使用 `sim_main.cpp`。模拟状态为本次会话所有，不写入真实胸牌；天气和电量为示例数据。Hermes助手连接真实本机 Hermes；云端语音、音频和无线硬件连接需另行验证。

依赖：ESP-IDF 6.1、`~/.local/share/esp32-agent/bin/esp-emu`、项目 `.venv` 中的 Pillow 和 PyObjC。支持 macOS，启动命令不会刷写真实设备。

无窗口验证按键与固件画面的往返：

```bash
.venv/bin/python scripts/passport_simulator.py --smoke
```

输出 `INTERACTIVE_SMOKE_PASS` 表示主页、菜单、小智页、返回及菜单长按翻页的命令通道通过。窗口需要在桌面登录会话中运行。

本机日志和最新截图：`releases/interactive-simulator/serial.log`、`emulator.log`、`screen.png`。关闭窗口会退出仿真器。

## Hermes助手

菜单「工具 · 设置 → Hermes」进入后，可在窗口输入框填写文字，点「发送到 Hermes」。该操作会交给本机真实 Hermes，并在虚拟胸牌屏幕显示结果。按确认聚焦输入框，上下滚动，长按上键新建独立会话。详见 [Hermes助手](hermes-assistant.md)。
