# PR4 · 共读成员与凭据

管理员在原身份管理页面输入管理员口令，点击「查看 / 刷新成员」。添加成员时选择人类读者或 Agent、填写昵称；系统生成不可更名的 member_<随机值> 稳定 ID。同昵称可以创建多次，各有独立进度与作者身份。新成员没有管理书籍或发凭据的权限；user 仍是保留系统身份，不能撤销、重用或改名。

人类读者复制网页地址及令牌，在现有网页门禁中登录。网页沿用已有令牌存储与请求头方式，不新增 Cookie 登录体系。刷新后仍使用自己的身份。历史成员类型标记为 legacy，不猜测旧 ID 的人类或 Agent 类型；未知旧创建日期为 null。公开版本不内置私人昵称。

Agent 选择「URL + Bearer 令牌」或「仅 URL」。第一种在客户端分别填地址和令牌；第二种只填完整 URL，链接本身就是机密凭据。仅 URL 需要已经配置的灰云 HTTPS 主机，转发全部 /mcp/ 子路径且关闭路径访问日志；不是把 Bearer 令牌随意拼到旧通用地址上。已有专用 004 URL 和普通 MCP 连接保持兼容。

管理页仅在创建响应中展示新凭据，不写 localStorage/sessionStorage，不在列表或日志回显。可点击「我已保存，隐藏凭据」。网页阅读器为维持既有登录，会按原机制保存读者自己输入的门禁令牌。提交成功后若网络中断导致未收到凭据，请刷新成员列表核对，撤销那项凭据后另建成员；本期不提供旧凭据取回或轮换。

## 存储与权限

仍只有 data/identities.json：旧的 ID→令牌字符串值不变，增加保留键 `$members: {version:1,members:{ID:...}}`。该键不属于合法身份 ID 字符集。每项含 nickname、kind、created_at、credential（id/channel/created_at/revoked_at）。P0 每位新成员发一项凭据，通道为 web、mcp 或 mcp-url；旧凭据为 legacy，保留原接入范围。元数据不复制令牌。文件整体为600权限。

创建在同一进程/跨进程锁内读取、校验、修改、备份，最后一次原子替换整个文件，成员及凭据没有跨文件提交窗口。首次操作会记录现存旧成员的元数据，保留其 ID、凭据和未知创建时间。损坏、重复键、重复令牌、状态不一致时拒绝写入。原始字节备份在私有 .token-backups/identities-<SHA256>.json。临时文件先 flush/fsync；替换失败原文件不变。进程在替换前后中断可读取完整旧版或新版；任意硬件断电/文件系统故障不在已验证保证中。

撤销移除该 ID 的有效令牌值，保留成员元数据、credential.id 和 revoked_at。所有后续认证重新读取文件，因此下一个请求拒绝，重启后仍撤销。已经通过认证的在途请求可能完成，已下载到浏览器的正文不能远程收回。旧 DELETE /api/identities/<ID> 现在执行撤销，不能重建同 ID。不要绕开事务手改文件；第三方旧管理脚本必须停用或升级，旧管理服务不能与新版同时写。

新 web 凭据可网页登录，不能接入 MCP 网关；新 mcp/mcp-url 凭据不能通过网页门禁。共同阅读数据 API 继续验证绑定身份（MCP 本来就调用这些 API），并非两个独立的权限空间。身份类型不提升权限；旧接口中的 who=user/ai 只是兼容显示角色，新人类评论保存 who=user 且 author_id 为其独立 ID。MCP 工具的写入身份继续来自认证，前端自称 author_id 不生效。

昵称/类型在已认证的 /api/auth-check 返回，不含凭据详情；管理员列表才提供凭据状态和凭据 ID。所有当前共读者继续共享既有书库授权范围。

## API 与配置

- POST /api/members：password、nickname、kind=human|agent、channel=web|mcp|mcp-url。仅管理员；返回 member、一次性 token、url、instructions。人类只能配 web，Agent 配后两种。客户端传来的 id/admin 不生效。
- POST /api/members/list：password；返回所有已登记成员及凭据状态，不含令牌。
- POST /api/credentials/revoke：password、member_id、credential_id；ID 不匹配409，user409，不存在404，错误口令403。
- 旧 POST /api/identities {name,password} 兼容；旧 DELETE 变为保留历史的撤销。旧 GET /api/status 仍只有有效身份名和服务状态。
- 新网关 /mcp/member/<随机凭据> 仅接受配置的灰云 Host。错误主机404、撤销/错误凭据401。新网关遇到显式不匹配的 Authorization 也拒绝。原 004 固定适配器的既有映射保持不变。
- PR4 本身无新 MCP 工具、无 Schema 变化，发布时为12工具；后续 [协作修订](REVISIONS.md) 增至15个。get_calling_guide 和 read_book 保存进度提醒不变，已有连接不需重配。

令牌管理服务和 MCP 应使用同一份数据目录及正确的配置：

```
READER_WEB_PUBLIC_BASE=https://reader.example.com/
READER_MCP_PUBLIC_BASE=https://reader.example.com/mcp/
READER_MCP_URL_PUBLIC_BASE=https://mcp.example.com
READER_MCP_EXTRA_HOST=mcp.example.com
```

READER_MCP_PATH_KEY_FILE 继续指向已有通用路径钥匙文件。缺少连接地址或钥匙时，在写文件前返回503，不创建半成品。member_store.py 需与 reader_server.py 同目录；管理服务自动查找相邻 core 或原 /opt/reader-mate/core，特殊目录可设 READER_CORE_DIR。无新依赖或常驻服务。

## 部署、排障、回退

停管理/Web/MCP 三服务，备份代码、identities.json 和现有数据，部署模块与对应三个入口、两个页面，然后一起启动。无需批量迁移 TXT、进度或批注；部署本身不写身份数据。更改前核对实际基线。个人昵称仅保留在私有部署配置/页面，不发布到通用源码。

症状：启动失败提示 user 缺失。检查可信私有身份备份，恢复正确保留凭据；不要关闭云端鉴权或新建一个不同 user。症状：创建失败503。检查身份文件校验、目录权限、连接域名及路径钥匙权限。症状：MCP404。检查 HTTPS 主机与 READER_MCP_EXTRA_HOST 一致、代理完整转发路径，非标准端口需显式配置 Host/Origin 白名单，不直接关防护。症状：已撤销成员仍显示批注。这是预期，撤销的是访问凭据，历史保留。后续昵称更改只改元数据，无需手改 progress.json。

在第一次成员创建/撤销前，可以仅恢复原代码，数据原样保留。只要身份文件已经发生操作，就不要运行旧管理服务：它不理解元数据，可能删历史或重用 ID；保留新版存储/鉴权，使用兼容修复前进，必要时暂时停管理服务。禁止用旧 identities/notes/progress 覆盖新记录。备份包含凭据，不能打进公开源码。

## 验收与后续

selftest-members.py 使用临时数据启动真实 HTTP/MCP，覆盖同昵称不同 ID、人类进度/评论归属、不能管理分类、两种 Agent 接入、旧004、撤销后拒绝/重启、失败不留成员、备份、损坏拒写、五进程并发、日志/列表不泄漏。默认还用 Playwright/Chrome 验证手机和桌面创建/登录/刷新/实际发批注；--no-browser 可在 Linux 执行其余测试。

旧保全、MCP/HTTP、阅读前端和目录/EPUB 回归需一并运行。真实 GPT 新成员的客户端手动连接仍需用户验收；旧004连接不要求重配。P1 昵称编辑、轮换、同成员增发凭据及管理审计展示未实现；后续草稿见 [DRAFTS.md](DRAFTS.md)，建议与审批见 [REVISIONS.md](REVISIONS.md)，正式发布仍未实施。
