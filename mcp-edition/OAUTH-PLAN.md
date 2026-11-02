# MCP OAuth 实施与验收设计

状态：阶段 0 设计快照。阶段 1 已在独立分支实现，当前可用接口和已执行结果见 [OAUTH.md](OAUTH.md) 及 [OAUTH-VALIDATION.md](OAUTH-VALIDATION.md)，尚未部署。需求见 [OAUTH-REQUIREMENTS.md](OAUTH-REQUIREMENTS.md)，现有代码审计见 [OAUTH-AUDIT.md](OAUTH-AUDIT.md)。本文保留初始拟议路径、配置名和测试设计，与实际实现存在差异的部分以阶段 1 文档为准。

## 入口与模块

建议在独立 HTTPS 主机启用新资源，例如 `https://oauth-mcp.example.com/mcp`，使用配置指定的 canonical resource；不从 Host 或 Forwarded 请求头推导 issuer。旧主机和旧私密路径继续使用原网关。新主机必须拒绝旧私密路径，否则整域反代可能暴露旧网关。

| 拟议端点 | 作用与检查 |
| --- | --- |
| `GET /.well-known/oauth-protected-resource/mcp` | RFC 9728 元数据，resource 精确指向新 `/mcp`；根 well-known 可返回同一资源元数据 |
| `GET /.well-known/oauth-authorization-server` | 固定 issuer、授权/Token/注册/撤销端点、S256、grant_types 与认证方式 |
| `GET /oauth/authorize` | 校验客户端、resource、scope、回调、state、S256；建立有时限的浏览器事务 |
| `POST /oauth/authorize` | 先证明邀请码资格，再显示身份与客户端回调并确认；拒绝 GET 执行授权 |
| `POST /oauth/register` | RFC 7591 公开客户端注册；只允许授权码及刷新；限制回调、字段、频率和总量 |
| `POST /oauth/token` | Authlib 执行授权码/刷新流程；原子消费；绑定原 client/resource/scope |
| `POST /oauth/revoke` | RFC 7009；只影响请求客户端的授权，未知 Token 也返回通用成功 |
| `/mcp` | 每个 HTTP 请求检查独立 OAuth Bearer；401 带 resource_metadata，scope 不足返回 403 |

通用模块拟为 `core/oauth_server.py`、`core/oauth_store.py`，身份适配拟为 `core/reader_oauth.py`，管理员本机操作拟为 `core/oauth_admin.py`。`core/mcp_server.py` 只做默认关闭的挂接。OAuth 可选依赖单独列入 `core/requirements-oauth.txt`，不强制旧接入安装。

采用 Authlib 授权码、PKCE 和刷新实现；实现前固定版本并核对 Python 3.11 生产兼容性。审计时 PyPI 可获取 Authlib 1.9.2；尚未验证其与现有运行环境的集成或内存开销，不构成已完成的选型验收。

首版可用 DCR 满足只添加 URL 的发现流程。公开客户端使用 `none` + S256，不把公开 client_id 或其自报品牌当作用户认证；注册回调严格解析并精确匹配，禁止通配符、fragment、userinfo 和公网 HTTP。授权页显示真实回调域名。需要限制客户端来源时，应从预注册白名单或经过审查的 CIMD 实现扩展，不能只匹配名字为 ChatGPT。CIMD 不能广告为已支持，除非已实现 HTTPS 元数据获取、SSRF/重定向/DNS 防护、缓存与回调验证。

## 持久数据与事务

SQLite 放在独立私有 state 目录，不放入 Git，也不与阅读 JSON 混用；备份需保护数据库、journal/WAL 和密钥文件。建议仅一个 MCP worker，所有安全状态以持久存储为准，不依赖进程内缓存判断撤销。

| 数据 | 关键字段及约束 |
| --- | --- |
| 客户端 | public client_id、精确回调集合、grant_types、创建时间；不接受客户端自定权限上限 |
| 邀请 | 随机独立 secret 的摘要、公开 invite_id、稳定 reader_id、有效期、剩余次数、撤销时间 |
| 浏览器事务 | 随机 Cookie 摘要、CSRF 摘要、已验证的 OAuth 参数、过期时间、待确认邀请引用、已消费状态 |
| 授权 | grant_id、原有稳定 reader_id、client_id、resource、scope、签发/到期/撤销时间 |
| 授权码 | 随机码摘要、grant_id、redirect_uri、S256 challenge、短有效期、已消费状态 |
| Token | 独立随机 access/refresh 摘要、grant_id、各自过期时间、refresh 已消费状态 |
| 审计 | 时间、事件类型、公开对象 ID、reader_id、结果；无原始请求体、URL 查询或完整凭证 |
| 限流 | 持久化全局与来源窗口计数及清理策略；拒绝客户端任意 X-Forwarded-For 绕过 |

默认建议：授权事务 10 分钟，授权码 60 秒，Access Token 15 分钟，刷新家族最多 30 天，邀请码一次性且默认一天过期；这些是待测试的配置默认值。

必须在事务内完成：确认授权并消费邀请码；校验/消费授权码并创建 Token；刷新轮换；授权撤销。并发只允许一个成功结果。刷新 Token 重放撤销同一授权家族，包括已签发的新 Access Token。读取 Token 时检查当前授权、期限、资源、scope 及身份状态；刷新不得扩大 scope、改变 reader_id 或 resource，不得无限延长家族最大寿命。

邀请码和 Token 均采用足够随机熵的独立值，摘要不可反推长期 Key。管理员 CLI 生成邀请时应将原值写入权限受限的指定输出文件，普通查询只显示公开 ID；不能默认打印到终端/日志。支持按公开授权 ID 查询和撤销，无须找回 Token 明文。

## 身份边界与适配

服务端仅接受现存且具有 MCP 访问资格的稳定身份，授权页显示稳定 ID 和可选昵称。旧代码的 `004` 与 `op004` 保持严格不同；正式映射必须通过只读核对确认。不得用 norm_who 清洗后回退到 user，也不得在找不到身份时创建或补齐身份。

新入口每次鉴权后，仅在服务端内部取得原身份凭证，复用旧 MCP 工具与 Web 二次认证。内部 API 目标必须限制为既有同机可信地址，拒绝跟随 HTTP 重定向转发凭证。原 Key 不进入 OAuth 响应或客户端元数据。此适配不将 OAuth Token 发给 Web 登录或其他服务。

先使用 `reader:mcp` 限定这套已有工具，其权限上限仍是原业务层。除 `user` 原有能力外，普通读者不能因 OAuth 获得导入/删除、审批、审计或查看他人私有建议的能力。跨读者公开信息按原规则可见；测试应区别合法共享信息与应隔离的数据，不能一概断言跨身份读取必须 403。

新主机 `/mcp` 不接受旧 Key，旧路径不接受 OAuth Token，Web 不接受 OAuth Token。撤销一个客户端授权不能撤销同身份其他授权或旧 Key；撤销/停用原身份应使所有相关 OAuth 访问失败。关闭 OAuth 开关不能悄悄退回匿名或旧 Key 模式。

## 浏览器与代理

- 使用随机浏览器事务、独立 CSRF、Secure/HttpOnly/SameSite Cookie；邀请码证明与最终授权分开，确认时再次检查有效期和使用次数。
- state 是客户端的 CSRF 关联值，服务端限制长度并原样返回；客户端负责比对，服务端另有浏览器 CSRF，不能用 state 替代。
- 对合法授权响应及可安全重定向的错误响应返回 `iss`，并广告 RFC 9207 支持；未验证回调的错误只在本页返回，不重定向。
- 页面 no-store、no-referrer、frame-ancestors 'none'，不载入第三方资源；Cookie 不含邀请、Key、Token 或可修改身份字段。
- 新主机的 MCP、well-known、OAuth 路径统一送到新入口，避免 Web catch-all；未知路径、错误 Host/Origin 和不受信任 Forwarded 头需拒绝。
- 仅外部 HTTPS；回环内部代理可用 HTTP，但只信任明确配置的代理，禁止请求头伪造公网 scheme 或 issuer。证书、代理错误日志、浏览器回调和 Token 端点都纳入端到端验收。

脱敏配置草案（尚未由代码读取；不复制到生产）：

```dotenv
READER_OAUTH_ENABLED=0
READER_OAUTH_ISSUER=https://oauth-mcp.example.com
READER_OAUTH_RESOURCE=https://oauth-mcp.example.com/mcp
READER_OAUTH_DB=/var/lib/reader-mate/oauth/oauth.sqlite3
READER_OAUTH_ACCESS_TTL=900
READER_OAUTH_REFRESH_TTL=2592000
# 如选用签名会话，密钥只配置文件路径，绝不在示例中放原值。
READER_OAUTH_SESSION_KEY_FILE=/etc/reader-mate/oauth-session.key
```

目录须 0700、私有文件 0600；示例不包含真实域名、邀请码或任何有效凭证。旧 READER_MCP 配置不因启用 OAuth 而被覆盖。域名/DNS/代理变更和生产开启仍需明确确认。

## 自动化测试设计

新增拟议 `core/selftest-oauth.py` 使用临时书库、临时身份、临时数据库及真实 HTTP/MCP SDK；任何测试不能默认读取生产路径。只有实现后实际通过，才能把对应状态从“待验证”改成“通过”。

| 测试组 | 必须覆盖 |
| --- | --- |
| 发现 | 匿名 401、WWW-Authenticate、well-known resource/issuer/端点一致；无数据/Key 泄漏 |
| 客户端 | 非法回调、重复参数、错误 client、非 S256、缺 resource、错误 scope 均拒绝；未验证回调不重定向 |
| 邀请/浏览器 | 正确、过期、失效、一次性、并发重用；CSRF、跨 Cookie、篡改事务、双击确认和超时 |
| 授权码 | 成功交换；错误 verifier/client/redirect/resource、过期、顺序/并发重放；失败不签发凭证 |
| Token | 过期、错误受众、缺 scope、刷新轮换、并发刷新、旧 refresh 重放撤销全家族 |
| 身份 | 004/op004/user 各自身份及进度；伪造 who/viewer_id 无效；公开与私有数据遵循原规则 |
| 业务连续性 | 先旧 Key 写进度和建议，再 OAuth 查询/继续写；旧 Key 再读，稳定 ID 和记录保持连续 |
| 撤销 | 只撤目标客户端；同身份另一授权和旧 Key 保持；身份停用/进程重启后不可复活 |
| 旧接入 | 001 Header、004 旧 URL、成员 mcp/mcp-url、错误 Host 和跨通道拒绝 |
| 保密/失败 | 页面、响应、日志、异常、Git 无完整凭证；存储损坏/缺失拒绝；代理重定向不泄漏内部 Key |
| 容量 | Linux/Python 3.11 测实际 RSS、slice 峰值、OOM/节流；保持原 256 MiB，不停其他服务腾额度 |

原有 `core/selftest-cloud.py`、`core/selftest-members.py`、`core/selftest-proposals.py` 应在隔离环境回归；涉及 B5 调用路径时追加对应 publication 集成验证。生产 HTTP、浏览器和证书测试不能被这些自动化测试替代。

## 真实兼容客户端验收

1. 独立测试域名、有效证书、独立 state 和合成书库；测试 viewer 不能指向生产数据，先检查服务配置清单。
2. 用管理员 CLI 给既有测试读者生成短期一次性邀请，不通过聊天或 Git 记录原值。
3. 在实际 ChatGPT 中只填无秘密的 HTTPS MCP URL；记录客户端类型/日期、发现结果及回调域名，不记录 state、code、邀请和 Token。
4. 浏览器验证邀请，确认准确稳定 ID 和权限后授权；完成回调及工具发现，实际读合成书、更新进度、提交测试建议。
5. 与同测试身份的旧 Key 比对进度/建议连续性；测试另一身份不能冒用或看到本不应可见的记录。
6. 等待 Access Token 到期，验证刷新/重新授权及断线恢复；撤销授权后，包括已连接客户端的后续请求和刷新都应失败。
7. 验证旧 001、旧 004、其他 OAuth 授权仍可按原规则访问；扫描脱敏日志和审计。
8. 留存分项结果和失败证据；SDK 成功只标记 SDK 成功。测试 ChatGPT、正式 ChatGPT、生产数据连续性分别留待验证状态。

正式阶段须用户明确确认后才部署并绑定原 004 身份；核对只读历史快照、上线后记录连续性，再进行真实客户端验收。任何测试不得清空生产进度来制造“连续性通过”。

## 回滚与迁移

1. 停用新 OAuth 域名的资源/授权路由或关闭新功能开关；确认没有退回匿名路径。
2. 保留独立 OAuth 状态和审计，按需撤销新授权。停止新 Token 签发与刷新，确认旧 Token 不再可访问。
3. 仅回退本功能代码/依赖和显式新增代理配置，恢复到包含 B5 保护的基线；不要从历史备份覆盖当前身份、进度、批注或建议。
4. 重启后验证原 001 Header 和原 004 URL、阅读端和管理端；核对隔离 Token 不能通过旧路由。
5. 在测试环境演练上述步骤并记录，再申请生产开启；不为了证明回滚而故意打断生产。

后续关闭旧入口另开明确迁移操作：先确认客户端依赖为零，再经用户确认撤销指定旧凭据；不能把关闭旧路径和撤销原身份混为一谈，也不能让原身份 Key 的撤销连带损坏仍需保留的 OAuth 授权。若当前身份存储将“身份活跃”与“Key 存在”绑定，须先设计独立身份活跃状态或独立旧凭据撤销机制并测试，再执行这一阶段。阶段 1 不做此迁移。
