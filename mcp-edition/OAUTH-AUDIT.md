# MCP OAuth 阶段 0：只读审计与实施方案

日期：2026-11-02（北京时间）。基线：`f38ce92fb91388e1a1e67619eac44ce377d593f0`，分支：`codex/mcp-oauth`。

## 范围与证据

审计对象为远程 main 的 `mcp-edition/`，已对照本地 B5 上线留档。工作区旧 `src/` 和历史发布包不是当前基线。未连接生产主机、读取生产密钥、修改身份数据、部署或撤销凭证。

| 位置 | 当前行为与影响 |
| --- | --- |
| `core/mcp_server.py` | FastMCP 1.30.0，15 个工具；`IdentityGate` 对带私密路径的入口检查 Bearer；裸 `/mcp` 返回 404；无 OAuth 元数据 |
| 同文件 `ChatGPT004Gateway` | 旧 004 私密 URL 转为已有身份凭证；成员 URL 凭证必须匹配 `mcp-url` 通道及指定 Host |
| 同文件 `credentials` / `api` | 服务端解析身份；向回环 Web 服务转交身份凭证，Web 再次判断身份和业务权限；不可仅在新入口信任 viewer_id |
| `core/reader_server.py` | `resolve_who` 由服务端钥匙映射决定身份，忽略自报 who；进度、批注、建议及管理权限继续由此执行 |
| `core/member_store.py` | 兼容旧身份及成员通道；成员撤销和显示名独立；无通用 OAuth 授权存储 |
| `deploy/cloudflared-ingress.example.yml` | 仅 `/mcp` 路由到 MCP；新增 well-known 与授权端点必须单独规划，不能落到 Web catch-all |
| `deploy/Caddyfile.grey.example` | 示例整域代理到回环 MCP；私密路径不应记录访问日志 |
| `core/selftest-cloud.py` / `selftest-members.py` | 可复用真实 HTTP、官方 MCP SDK、隔离临时身份、进度与通道回归基础 |
| B5 上线留档 / `PUBLICATION.md` | 256 MiB 总配额；已有发布代次时不能退到无 B5 恢复保护的代码 |

**身份名必须精确保留。** 旧网关明确绑定字符串 `004`，测试同时存在 `op004`。二者不能自动视为同一人，也不能迁移或重命名进度键；管理员邀请码必须绑定实际存在的稳定 ID，页面显示该 ID，验收以服务器真实映射为准。

## 协议依据

实施时官方 latest 指向 [MCP 2026-07-28 Authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)。采用 RFC 9728 受保护资源发现、RFC 8414 AS 元数据、RFC 7636 S256、RFC 8707 资源绑定、RFC 9207 issuer 响应及 RFC 7009 撤销。

[OpenAI 官方授权文档](https://developers.openai.com/plugins/build/auth)支持预注册客户端、DCR 和 CIMD；不能把旧的 URL Key 连接成功当作 OAuth 客户端验收。[Authlib](https://docs.authlib.org/en/latest/oauth2/authorization-server/flask/grants.html)提供维护中的授权码与刷新令牌实现，应复用其协议校验而不手写 OAuth 状态机。

## 推荐架构

新增独立公开 HTTPS 主机上的无密钥 `/mcp`，与旧 001/004 接入并行。新入口默认关闭，只有明确配置 issuer、私有存储和安全会话密钥后启用；新入口只接受 OAuth Bearer，不接受旧 Key、query token 或客户端自报身份。

1. 通用层：Authlib 授权码与强制 PKCE S256；客户端注册及精确回调绑定；SQLite 事务化保存授权码、授权、Token 生命周期与撤销；访问 Token、刷新 Token 和邀请码只保存摘要；刷新轮换并检测重放。
2. 身份适配层：管理员通过本机 CLI 创建邀请码，指定已存在且可使用 MCP 的身份；邀请证明资格后，单独确认绑定的稳定身份、客户端回调和权限。不会创建新阅读身份，不写原 identities.json。
3. 受保护资源：每次请求检查 Token、资源、scope、授权撤销和当前身份可用性，再在服务器信任边界内映射到原身份凭证；已有 Web API 再次校验原凭证及所有业务权限。客户端始终只接收独立 OAuth 凭证。
4. 通道隔离：新 Token 不可用于旧 Key 路由或 Web 登录；旧 Key 不可用于新 OAuth 入口。新入口不允许跨主机落入旧 URL 网关。现有身份被撤销时，对应 OAuth 也应立即失效。

初版资源权限可用 `reader:mcp` 表达现有 15 工具集合，不能扩大任何业务权限；`offline_access` 只在 AS 广告，不作为资源必需 scope。后续需要更细权限时再拆读写 scope，不能广告尚未落实的权限粒度。

## 风险与控制

- 授权码与刷新并发：数据库事务确保只消费一次；刷新重放撤销整个授权家族。
- CSRF / 重定向劫持：浏览器事务绑定安全 Cookie、独立 CSRF、短时限；OAuth state 原样返回；回调精确匹配注册值，禁止 fragment、userinfo 和非 HTTPS 公网 URI。
- 邀请码猜测：高熵随机值、摘要存储、有效期/次数限制、持久化全局与来源限流；失败不泄漏具体身份。
- 客户端滥用：只允许授权码和刷新，公开客户端使用 none+PKCE；注册限制数量、字段大小和频率，不能让客户端元数据声明可信品牌。
- 凭证与日志：所有响应 no-store；浏览器页面不载入第三方资源、禁止 framing、设置 no-referrer；代理和应用访问日志关闭或专门脱敏，不能记录完整 URL、表单、Authorization、邀请码和 Token。
- 存储损坏或缺失：拒绝授权；生产 Linux 目录 0700、文件 0600；独立于身份、进度和书库。
- 资源紧张：不增加独立常驻大型 IdP；即使复用 MCP 进程，新增依赖也需要在隔离环境测量峰值，不能凭代码体积宣布符合 256 MiB。
- 既有 URL 凭证仍存在独立传输风险；OAuth 上线不等于旧凭证已安全，也不得提前撤销。

## 预计改动与阶段边界

阶段 1：新增可复用 OAuth 模块、Reader-Mate 适配模块、CLI、可选依赖、默认关闭的 MCP 挂接、脱敏代理/环境示例；预计 8–12 个文件。原 Web/阅读/建议/发布业务保持原实现。

阶段 2：新增授权码、PKCE、身份串号、并发重放、过期、刷新、撤销、日志扫描和旧路由回归；使用临时数据、真实 HTTP 与 MCP SDK。提供真实 ChatGPT 连接的逐项验收记录模板，未实测项必须标记未验证。

阶段 3：必须先获得测试 HTTPS 入口与实际兼容客户端验收，再由用户明确确认正式部署；将邀请码精确绑定既有 004 稳定 ID，核对进度/建议连续性和断线恢复。不得把 SDK 通过冒充 ChatGPT 通过。

阶段 4：仅在用户确认旧 URL 已无依赖时单独撤销；001 Header Key 可长期保留。通用 OAuth 层可抽取，Leto 的 OAuth 客户端角色需另做适配。

## 验证与回滚原则

实施需提供自动化测试及隔离 E2E 命令、匿名发现与旧入口回归、配置与安全存储检查。真实 HTTPS 证书、OpenAI 网络可达性、客户端 UI、生产数据连续性、256 MiB 预算需要各自实测，不能由本地单元测试代替。

回滚先关闭新 OAuth 入口/代理，撤销新授权或停用新功能开关，再恢复这次功能的代码及可选依赖；保留 OAuth 审计与撤销记录，不覆盖任何期间新进度、建议或 identities.json，不回退 B5 保护。旧入口及旧凭证不依赖新 OAuth 存储，保持原接入。
