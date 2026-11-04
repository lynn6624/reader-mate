# OAuth 并入原 Token 管理页

2026-11-04：按用户要求替代“新增 OAuth 域名、通过 CLI 发邀请”的上线草案。仍在 codex/mcp-oauth / PR #11 分支，尚未部署生产。阶段 2 验收记录保留为历史证据；新增管理界面不能据此冒称已部署。

## 使用方式

在原 Token 页面输入现有管理员口令，点击“查看 / 刷新 OAuth 授权”。选择一个已有、有效且允许 MCP 的稳定读者 ID，设置邀请码期限与使用次数，点击生成。默认 24 小时、一次性；邀请码仅在这次响应展示，可复制或立即隐藏。刷新/离开页面、修改口令会清空展示，不写入浏览器持久存储。

同时显示无密钥 MCP URL、邀请使用记录及客户端授权。邀请记录不返回原邀请码；授权记录不返回 Access/Refresh Token 或底层长期 Key。撤销邀请不撤销已经产生的授权；撤销客户端授权只影响所选授权家族，保留读者身份、进度、批注、建议及原凭据。界面要求显式确认撤销操作。

界面生成的是授权资格，不会创建新成员，也不会重做原登录体系。生产 004 应选择精确 ID 004，不改成 op004。公开 OAuth 回调和管理 API 是不同的路由，不能让管理员口令成为 OAuth 客户端凭证。

## 同域路径分流

推荐复用现有 Token 管理 HTTPS 域名，例如 token.example.com：

| 路径 | 后端 |
| --- | --- |
| /mcp、OAuth well-known、/oauth/authorize、/oauth/token、/oauth/register、/oauth/revoke | 现有 MCP 进程 127.0.0.1:8832 |
| /、/api/oauth/*、原有 /api/* | 现有 Token 管理进程 127.0.0.1:8860 |

Token 域名须与旧 Header/URL MCP 主机不同，符合当前已验证的主机隔离规则。无需新增 DNS 或常驻进程。若 Token 域名经 Cloudflare Tunnel，必须在旧整域规则前插入 OAuth 路径规则；单改 Caddy 不一定影响隧道路由。两种脱敏片段见 deploy/，不能用示例覆盖整份生产配置。

浏览器管理员请求继续使用 JSON 中的现有口令；拒绝不匹配的 Origin、非 JSON、URL 查询参数，OAuth 管理请求有独立持久限流。页面禁止 framing，响应 no-store / no-referrer。管理入口是受信任同源页面；不要在该域名托管第三方脚本或不受信任应用。

## 配置与实现

Token 服务显式设置 READER_OAUTH_MANAGEMENT=1，issuer/resource/DB 与 MCP 相同；TOKEN_PUBLIC_ORIGIN 指向实际管理页面 origin。默认关闭，旧成员管理独立运行。示例见 deploy/token-oauth.env.example。OAuth 库仍须先由本机 CLI 初始化，目录 0700、文件 0600、与 reader 账户权限一致；配置缺失/库损坏返回失败，不自动创建或替换库。

core/oauth_management.py 只加载标准库、member_store 和 oauth_store，避免在 Token 进程加载授权框架。两个进程共享 SQLite 事务和撤销状态，不另存 Token 副本；邀请码只保存摘要。每页最多 50 条邀请和 50 条授权，可翻页，避免把整个凭证库加载进管理进程。

新增管理员 POST API：/api/oauth/overview、invite、revoke-invite、revoke-grant。overview 接受从 0 开始的 page；invite 接受 reader_id、ttl（秒）及 uses，后两项默认 86400 / 1。生成及撤销复用现有审计，不记录请求体、口令或完整凭证。

## 验证与上线边界

core/selftest-oauth-management.py 验证真实管理 HTTP → 生成邀请 → OAuth 交换 → 官方 SDK 以 004 调用工具 → 独立撤销邀请和授权；覆盖管理员/Origin/格式校验、权限、分页、原身份文件不变、日志不泄密。真实 Chrome 测试覆盖桌面/手机生成与隐藏、无 localStorage、修改口令后清空和无水平溢出。可用 --no-browser 跳过浏览器，CI 分别运行两种模式。原 token-mcp/selftest.py 也加入 OAuth CI。

新版同域 HTTPS、管理接口并发、生产同款 Python3.12/3.10 组合及 256MiB 共享预算已在新合成环境复测通过，范围、采样误差、节流及脱敏证据见 [OAUTH-MANAGEMENT-VALIDATION.md](OAUTH-MANAGEMENT-VALIDATION.md)。实际 Tunnel 合并候选已离线验证，但未应用到生产。

管理端更新 server.py / index.html，新增轻量 oauth_management.py 及管理环境配置；需重启 Token 与 MCP，Web 不需重启。应用已有 Tunnel 的路径规则还需重启 reader 专用隧道，现有连接可能短暂重连，旧凭据继续有效，需纳入上线确认范围。生产公网路由及真实数据连续性仍须发布后单独验收。

回滚先禁用 READER_OAUTH_MANAGEMENT 并还原本次 Token 界面/服务文件及新增代理规则，保留 OAuth 数据库与审计；如同时关闭 MCP OAuth，按 OAUTH.md 关闭。只回退代码/新增配置，不回退期间产生的阅读数据，不撤销旧凭据。生产发布仍需用户明确确认，旧的独立域名审批包已失效。
