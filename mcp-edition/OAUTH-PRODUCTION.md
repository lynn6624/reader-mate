# Token 页 OAuth 生产发布记录

日期：2026-11-04。用户明确确认新版清单后发布；部署版本 3ef03a32b1646eec827b6ab239433664c1ccbd9e。PR #11 仍为独立草稿，未合并。域名在本文及公开配置中脱敏，实际接入说明在私有交付目录。

## 已执行范围

复用原 Token HTTPS 主机的 /mcp，保持原管理页面及 /api/* 到 Token 后端，将精确 OAuth/well-known/MCP 路径分流到原 MCP 进程。没有新增 DNS 或证书，没有修改 Caddy，也没有撤销原 Header/URL 凭据。

更新 MCP/OAuth 模块及 Token 服务/界面，MCP 使用独立且与隔离验收快照完全一致的依赖环境，Token 保持系统 Python3.10 和标准库适配模块。新生产 SQLite 独立初始化，目录 0700、库 0600，服务属主 reader；从未复用测试身份/数据库。

切换仅重启 reader-mcp、token-mcp、cloudflared-reader。Web 和 Caddy MainPID 保持，身份、进度、批注三个正本 SHA 与发布前一致。原管理口令文件不在变更集合中，原环境文件摘要保持。

## 传输边界修复与重发

首次切换的 HTTPS 正向验证通过，但补充 HTTP 检查发现回环适配器把明确的访客 HTTP 也视作 TLS 终止请求。立即按预案恢复原代码/环境/隧道，保留新 OAuth 库及撤销状态，未恢复或覆盖业务数据。

修复后，仅可信回环代理的明确 https 协议值可通过；明确 HTTP、重复或未知协议值拒绝处理，协议头不能使非可信连接取得信任。启用 OAuth 管理后，管理页 HTTP GET 跳转 HTTPS，写请求拒绝。Cloudflare 覆盖客户端自行设置的协议头，见 [官方说明](https://developers.cloudflare.com/fundamentals/reference/http-headers/#x-forwarded-proto)；同时做了公网伪造该头的实际负向检查。原 MCP 主机的兼容接入行为保留，不能因此声称所有旧 HTTP 风险都已消失。

修复版本通过本地协议及管理端回归、Linux/Python3.11 与桌面/手机 Chromium CI 后重发；重新验证保留库，未重建或回退授权状态。

## 发布后已验证

| 项目 | 结果 |
| --- | --- |
| 公网 HTTPS | 公开 CA 验证开启，无证书例外；匿名 MCP 401 且带正确发现地址 |
| 协议发现 | Protected Resource、AS issuer/resource、S256 一致 |
| 管理页面 | 新邀请界面可获取，原管理员配置仍存在；无口令及错误 Origin 都为 403 |
| HTTP 与伪造协议头 | 新 MCP 400；伪造 https 协议头仍 400；管理页 GET 跳转 HTTPS，写请求 400 |
| 原 001 / 004 | 官方 MCP SDK 从可信回环使用原凭据验证：15 工具、调用指南及书单，身份分别 op001 / 004；不输出凭据/书单 |
| 数据及服务 | 身份/进度/批注正本及原环境摘要保持；Web/Caddy 不重启，五项服务 active |
| 日志与权限 | 四项相关服务近 45 分钟日志扫描未发现完整旧 Key 或 OAuth Token/邀请码；库权限 0600 |
| 部署资源事件 | 切换窗口内 memory.max/oom/oom_kill 无新增；该观察不是负载峰值测试，配额验收见新版隔离记录 |

脱敏 JSON 证据见 checks/oauth-production-20261104/。发布时原库保留且最终检查时未创建生产客户端、邀请或授权；管理员正常操作从原页面开始，不索取或重置原口令。

## 阅读页管理入口修复

用户反馈 Safari 从阅读页跳转后无法建立安全连接。核查发现阅读页仍使用公开版占位地址 token.example.com；实际 Token 域名的 IPv4/IPv6 TLS 证书校验和 HTTPS 页面访问正常，用户直接打开实际地址后确认可访问。

备份原 HTML 后仅将该链接替换为实际 HTTPS 管理地址。服务端真实 HTTP 返回页面已包含新链接，Web PID 不变，身份/进度/批注摘要保持。未更换证书或调整浏览器安全设置。新增 deploy/configure-token-link.py 供后续发布配置和检查链接；公开文档继续使用占位域名，不把部署专属地址写入通用代码。

链接修复可单独回滚：保留当前文件属主和权限，将本次保存的 reader-token-link-before.html 以原子替换恢复到 core/web/reader.html，再刷新页面；不覆盖 reader_server.py、OAuth 数据库或业务数据。该备份含原错误链接，仅用于故障恢复，正常发布应重新运行链接配置工具。

## 尚待用户验证

2026-11-05 更新：用户取得 ChatGPT 创建连接的具体错误：动态客户端注册端点返回403。逐文件对照成功的隔离版本后，OAuth服务器与存储逐字节一致，MCP与身份门禁统一换行后一致，依赖版本相同；授权路由唯一行为变化是新增访客协议校验。短时只读观察显示用户重试窗口内发现请求全部200、Host与https协议头匹配，没有DCR请求到达源站。

随后从VPS访问正式公网入口，发现GET为200，而POST /oauth/register和POST /mcp均返回HTML 403，带cf-mitigated: challenge，确认复现了Cloudflare边缘挑战。先前从另一客户端网络检查通过，不能代表ChatGPT或数据中心出口可用。正式客户端验收仍未通过；具体命中产品/规则须通过Cloudflare安全事件确认，不能只看到403就指定某一规则。

新增无凭证公网检查：`python3 core/check-oauth-public.py --issuer https://实际Token域名`。应分别从VPS和客户端网络运行。发现接口须为200；无效空JSON注册须由应用返回400 invalid_client_metadata，匿名MCP须401且带发现挑战。检查不生成客户端或读者授权、不传递口令或Token，不保存响应正文；403挑战必须作为失败。无效注册会占用一次注册限流计数，避免高频重复执行。

同一VPS出口、地址和空请求体的受控对比进一步确认请求特征差异：python-httpx请求头两次均为403挑战，诊断请求头返回正常JSON 401，简单Mozilla请求头仍为403。因此检查默认采用已固定HTTPX版本的请求头，可用--user-agent做对照；仅修改诊断请求头后通过不能宣称ChatGPT已修复，最终必须验证真实客户端。具体命中规则尚待安全事件核对，不将此结果当作已确定某种Bot产品或规则名称。

修复边缘规则时，仅对实际OAuth主机和明确的协议路径调整命中的挑战规则；根页面与/api/*管理端不在豁免范围。保留应用层邀请码、PKCE、CSRF、限流、Token验证和原身份门禁。具体策略须区分WAF/Super Bot Fight Mode与普通Bot Fight Mode；后者不能由WAF Skip规则跳过，见[Cloudflare说明](https://developers.cloudflare.com/bots/troubleshooting/false-positives/)。变更前保存原规则与顺序，回滚恢复该项规则；修复后重测公网POST、真实ChatGPT注册/授权及原004数据连续性。尚未修改Cloudflare策略，未关闭全站防护。

同日用户提供安全事件导出，与诊断请求的完整Ray ID精确匹配：POST /oauth/register、source=botFight、ruleId=bot_fight_mode、action=managed_challenge。现已确认命中普通Bot Fight Mode，上段的路径豁免设想不能通过普通WAF Skip实现。后续须选择独立直连HTTPS入口、支持精确豁免的防护方案，或经单独确认调整该域名的Bot Fight Mode；不能声称添加一条路径Skip即已修复。事件导出原件及IP/查询等字段不进入Git，仅保存脱敏摘要。生产策略尚未变更。

用户用原管理口令选择稳定 ID 004 生成一次性邀请，在真实 ChatGPT 中新增正式 OAuth URL，确认身份并读回原进度/建议，随后验证刷新和恢复。旧连接先保留；真实生产数据连续性不能用合成环境通过代替。

撤销单个 OAuth 客户端使用 OAuth 区的授权按钮；旧成员凭据的撤销会让原身份门禁失效，也会使对应 OAuth 请求失败。旧 URL 的最终停用仍是阶段 4 单独确认事项。

## 回滚与留档

服务器私有发布目录保存原始待改文件、属主/权限、环境及单元备份，备份不进入 Git。回滚恢复本次代码/新增配置/原隧道，仅重启同三项服务；不恢复历史阅读数据，不回退 OAuth 数据库及撤销记录，不删除新库来“重置”授权。后续故障按当前真实状态检查，不能盲目重复历史回滚脚本覆盖未来变更。
