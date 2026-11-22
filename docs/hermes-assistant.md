# Hermes助手

菜单「工具 · 设置 → Hermes」打开 **Hermes助手**，连接这台 Mac 上已安装、已登录的 Hermes Agent。

## 使用

- 胸牌与电脑助手先通过 USB 或蓝牙配对连接。
- 在 Hermes助手 按确认开始说话，说完再按确认。语音识别沿用胸牌的小智识别身份，需要联网并完成该身份的激活。
- 识别后的文字由电脑助手交给本机 `hermes chat`，回复显示在胸牌上；上下键滚动当前回复。
- 可直接聊天，也可用自然语言安排任务。工具执行、提醒时间和成功与否以 Hermes 的实际回复为准；本项目不绕过 Hermes 的权限和批准设置。
- 长按上键开始新会话；不会删除旧 Hermes 会话。长按确认返回菜单，电脑端已接受的任务继续处理。
- 本功能的会话独立于用户已有的其他聊天，通过保存的会话 ID 续聊。完整记录保留在 Hermes 中。胸牌只展示最近一轮及有界回复，截断时提示在电脑查看。

电脑助手使用当前用户 PATH 中的 `hermes`，找不到时尝试 `~/.local/bin/hermes`。沿用已有模型、登录和网关，不将账户密钥写入固件。若 Hermes 提示待批准操作，请在电脑端处理。

## 交互模拟器

```bash
.venv/bin/python scripts/passport_simulator.py --build --hermes
```

进入「工具 · 设置 → Hermes」，在窗口新增的输入框里填写消息或任务，点「发送到 Hermes」。确认键会聚焦输入框，模拟器不冒充麦克风识别。

发送链路为：输入框 → 虚拟固件 → 电脑桥接 → 本机 Hermes → 虚拟固件 → 实际 LVGL 画面。模拟器使用独立的 Hermes 会话，保存在忽略提交的 `releases/interactive-simulator/hermes-state/`。

电脑助手的会话 ID 和诊断位于用户数据目录的 `Hermes/`，Mac 默认在 `~/Library/Application Support/AI Passport/` 下。

## 运行状态

处理任务时显示「Hermes 正在处理」，结束后显示真实回复。一次处理一条消息；同一请求不会重复执行。超时或电脑助手退出时，任务可能仍在 Hermes 网关中运行，请先到电脑确认，避免重复安排。启动器不会自动重新提交未知状态的任务。

## 开发验证

```bash
.venv/bin/python tests/test_passport_hermes.py -v
.venv/bin/python tests/test_passport_menu.py -v
env SIM_INTERACTIVE=1 bash scripts/build_passport_suite.sh
bash scripts/build_passport_suite.sh device

# 已安装并登录本机 Hermes 后，可选运行真实对话验证（不执行任务工具）
.venv/bin/python scripts/test_passport_hermes_sim.py
```

新增应用追加在现有协议 ID 后，保持其他应用 ID 与菜单 NVS 记录格式不变。默认菜单为 49 项，工具页占 9 格。

模拟器的真实 Hermes 聊天、会话续聊和任务计划验证不能替代真机麦克风、USB/BLE 重连、WiFi、耗电及任务工具批准流程的验收。

## 远程设备接口与模拟器

现已提供独立的 Mac 设备网关与 HTTP 客户端。已验证回环 HTTP 及 Tailscale Funnel 公网 HTTPS → Mac Hermes CLI → 虚拟固件屏幕，均通过聊天、续聊和任务计划测试；**真实胸牌的 WiFi 固件及 NVS 配置已实现，尚未烧录及实机验证**。模拟器的这个模式由 Mac Python 客户端访问 HTTP，再经虚拟 UART 将回复送入固件，不能当作 ESP32 网络栈的证明。

启动网关（仅监听 `127.0.0.1:8769`）：

```bash
.venv/bin/python scripts/passport_hermes_remote.py
```

首次生成 `releases/hermes-remote/device-token`（私有设备凭据），以及 SQLite 任务记录与独立胸牌会话。此目录忽略提交；不要公开令牌或把整个目录作为网页目录发布。当前一个网关服务一个设备身份，电脑模拟器可共用该身份；多设备独立吊销尚未实现。网关需保持运行，目前未安装开机自启。

另开终端启动远程模拟器：

```bash
.venv/bin/python scripts/passport_simulator.py --hermes \
  --hermes-url http://127.0.0.1:8769 \
  --hermes-token-file releases/hermes-remote/device-token
```

消息通过设备接口提交，随后按任务 ID 查询；长回复点击窗口「下一页回复」，每页内继续使用胸牌上下键滚动。切换网关地址时请使用 `--output-dir releases/interactive-simulator-remote` 等独立模拟器状态目录，不要复用未结任务状态；命令行会阻止用原状态悄悄切换网关。

设备接口：

- `POST /device/tasks`，JSON 为 `request_id`、`session_id`（两者均 32 位小写十六进制 UUID）及 `text`（最多383 UTF-8字节），返回202。
- `GET /device/tasks/{request_id}?cursor=0`，返回200、任务状态、有界回复片段和 `next_cursor`；分页按字符游标取出，响应控制在1200字节附近、完整页面不超过设备客户端2048字节限制。
- 请求均需 `Authorization: Bearer <设备令牌>`；公网客户端必须使用有效证书的 HTTPS，拒绝跳转转发令牌。没有网页登录、CORS 或远程自动批准接口。
- 同 ID 同内容重复提交返回原记录；同 ID 换内容返回409。原会话尚有任务或未知结果时拒绝新任务。最多存1000条任务，单条答复保存上限256KiB，超出显式标记截断。

默认 CLI 后端复用已有登录，创建独立远程会话，不接管原电脑模式会话。也可显式选择已配置的官方异步 API：

```bash
# API_SERVER_KEY 从现有安全环境提供，不要在命令行写出真实密钥
.venv/bin/python scripts/passport_hermes_remote.py \
  --state releases/hermes-remote-api --backend api --api-url http://127.0.0.1:8642
```

API 模式把设备 request_id 作为上游 Idempotency-Key；取得 run_id 后持久化，重启只查询该 run。未知提交结果不会盲目重发。API Server 需要另行启用；本阶段真实端到端测试采用 CLI 后端，API 采用协议替身测试，未宣称已验证本机 API Server。切换后端必须使用独立状态目录。两种后端均不绕过 Hermes 审批；API 可显示等待审批，CLI 遇到批准或执行异常时保守显示未知状态。

### Tailscale 公网入口

普通 WiFi 胸牌不能直接加入 tailnet，因此需要 **Funnel**，仅开 Serve 不够。先核对已有配置，避免覆盖其他服务：

```bash
# 本机 symlink 启动器会崩溃；使用 app 内的真实路径
TAILSCALE_BE_CLI=1 /Applications/Tailscale.app/Contents/MacOS/Tailscale funnel status
# 确认目标端口没有已有映射后，将窄设备服务转发到公网 HTTPS 的8443端口
TAILSCALE_BE_CLI=1 /Applications/Tailscale.app/Contents/MacOS/Tailscale funnel --bg --https=8443 http://127.0.0.1:8769
```

本机已由用户在浏览器批准，并成功启用8443端口的 Funnel 映射；其他机器仍需以执行结果为准，可能需要账户管理员启用 HTTPS/Funnel。将返回的 HTTPS origin 配到客户端；不要暴露电脑助手的其他端口或整个 Hermes 主 API。撤销时只关闭本功能对应映射：`TAILSCALE_BE_CLI=1 /Applications/Tailscale.app/Contents/MacOS/Tailscale funnel --https=8443 off`，避免执行会清空其他配置的 `funnel reset`。

### 断线与未知任务

客户端提交前落盘请求编号，重启后查询原编号；断网时继续查询，404或认证失败保留未结状态并提示核查，不新建任务。Mac 进程中断、CLI超时或上游提交结果丢失可能留下 `unknown`。这不是已取消：请先在 Mac Hermes 核对，停止网关后显式更新本地记录，再重启网关让设备查询确认：

```bash
.venv/bin/python scripts/passport_hermes_remote.py \
  --resolve-task <原request_id> --resolution completed
```

`--resolution` 可为 `completed`、`failed`、`cancelled`，只用于记录人工核查结果，不执行或取消 Hermes 任务。需提供原来的 `--state`、后端选择和相同安全环境；不要删除任务数据库来强行重试。

验证命令：

```bash
.venv/bin/python tests/test_passport_hermes_remote.py -v
# 实际 Hermes，无工具聊天、续聊和仅生成计划；检查固件 ACK 与真实 LVGL 帧
.venv/bin/python scripts/test_passport_hermes_sim.py --remote
```

公网验证可使用同一测试脚本；实际凭据仍从本地文件读取，不出现在命令参数或日志中：

```bash
.venv/bin/python scripts/test_passport_hermes_sim.py \
  --remote-url https://<本机名称>.<tailnet>.ts.net:8443 \
  --token-file releases/hermes-remote/device-token --public-dns
```

`--public-dns` 只在测试进程内使用公共 DNS 的 Funnel 中继地址，保留原域名的 TLS SNI 和证书校验，避免本机 MagicDNS 走私网导致误判；不修改系统 DNS。输出记录在忽略提交的 `releases/hermes-public-test/simulator/verification.json`，其中公网标记与实机标记分开。当前测试环境的公共 DNS 查询使用 Google DoH，其他网络需验证可达性。


## 工牌连接 iPhone 热点（固件已实现，待实机验证）

运行链路：工牌 → iPhone 2.4 GHz 热点 → Funnel HTTPS → Mac 设备网关 → 本机 Hermes。工牌不需要安装 Tailscale。Mac 仍需保持开机、联网且运行网关。

首次配置建议用 Mac 或另一台手机打开工牌的手机配置页，同时让提供热点的 iPhone 保持开启热点，避免同一台手机反复切换配网 WiFi 与自己的热点：

1. 首次在工牌「Hermes助手」长按下键开启加密配置热点；配置用设备连接 `AI-Passport-*`，输入工牌屏幕上本次生成的随机密码，浏览器访问 `http://192.168.8.1`。
2. 向右滑动顶端标签栏，进入「Hermes」。选择「WiFi 远程连接」，填写 Funnel 的 HTTPS origin（含8443端口，不含末尾斜杠或路径）及设备令牌。令牌来自 Mac 的 `releases/hermes-remote/device-token`，不能填写模型 API 密钥。页面不会读回真实令牌；同地址留空保留原值，更换地址必须重新填写令牌。普通免密配网热点拒绝录入新令牌；已有 Hermes 令牌后，配网默认使用随机密码热点。
3. 先保存 Hermes 设置，再到 WiFi 页选择 iPhone 热点、填写密码并连接。连接成功后才保存 WiFi；沿用现有已保存网络自动重连与每分钟扫描机制。
4. 断开配置连接并重启工牌，进入「Hermes助手」。确认开始说话，再确认结束；识别仍使用已绑定的小智识别身份。回复上下滚动，长按下键读取后续一页，长按上键开始新对话，长按确认返回。

iPhone 12及后续支持的机型可在「个人热点」开启「最大兼容性」，把热点限制到2.4GHz；连接时可保持个人热点设置页打开。[Apple 说明](https://support.apple.com/en-us/119837)、[频段说明](https://support.apple.com/en-ge/guide/security-pdf/secfd166f620/1/web/1)。手机蜂窝网络是否能访问该 Funnel 域名，仍须在真实运营商网络测试。

Hermes 配置和任务记录分别存入独立的 `hermes/config_v1`、`hermes/task_v1`，不改变旧系统设置记录。配置修改重启生效；用版本编号拒绝旧运行任务覆盖新配置。开始提交前先保存请求编号；一旦进入未决状态，重启或断线只查询原编号，绝不自动重复 POST。语音识别连接结束后，原生网络任务释放音频资源，再串行发起 Hermes HTTPS；校验 CA 证书与时间，不跟随重定向。

存在未决任务时不能切换配置或开始新会话。若明确显示未知状态，先到 Mac Hermes 核查原任务：能在网关确认完成时优先恢复原任务状态供工牌查询；确需放弃旧会话时，在工牌配置页勾选已核查并解除未知任务，再立即重启。此操作只清除工牌的未决记录并开始独立新会话，不取消任务、不重复执行；仍在处理中的记录不能解除。

本轮验证：生产 C++ 状态机及存储代码通过宿主 ASan/UBSan 故障测试；手机配置页在隔离浏览器验证；物理固件构建通过。尚未测试实机麦克风、iPhone 热点断线恢复、TLS 峰值内存或耗电。现有模拟器仍由 Python 访问 HTTP，不能替代真实 ESP32 网络验证。

```bash
.venv/bin/python tests/test_passport_hermes_firmware.py -v
node scripts/test_passport_hermes_portal.mjs
bash scripts/build_passport_suite.sh device
```
