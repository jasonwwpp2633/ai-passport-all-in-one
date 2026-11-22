# 胸牌通过 WiFi / 外网连接 Mac Hermes：研究记录

核验日期：2026-11-23。范围：公开上游接口与网络架构；未部署、未开端口、未修改 Hermes 配置，未读取凭据或个人聊天。

**建议：优先复用本机已存在的 Hermes 异步 API，按需增加专用设备轻量适配层，再通过 HTTPS Tunnel 供胸牌访问；外网优先评估 Cloudflare 正式 Tunnel。任务采用“提交后取得 ID，再轮询结果”，避免胸牌保持数分钟连接。** 这是设计建议，不代表当前固件或本机服务已经实现，不必重造完整聊天网关。

## 上游接口确实存在，但不等于本机可用

官方 [Programmatic Integration](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/programmatic-integration.md) 明确列出三类入口：

| 入口 | 官方传输 | 对胸牌的意义 |
| --- | --- | --- |
| API server | HTTP / SSE | 最适合作为 Mac 桥接的后端，客户端实现较简单 |
| TUI gateway | JSON-RPC / stdio 或 WebSocket | 更完整的会话、审批与事件能力；共享 `/api/ws` 使用 `hermes-gateway-v1` 和每次连接的一次性 ticket，重连需要新 ticket |
| ACP | `hermes acp`，JSON-RPC / stdio | IDE 协议；不能直接当作 ESP32 可连接的公网 HTTP 服务 |

上述接口驱动 Hermes agent，而不是只调用一个裸 LLM；工具能力在 Hermes 所在机器执行。不要把“同样叫 gateway”当作兼容协议的证据。

官方 [API 源码路由表](https://github.com/NousResearch/hermes-agent/blob/main/gateway/platforms/api_server.py) 可直接核实 `/v1/chat/completions`、`/v1/responses`、`/v1/capabilities`、sessions 与 runs 等路由。`/v1/capabilities` 用于实际版本能力探测，不能仅依据仓库最新文档硬编码功能。

官方 [API Server 文档](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/api-server.md) 说明：API 默认关闭，默认监听 `127.0.0.1:8642`，使用 `API_SERVER_KEY` Bearer 认证。HTTP API 可承载终端和文件工具权限。**API 创建的会话不能接管 CLI、TUI、Desktop、ACP 等其他入口已有会话；此类请求会被拒绝。** 要对其他入口已有会话发消息，当前上游指导使用共享 gateway WebSocket 的 `prompt.submit`。

因此需区分：“在同一台 Mac、使用同一配置的新专用胸牌会话”和“继续操控用户已打开的那条 CLI 会话”。前者可优先采用 HTTP，后者涉及 gateway 的权限与会话归属。

已只读核对本机 `/Users/mrwang/.hermes/hermes-agent`：远程来源为官方 NousResearch 仓库，工作树内存在 API Server、Runs API 和幂等存储实现。源码默认端口为 8642，使用 Bearer 认证并检查密钥强度。这证明安装内容包含接口实现，尚未验证 API 服务已启用或正在监听。上述网络引用指向会变化的上游 `main`；实际开发应针对本机能力探测，不应为接胸牌直接升级替换现有 Hermes。

## 公网桥接选择

| 方案 | 流量路径与条件 | 评价 |
| --- | --- | --- |
| Cloudflare 正式 Tunnel | 胸牌 HTTPS → Cloudflare → Mac `cloudflared` → loopback 桥接 | 优先候选；Mac 主动出站，无需家庭路由器端口转发；正式域名及账户配置仍待确认 |
| Tailscale Serve | tailnet 客户端 → Mac Serve → 桥接 | 适合手机/电脑；单独启用 Serve，普通外网 WiFi 胸牌无法进入 tailnet |
| Tailscale Funnel | 胸牌 HTTPS → 公共 Funnel URL → Mac 桥接 | 可用备选；胸牌无需 Tailscale，但公网访问需自己做应用认证 |
| VPS 反向通道 | 胸牌 HTTPS → VPS 认证入口 → 反向 SSH / 私网链路 → Mac 桥接 | 网络控制更灵活；需要维护 VPS、TLS、通道重连及访问日志 |

[Cloudflare 官方说明](https://developers.cloudflare.com/tunnel/) 确认 `cloudflared` 从源站出站建立隧道，并可将公开 hostname 映射至 localhost HTTP 服务。这里选择 HTTP 发布，胸牌只需要普通 HTTPS 客户端；不把原始 TCP 隧道当作无需客户端的 HTTPS。公网域名经过 Cloudflare 不会自动变成私有访问。

[Cloudflare Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/) 是临时测试工具，地址变化、无 uptime 保证、最多 200 个并发在途请求，且不支持 SSE。正式实现不应依赖它。若用 Access，[Service tokens](https://developers.cloudflare.com/cloudflare-one/access-controls/service-credentials/service-tokens/) 支持自动客户端通过 `CF-Access-Client-Id` / `CF-Access-Client-Secret` 认证，策略须采用 Service Auth；浏览器邮箱验证码流不适合胸牌。

[Serve 官方文档](https://tailscale.com/docs/features/tailscale-serve) 将其访问范围限定为 tailnet；由此推断，未运行 Tailscale 且没有额外路由/代理的 ESP32 无法靠公网 WiFi 直接访问 Serve。不要假设手机开了 Tailscale，手机热点里的胸牌就自动加入 tailnet。本次未发现/验证 ESP32-C3 官方 Tailscale 客户端；本方案不依赖它。

[Funnel 官方文档](https://tailscale.com/docs/features/tailscale-funnel) 明确支持公共互联网访问，限制为 tailnet 域名及 TLS，监听端口为 443 / 8443 / 10000，并有不可配置带宽限制。文档对 macOS 安装变体的表述有不一致之处，需以本机安装版本实测端口转发支持；不可直接判定 App Store 版一定可用。Funnel 公网请求没有 Serve 的用户身份头，不能把 tailnet 身份当作胸牌认证。

VPS 方案可利用 [OpenSSH `ssh -R`](https://man.openbsd.org/ssh.1) 将 VPS 端口转发回 Mac；官方默认远端监听 loopback。建议 VPS HTTPS 网关连接该 loopback 转发，不将反向端口直接监听全网。部署图是设计推论；本次没有配置或测试 SSH/VPS。

所有“Mac 执行”方案均要求 Mac 醒着且联网。VPS 只做代理不能解决 Mac 睡眠；需要离线受理时应额外设计持久队列及过期规则，不能声称代理会自动补做任务。

## 长任务、断线和认证设计建议

胸牌可直接适配 `/v1/runs` 提交/查询协议。若需隔离 Hermes 主密钥、限制会话访问或裁剪大答复，再加窄接口，例如 `POST /device/tasks` 提交 `request_id`、会话标识和输入，快速返回 `task_id`，`GET /device/tasks/{id}` 返回有限长度的状态、答复片段和游标。这两个 `/device/*` 是**可选拟定路由**，不是 Hermes 已有端点。

Mac 适配层按实际能力选择：

1. 如已支持 Runs API，使用 `/v1/runs` 提交，保留 `run_id`，再查询结果；否则将现有 CLI 调用包成后台任务并由桥接维护状态，不能用 HTTP 请求存活期作为任务生命期。
2. 提交前持久化设备 `request_id` 和任务记录；重复 ID 返回原任务，不创建新任务。传输超时是“结果未知”，不能自动生成新 ID 重做。
3. 胸牌重连只查询原任务；设备重启后仍保留未结任务的标识。Mac 重启后的运行任务应标记未知/中断，等待核查，不能静默再次执行。
4. 队列、输入、答复与事件均设上限；允许按游标取答复。长输出不应整段塞入 MCU RAM 或当前设备包。
5. 如要审批，明确展示等待状态和当前审批 ID；没有审批 UI 时保持等待或拒绝，不能自动同意。取消应返回可观测状态，不能把“断开网络”当作已停止工具执行。

上游 [Runs 实现](https://github.com/NousResearch/hermes-agent/blob/main/gateway/platforms/api_server_runs.py) 支持 polling、事件序号和 `Last-Event-ID` / `last_seq` 续接；其事件缓存有保留边界，缺口会出现 `replay.truncated`，不是无限历史。恢复后仍应拉取任务最终状态，而不只拼接流。上游 [幂等存储实现](https://github.com/NousResearch/hermes-agent/blob/main/gateway/platforms/api_server_run_idempotency.py) 使用持久存储，保留期为 24 小时；本机源码中持久层初始化失败会退化为内存去重，不能宣称无条件跨重启去重。它也不能替代设备定义的重试窗口和长期去重规则。当前上游对于执行未知状态另有显式解决流程；自动重发并不安全。

认证方面，建议每台胸牌拥有可吊销的独立凭据，限定只能调用设备任务接口；Hermes 主密钥留在 Mac。桥接绑定 loopback，Tunnel/Funnel/VPS 只转发桥接。校验 TLS 证书和设备时间，不使用跳过证书校验。对设备做限流、长度限制和单会话串行化。密钥不放入 URL、日志或仓库；设备遗失时能够撤销。以上是建议，不声称固件已具备安全存储或密钥轮换功能。

## 本地项目核对与改造范围

本轮只读核对发现：

| 位置 | 当前行为 | 远程模式要改什么 |
| --- | --- | --- |
| `firmware/passport_suite/main/apps.hpp` | Hermes 被标记为需要电脑、不需要互联网 | 区分电脑模式和远程 WiFi 模式，不能全局简单翻转标记 |
| `firmware/passport_connectivity/main/live_services.cpp` | 启动语音受电脑桥接在线状态约束，消息交由电脑伴侣执行 | 远程模式取消电脑心跳依赖；接入任务提交、轮询和审批等待状态 |
| `firmware/passport_device/main/native_services.cpp` | 已有 HTTPS、CA 证书校验、POST 与请求头支持；通用请求超时 12 秒且只接受 HTTP 200 | 接受任务提交响应码；POST 与查询采用不同重试策略，避免重复执行 |
| `firmware/passport_suite/main/hermes.hpp` | 输入上限 383 字节，单次答复上限 1152 字节 | 保持有界缓冲；长答复按页获取，不能直接解析任意大的 API 输出 |
| `scripts/passport_hermes.py` | 电脑伴侣通过 CLI 执行并维护 CLI 会话 | 如需两种连接方式延续同一对话，应统一接入同一个 API 专用会话；HTTP API 不能直接接管旧 CLI 会话 |

设备是无 PSRAM 的 ESP32-C3。原生语音识别也使用 TLS/音频缓冲，因此应先结束语音连接、释放资源，再提交 Hermes 任务；当前按应用选择语音任务的分发逻辑需要细分，避免轮询操作进入语音识别路径。现有 HTTPS 代码提供了实现基础，并不代表本轮已经验证真实胸牌的 HTTPS 或内存余量。

建议第一版明确选择“电脑连接 / 远程 WiFi”，每个任务固定后端和会话；不要在超时后自动切换通道重新执行。设备为提交生成稳定 request_id，将已接受的 run_id 和专用 session_id 少量写入 NVS；重启后查询原任务，不在每次轮询时写 Flash。Mac 适配层保存映射并将上游大结果裁剪成设备可接收的分页数据。

本机 PATH 中可找到 `tailscale`，未找到 `cloudflared`；没有检查账户登录状态、域名配置，也没有安装、启用公网入口或改动凭据。

## 推荐分阶段实现

1. 先实现本地 Mac 设备适配层，复用官方 Runs API，提供小响应、设备认证、持久任务标识和幂等提交；用电脑请求核验。
2. 在交互模拟器加入远程传输模式，通过实际适配层验证聊天、任务状态、掉线查询、重复提交和长答复分页。模拟器不能证明 ESP32 的 TLS 内存够用。
3. 固件加入远程模式与配置入口；在真机上验证语音结束后提交、网络切换、TLS 校时、内存余量及重启恢复。
4. 选择可达的正式 HTTPS Tunnel，再用手机热点等外网验证。Mac 必须保持开机、联网且服务运行；需要独立全天运行时，再评估把 Hermes 本身迁到常在线主机。

## 尚待确认和验证

- API 服务实际启用状态与能力探测结果；采用胸牌专用 API 会话，还是必须继续某条旧 CLI 对话。
- 可用域名、Cloudflare/Tailscale 账户及本机安装变体；不需要将凭据写入仓库。
- 国内及出差网络对候选服务的 DNS/TLS 可达性、热点切换和实际延迟。
- 设备丢失撤销、Mac 重启后的未知任务、审批等待及取消的实际行为。

目前结论是架构可行性与接口核验；尚无胸牌端到端外网联网成功的证据。本轮仅新增研究文档，未修改业务代码、启用服务或开放公网访问。


## 后续实施进展（2026-11-23）

上述为先期只读研究记录。后续已实现设备网关、HTTP客户端与模拟器远程模式；用户选择 Tailscale 并批准 Funnel 后，完成了经公共 DNS 中继的 HTTPS 实测，聊天、会话续聊、任务计划及实际 LVGL 帧均通过。实际后端为现有 Hermes CLI；官方 Runs API 后端目前为协议替身验证。真实胸牌 WiFi/TLS/NVS 尚未接入，不能把模拟器的 Python 网络请求当作实机验证。使用方式与运行边界见 [Hermes助手](../hermes-assistant.md)。
