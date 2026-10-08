# NAI-Utility-Tool 专用中转（无需 Nginx）

本分支基于 Nya-Foundation/NyaProxy v0.8.2，提交
`21db6859c2d89420731af80747670c2a530634f4`。客户端源码核对于
Aeka0/NAI-Utility-Tool `c4ee52c87619beb95ed3d91c88b656caa7cff9a0`。

链路：原版 Windows 客户端 → 本服务 HTTPS → NovelAI 图像 API。
客户端设置自定义 API URL 为 `https://你的域名:18080/api/novelai/ai/generate-image`，
Token 填下游客户端 Key。客户端的 HTTP 代理设置没有额外需求时关闭。
服务端保存真实 NAI Key，两种 Key 不相同。

## 实现

启用 `server.nai_utility.enabled` 后，精确入口根据客户端的原始 JSON 分流：

| 业务 | 识别条件 | 官方路径 |
|---|---|---|
| 文生图、图生图、局部重绘 | action 为 generate/img2img/infill，input 为字符串 | /ai/generate-image |
| SSE 生成 | 上述条件，parameters.stream=sse，同时 Accept 包含 text/event-stream | /ai/generate-image-stream |
| Vibe 编码 | 无 action，image 为字符串，parameters.information_extracted 存在 | /ai/encode-vibe |

请求体保持原始字节；禁用本入口的 request_body_substitution。
内部目标路径仍经过现有白名单、队列和上游 Key 选择。
上游请求头重新建立为 Authorization、Content-Type、Accept、Host、Content-Length。
不继承 Cookie、客户端 User-Agent、自定义头、代理链、Origin、Referer，
也不继承 YAML 浏览器头模板或 HTTPX 默认身份头。
HTTP 库仍产生正常的 TLS/HTTP 网络行为；这不等于模仿 .NET 的 TLS 指纹。
上游账号、服务器出口 IP、Prompt 和参考图片仍然是官方可见业务信息。

`only_entrypoint: true`（启用兼容模式时的默认值）只允许精确业务入口的 POST
和 `/health`；其他 URL，包括管理、配置、文档、别名、独立 Vibe 路由，返回 404。
管理应用不挂载。不接受 Cookie 代替业务请求凭证。
启用兼容模式且未配置下游 Key 或 novelai 上游时，服务拒绝启动。
本分支默认不启用兼容模式，原有通用代理行为保持。

响应的状态码、原始字节、必要端到端头保留；跳过 hop-by-hop 头，所有兼容入口响应去掉
Content-Length 并及时传递数据。不会跟随上游重定向，避免携带业务数据二次请求。
示例关闭自动重试，避免重发生成操作及改变上游错误语义；可按需自行调整。

## 直接运行（先在本地准备配置）

```bash
python3 -m venv .venv
.venv/bin/pip install .
mkdir -p data
cp configs/novelai-utility.yaml data/config.yaml
chmod 700 data
chmod 600 data/config.yaml
```

编辑 `data/config.yaml`：替换首个管理 Key、后续客户端 Key 和 variables.keys 中
的真实 NAI Key。管理 Key 与客户端 Key 应分别随机生成。
不要提交真实配置、证书私钥或日志。示例占位符本身并不是有效安全配置。

已有可信证书时，直接 HTTPS，不需要 Nginx：

```bash
.venv/bin/nyaproxy --config data/config.yaml --check-config
.venv/bin/nyaproxy --config data/config.yaml --host 0.0.0.0 --port 18080 \
  --no-reload --ssl-certfile /你的证书/fullchain.pem --ssl-keyfile /你的证书/privkey.pem
```

只有通过 SSH 转发、可信私有隧道或现有 TLS 入口连接时，才使用无证书的 HTTP
模式，并绑定 `127.0.0.1`。HTTPS IP 地址需要证书包含实际 IP 的 SAN。
服务只读取证书，不自动申请或续期；更新证书后重启服务。不要关闭客户端证书校验。

## Docker Compose（直接 TLS）

```bash
mkdir -p deploy/nai-utility/data deploy/nai-utility/certs
cp configs/novelai-utility.yaml deploy/nai-utility/data/config.yaml
# 修改 config.yaml 的全部占位符，将已有证书放入 certs/fullchain.pem、certs/privkey.pem
# Compose 默认 UID/GID=1000：确保目录及配置可由 1000 读写，私钥仅对运行用户可读。
BIND_ADDRESS=0.0.0.0 docker compose -f deploy/nai-utility/compose.yaml up -d --build
```

默认只发布到回环；`BIND_ADDRESS=0.0.0.0` 对外开放 TLS 端口。
`PORT` 默认 18080，`PUID`/`PGID` 默认 1000。
健康检查的 `--no-check-certificate` 仅用于容器内回环探活，不影响客户端或上游验证。
已提供 Compose 配置；交付环境无 Docker，未执行容器构建。

## 边界与验收

- 服务器不能解决原版客户端写死的官方超分、文本生成和账号信息直连接口。
  不要给客户端真实上游 Key。客户端部分账号显示可能不可用。
- 客户端生图请求包含 30 秒超时。示例总请求预算 25 秒、等待执行最多 10 秒，
  不添加随机延迟；多设备争用一个上游 Key 或官方生成慢时仍可能超时。
  延长服务超时不能延长客户端超时。
- 每个客户端 Key 可独立撤销，但共享同一上游池；不提供逐 Key 的独立付费余额。
- 路径级权限不限制 action 内部参数、模型、尺寸和费用。若需限制这些，另加业务策略。
- 日志示例禁用；兼容请求错误日志也不打印请求体。避免自行开启会记录内容的调试工具。
- 真实 NAI 端到端生成尚未测试：交付环境未接入你的服务器或 NAI Key。

自动测试使用真实本机 HTTP 服务：验证三种 action、Vibe 二进制、ZIP 字节、SSE 首段
提前到达和 Key 释放、上游头严格白名单、鉴权、入口封闭、超限、错误和压缩响应、
重定向不跟随。另测试 SSE 含 Content-Length 仍按流式处理和分块上传大小限制。

```bash
uv sync --extra dev --extra lint
uv run pytest -q
uv run ruff check nya tests
```

上游维护：保留 upstream 指向 https://github.com/Nya-Foundation/NyaProxy.git，
更新时先 fetch 再在本分支合并，运行上述测试，避免直接覆盖改造。

## 小内存、单账号短队列配置

本版专用入口固定最多 1 个执行请求（包括上传到上游、等待响应、响应流），
最多再容纳 2 个等待/上传中的请求；第 4 个请求直接返回 503。所有客户端 Key 共用这一个执行槽；
保持 `key_concurrency: false`、单进程、关闭重试。不启用付费并发，
也不修改客户端或请求中的尺寸、步数、模型及其他参数，因此仍须客户端自行选择免费规格。

- 在读取/解析请求体之前检查准入：最多 2 个同时上传，满额立即返回 503。
  这两个上传槽在本地读完并解析后释放，不随排队一直占用，所以小请求可形成完整 2 个等待。
- 默认单体上限 32 MiB；等待体预算 96 MiB。开始上传先预留完整单体上限，
  不信任 Content-Length；读完后缩减为实际字节数，转为执行时移出等待预算。
  因而 2 个等待是数量上限，并不保证它们都能在客户端超时前完成。
  分块上传也受同一大小限制；超限返回 413。
- 内存队列，无强制磁盘队列、无 Nginx。重启不会保留等待请求。
  `max_body_bytes` 可选择提高到 52428800（50 MiB），但会明显增大解析峰值并减少可接纳数；
  不建议此约 1 GiB 共享主机默认启用。96 MiB 是排队原始体预算，不是整个进程内存上限。
- `total_timeout_seconds: 25` 从准入、读取请求体之前开始，涵盖上传、排队、
  上游处理以及发送完整响应流。上传额外上限 10 秒，等待执行额外上限 10 秒；
  后续阶段只剩总预算中未消耗的时间，不是 10+10+25 秒。
  HTTPX 的连接/读写超时仍可更早结束请求。响应头发出前超时返回 504；
  响应已经开始后到期只能终止流，客户端会看到不完整响应/连接错误。
- 原版客户端总超时仍是 30 秒：2 个等待也不意味着每个都能在 30 秒内完成。
  慢生成可能使等待请求提前 504；已断连或到期的等待请求会被移除，绝不再调度上游。
  不发送伪心跳来延长客户端计时。客户端需重新手动发起失败请求。
- ZIP、SSE、Vibe 及上游错误体全部按原始字节增量发送；即使上游给了 Content-Length
  也不整包缓冲。过滤 Content-Length/hop-by-hop，保留压缩等端到端语义。
  响应正常完成、错误、取消或断连时关闭上游并释放执行槽和账号锁。

如调整等待数量，修改 `server.nai_utility.max_waiting_requests`，同时保持底层 `default_settings.queue.max_size` 一致；其余预算选项按需调整。
不要用多个 Uvicorn worker/多实例绕开内存中的单账号限制。
本地 mock 内存测量只验证测试负载，不能保证所有输入都落在 350–450 MB 内：
JSON 对象展开、同时解析、Python 分配器、TLS 和其他服务均会影响实际峰值。
