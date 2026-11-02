# MCP OAuth：阶段 1 接口与操作说明

日期：2026-11-02。状态：独立分支已实现，默认关闭；协议、真实浏览器、公网 HTTPS 及 Linux 隔离预算验证通过，真实 ChatGPT 仍在验收。未部署到生产或合并。阶段 0 文档仍是设计快照，当前可用接口以本文为准；最新测试范围与问题见 [阶段 2 记录](OAUTH-STAGE2.md)。

## 行为与模块

开启后，独立 HTTPS 主机的 `/mcp` 接受 OAuth Bearer；旧 Header Key 与 URL Key 的主机、路径、身份和凭据保留。新主机拒绝旧路径，旧入口不把 OAuth Bearer 解释为长期身份凭据。所有 15 个 MCP 工具继续使用原 Web API 身份校验和权限规则。

- `core/oauth_server.py`：Authlib 1.9.2 授权码、强制 S256、DCR、元数据、两步邀请码授权、刷新轮换及撤销。
- `core/oauth_store.py`：独立 SQLite、事务消费、凭证摘要、资源绑定、限流、脱敏审计和清理。
- `core/reader_oauth.py`：读取既有身份、MCP 通道检查、独立主机分流；不创建或写回身份。
- `core/oauth_admin.py`：本机初始化、生成/撤销邀请、查询/撤销客户端授权及审计。
- `core/mcp_server.py`：默认关闭的挂接；OAuth 内部请求仅向配置的回环 Web 地址发送原身份凭证，不跟随重定向或环境代理。

MCP 与 AS 角色在同一进程，不增加独立 IdP 服务。浏览器采用随机 Secure/HttpOnly/SameSite Cookie，数据库仅保存会话 Cookie 与 CSRF 的摘要，不需要签名会话密钥。通用模块不读取 identities.json；适配层通过回调提供现存身份状态。当前 scope 和授权页面针对 Reader-Mate，后续抽库仍需配置化；不能直接把授权服务端复制成 Leto 的 OAuth 客户端。

## 协议与状态

| 接口 | 行为 |
| --- | --- |
| `/.well-known/oauth-protected-resource` 及 `/mcp` 后缀版本 | 广告固定 resource、issuer 和 `reader:mcp` |
| `/.well-known/oauth-authorization-server` | 广告 S256、授权码/刷新、none 认证及 RFC 9207 iss 响应 |
| `POST /oauth/register` | 公开客户端 DCR；HTTPS 精确回调、禁止 fragment/userinfo/通配符和预填 code/state/iss/error；不抓取客户端 URL |
| `GET /oauth/authorize` | 校验 OAuth 参数，创建 10 分钟浏览器事务 |
| `POST /oauth/authorize` | CSRF 校验；先验证邀请码，再显示稳定 ID/昵称及实际回调地址，由用户确认 |
| `POST /oauth/token` | 授权码 + PKCE 或刷新；resource 必须与固定资源一致 |
| `POST /oauth/revoke` | form `client_id`、`token`；仅撤销该客户端持有 Token 的授权家族，未知 Token 返回 200 |
| `/mcp` | 每次 HTTP 请求校验有效期、授权、资源、scope 和当前身份；匿名 401 带发现地址，scope 不足 403 |

支持 `reader:mcp`，权限上限等于该读者原有 MCP 权限；`offline_access` 只在 AS 广告。客户端须注册 `refresh_token` grant，授权页明确说明离线刷新及最长寿命，确认时记录刷新同意；无须额外请求 offline_access，业务 scope 也不因此扩大。旧授权按原同意处理，原来没有刷新资格的授权须重新确认。刷新不改变身份/资源，授权家族最长 30 天；Access Token 默认 15 分钟，授权码 60 秒。

授权码、邀请和刷新消费均为 SQLite `BEGIN IMMEDIATE` 事务。授权码重放与旧刷新 Token 重放拒绝并撤销原授权家族，包括新 Token。并发重试也按重放处理，客户端应串行刷新；若网络丢失刷新响应，应重新授权。授权码交换失败不会提前消费邀请码以外的其他读者状态。

Token、邀请码、授权码与会话 Cookie 在持久存储中仅保存摘要；真实原值只在标准响应或指定私有邀请文件中交付。数据库绑定 issuer/resource，缺失、损坏或换错配置时拒绝访问，不自动创建新库。身份文件失效或身份被停用时，OAuth 请求立即失败；撤销单个 OAuth 授权不改其他授权、原 Key、阅读进度或建议。

当前 DCR 的客户端名称不作为可信身份，也不返回注册管理凭证；授权页显示实际回调地址和 client_id。未实现 CIMD、OIDC、账号注册或模型 API，不广告这些能力。

## 隔离环境准备

先准备独立的源码、书库、data 和 state；所有路径都应指向隔离副本。仍需原 MCP 入口的测试路径钥匙与既有测试身份，不能把实验连接到生产 Web 后端。

```sh
python3 -m pip install -r core/requirements-oauth.txt
```

配置见 [reader-oauth.env.example](deploy/reader-oauth.env.example)。issuer 必须是无路径、无尾斜杠的 HTTPS origin；resource 必须为 `issuer + /mcp`；新主机应与旧主机不同。`READER_API_URL` 必须是无路径的回环 HTTP 地址，不能包含用户信息、query 或 fragment。

配置中的 `READER_OAUTH_ENABLED=0` 保持关闭。初始化 CLI 只读取 issuer/resource/DB 配置，不要求打开功能。Linux 上先确保私有父目录归属服务用户，权限 0700；已有数据库拒绝覆盖。服务启动时会再次检查目录和文件权限。Windows 的文件模式位不能替代 ACL，测试机和隔离部署需另设仅账户可读写的 ACL。

```sh
python3 core/oauth_admin.py init
python3 core/oauth_admin.py invite --identities /isolated/reader/core/data/identities.json --reader-id test_reader_004 --output /isolated/reader/state/oauth-invitation.txt --ttl 3600 --uses 1
python3 core/oauth_admin.py invites
python3 core/oauth_admin.py grants
python3 core/oauth_admin.py audit
```

示例 `test_reader_004` 必须换成该隔离身份文件中已存在、可使用 MCP 的准确稳定 ID。旧网关的 `004` 和 `op004` 是两个不同 ID；工具不会替你创建、清洗或重命名身份。邀请只写入新建的 0600 文件，不在终端显示；普通查询只显示公开 ID，不显示原邀请或 Token。不要将邀请原值粘贴到命令行、聊天、日志或 Git。

开启隔离功能后才启动该副本的 MCP 服务。示例反代见 [Caddyfile.oauth.example](deploy/Caddyfile.oauth.example)，所有示例均未安装或执行。只开放经有效证书保护的 HTTPS，回环监听端口不对公网开放。MCP 进程在 OAuth 开启时禁用 Uvicorn 的 Forwarded 自动信任；只允许本机回环的 TLS 终止代理，不能使用不受信任的同机代理代为声明安全传输。

反代主机仅开放清单内的新端点，其余路径 404，不落入 Web catch-all 或旧 URL 网关。不要开启请求访问日志、请求头/体跟踪或响应 Location 跟踪；后者含一次性授权码。若实际部署使用 Cloudflare，需单独核对 HTTPS-only、主机与 well-known 路由，不能照搬旧的仅 `/mcp` ingress。

## 限流、容量与运维

限流持久化在 SQLite，按真实连接来源及全局计数；忽略客户端 Forwarded/X-Forwarded-For。由于可信反代来自回环，同一代理下的用户保守地共享来源配额：注册 10 次/小时，授权页面及提交合计 30 次/10 分钟，其余 AS 请求 120 次/分钟；对应全局上限为 50、120、600。MCP 业务访问不使用这些 AS 配额。

最多 1024 个注册客户端、4096 个邀请码、2048 个保留的浏览器事务；数据库最多 16384 页（默认页大小下约 64 MiB）。OAuth 审计仅保留最近 10000 条，不是永久账本；如需长期记录，应做脱敏离线归档。服务不会将完整请求写入 OAuth 审计。

定期由同一服务用户执行清理，释放过期邀请/事务等配额：

```sh
python3 core/oauth_admin.py prune
python3 core/oauth_admin.py revoke --grant-id <公开授权ID>
python3 core/oauth_admin.py revoke-invite --invite-id <公开邀请码ID>
```

撤销邀请不撤销已产生的授权，撤销授权不影响旧身份 Key。刷新/授权码的重放检测记录保留到授权家族到期。客户端注册配额不会自动清空；达到上限时先调查滥用，不用清空数据库绕过限制。备份 SQLite 前应停止该 MCP 实例，保留目录、数据库及可能存在的 journal，文件维持私有权限；不从旧备份回滚撤销记录使凭证复活。

## 自动化与验收

在安装可选依赖的隔离开发环境运行：

```sh
python3 core/selftest-oauth.py
python3 core/selftest-cloud.py
python3 core/selftest-members.py --no-browser
python3 core/selftest-proposals.py --no-browser
python3 core/selftest-publication-integration.py
```

新增测试使用一次性身份、书库、SQLite 和实际子进程，不读取生产路径。包含 DCR、发现、PKCE、邀请码、CSRF、issuer/state、有效期、刷新/授权码重放与并发、身份区分、权限与通道、撤销、损坏拒绝、CLI、摘要存储、日志与重定向泄漏保护。实际 HTTP/MCP 流程模拟本机 TLS 终止代理，手工传递测试 Cookie；不能据此声称真实浏览器 HTTPS 或 ChatGPT 通过。

阶段 1 历史结果见 [OAUTH-VALIDATION.md](OAUTH-VALIDATION.md)，阶段 2 的真实浏览器、公网 HTTPS、资源预算、回滚和客户端进展见 [OAUTH-STAGE2.md](OAUTH-STAGE2.md)。后者同时说明可选浏览器依赖与复现命令。正式部署、数据映射与旧凭证撤销均需用户明确确认，不能由本轮自动执行。

## 回滚

1. 从代理撤下新 OAuth 主机的资源/授权路由，或将 MCP 环境 `READER_OAUTH_ENABLED=0` 后重启该 MCP 实例；确认裸新 `/mcp` 关闭且没有匿名回退。保持旧主机反代不动。
2. 保留独立 OAuth 数据库及审计，不恢复历史数据库覆盖新的撤销状态。需要永久停用连接时，先按公开授权 ID 撤销新授权。
3. 如需代码回退，仅恢复这次修改的 MCP 文件并移除新增 OAuth 挂接；原 Web、B5、身份、书库、进度和建议数据不回退。恢复到本分支之前的含 B5 基线，不能退回不认识 B5 发布代次的旧版本。
4. 验证原 001 Header Key、原 004 URL Key 和管理/阅读端。已完成关闭功能且未安装 OAuth 依赖的隔离回归；阶段 2 另演练了新测试 MCP 的关闭/恢复、旧接入兼容、撤销跨重启及数据保全。正式代理切换仍需部署时验证。

阶段 1 不撤销旧 URL Key。后续迁移必须先解决“身份活跃”与“旧凭据存在”的耦合，再单独审批和撤销不再依赖的旧凭据，避免停用仍需 OAuth 使用的原身份。
