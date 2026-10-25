# MCP 版部署

以下为单机 Linux 示例。Web、MCP 和令牌管理服务均监听 `127.0.0.1`；公网仅开放反向代理的 HTTPS。示例域名需要换成自己的域名，真实凭证只在服务器生成。

## 1. 安装代码与依赖

创建独立的 `reader` 系统用户，将本目录安装到 `/opt/reader-mate/`，保留 `core/`、`token-mcp/`、`deploy/` 结构。建 `core/data/`、`core/书库/`、`state/`，属主为 `reader`；数据目录与 `state/` 限制为 0700。Python 3.11+：

```sh
cd /opt/reader-mate
python3 -m venv .venv
.venv/bin/pip install -r core/requirements-mcp.txt
sudo -u reader python3 deploy/init-identities.py
sudo -u reader python3 deploy/init-mcp-keys.py
sudo -u reader python3 deploy/init-token-bootstrap.py
```

初始化脚本遇到已有文件会保留或拒绝覆盖；迁移旧实例时先备份 `core/data/`、`core/书库/`、`state/`，切勿拿空目录覆盖。`init-identities.py` 只建主人 `user` 身份；其他共读身份在令牌管理页创建。主人令牌和一次性初始化码仅从服务器私下取用，不放入 URL 文档、日志或仓库。

把 `core/MCP工具标准调用参考_只读.txt` 以相同文件名复制到 `core/书库/`，设为 root 属主、0444 权限；先确认书库没有同名用户文件。云端上传与删除接口会拒绝改动此参考书。MCP 工具本身从 `core/` 读取它；若升级时更换内容，应同时更新两份文件。它不是数据库迁移，其他书籍和阅读数据不需要改动。

## 2. 配置服务

以 `deploy/reader.env.example` 为模板创建 `/etc/reader-mate.env`，填自己的两个 MCP Host 和通用 URL 基址。文件权限设为 0600。默认路径钥匙文件位于 `/opt/reader-mate/state/`。若使用其他位置，需同时配置 `READER_MCP_PATH_KEY_FILE` 与 `READER_MCP_004_KEY_FILE`。

把 `deploy/reader-mate.slice`、`reader-web.service`、`reader-mcp.service`、`token-mcp.service` 安装到 `/etc/systemd/system/`，核对其中的安装路径，执行 `systemctl daemon-reload` 后启动三项服务。Web 端口 8831、MCP 端口 8832、令牌管理端口 8860，均只绑回环地址。当前 JSON 写锁只在单个 Web 进程内生效，不要让两个 Web 实例写同一数据目录。

## 3. HTTPS 与域名

`reader.example.com` 指向 Web 阅读页；其 `/mcp/*` 路由转发到 8832，其余路径到 8831。`token.example.com` 转发到 8860。两者可通过 Cloudflare Tunnel 橙云代理，示例见 `deploy/cloudflared-ingress.example.yml`。需要 ChatGPT 的无挑战直连入口时，把 `mcp.example.com` 设为灰云 DNS-only A 记录，使用 Caddy 将 HTTPS 443 转发到 8832，示例见 `deploy/Caddyfile.grey.example`。安全组只开放 443；不要开放 8831、8832、8860。

**仅安装可选 Kindle 扩展时：** `/k/<一次性码>`、`/kindle`、`/kindle/page` 也必须转发到 Web 8831。Web 服务使用 `READER_MCP_PUBLIC_HOST` 生成 `https://reader.example.com/k/...`；若阅读页使用不同域名，可设 `READER_KINDLE_PUBLIC_BASE=https://reader.example.com`。两者都必须指向同一 Web 服务并使用 HTTPS。不要记录完整配对路径。

通用 MCP URL 为 `https://reader.example.com/mcp/<state/mcp-path-key.txt 中的值>`；客户端还需填写自己的 Bearer 身份令牌。令牌管理页只会在管理员成功创建身份后同时返回这两项。示例 ChatGPT 专用 URL 为 `https://mcp.example.com/mcp/<state/mcp-004-url-key.txt 中的值>`，Auth 选 None；服务端会按现有 `004` 身份令牌鉴权。此 URL 本身可调用 004 的读写工具，应像口令一样保管；泄露时轮换专用路径钥匙并重启 MCP 服务。不要在代理访问日志中记录完整 MCP 路径。

## 4. 验收与回滚

### 2026-10-21 书架分类升级

备份 `core/reader_server.py`、`core/mcp_server.py`、`core/web/reader.html`、调用参考 TXT 和 `core/data/library.json`；校验后替换上述代码及代码目录/书库的两份调用参考，按 Web 后 MCP 顺序重启。保留其余数据目录、书籍、令牌与路径钥匙。可选 Kindle 扩展只在原本安装时同步同版扩展；默认发行包不启用 Kindle。

没有独立数据库迁移。首次目录扫描在 `library.json` 的每本记录补 `category`、`category_source`，旧书默认 `uncategorized`；手动修改会记 `category_updated_at`、`category_updated_by`。六个 ID 为 `literature`、`web-fiction`、`nonfiction`、`science-tools`、`humanities`、`uncategorized`。`GET /api/books?category=...` 和 MCP `list_books({"category":"..."})` 支持筛选；原无参调用仍返回全部目录。管理员 `POST /api/books/category` 提交 JSON `{ "name":"原文件名.txt", "category":"literature" }`；普通身份和 Kindle 设备会话不能调整。上传接口 `POST /api/import?name=...&category=...` 校验枚举；旧脚本省略分类时归待分类，空值或无效值拒绝。前端确认框每本必选分类后才提交。

第六组补丁只替换 `reader_server.py`、`mcp_server.py` 及服务目录/书库两份调用参考 TXT，再重启 Web/MCP。折叠书架、管理和上传选择器、Kindle 扩展均读取同一分类表，无需另改页面结构。`nonfiction` 仅显示名称变为「随笔与纪实」，不更换 ID；`humanities` 新增后由用户自行分书，不修改已有类别或分类来源。升级前后可对比每本记录的所有 `category*` 字段，确认人工结果保持。回滚到五类版前，须先由用户决定如何处置升级后新设为 humanities 的书籍，并保留最新索引快照，避免旧分类白名单将其归待分类；不要恢复旧进度或批注数据。

运行 `core/selftest-import.py`、`core/selftest-cloud.py`、`core/selftest-browser.py`；安装扩展时另跑 `core/selftest-kindle.py`。测试使用临时书库，覆盖默认折叠、搜索、批量/逐本分类、权限、失败清理和既有阅读数据保留。回滚前单独保留升级后的 `library.json` 快照，因为旧代码重新扫描会丢弃新分类字段；只恢复代码和参考文档，不用旧书库或进度数据回滚。重装新版时按现存文件名合并快照中的分类字段即可保留人工结果。

### Kindle 设备配对（安装可选扩展后）

主人 `user` 用现有身份令牌进入阅读页，在书库抽屉底部生成配对链接；只在受信任的 Kindle 浏览器打开并点击确认。GET 只显示表单，POST 才一次性兑换。设备 Cookie 为 `Secure`、`HttpOnly`、`SameSite=Lax`，有效期 90 天；服务端只保存随机会话密钥的摘要。主人可列出并撤销 Kindle 会话，设备不能凭 Cookie 创建新配对、导入或删书、读取审计。设备的进度和批注仍归 `user`，MCP 的 Bearer 令牌不变。

隔离验证运行 `python3 core/selftest-kindle.py`、`python3 core/selftest-cloud.py` 和浏览器测试。上线前备份 `core/reader_server.py` 与 `core/web/reader.html`；先核对哈希，再原子替换并重启 `reader-web.service`。新文件 `core/data/kindle_auth.json` 在首次生成链接时懒创建，权限应为 0600，目录归 `reader` 且为 0700；无需数据库迁移。代码回滚时保留此文件与其他数据；旧版忽略设备会话，恢复新版代码后会话可继续使用。若需立即使所有 Kindle 会话失效，应先在主人页面逐台撤销。

已配对设备访问 Web 根路径时自动转到 `/kindle`；该页不依赖 JavaScript、滑动或固定高度滚动容器，使用大触屏按钮翻上一页/下一页。它从 `user` 的现有 UTF-16 进度接续；每次点按通过签名表单提交到 `/kindle/page`，复用 `POST /api/progress` 的正文版本与修订号检查，冲突时不覆盖手机的新进度。旧书正文变化时显示定位异常，不会静默移到另一段。目录可按书名搜索；正文只按页读取，内存里最多保留两本书的稀疏定位索引，不另建书籍副本或进度表。完整原阅读页可通过 `/?full=1&kindle=1` 手动打开。KPW6 是否能舒适点按翻页需真机验收。

2026-10-16 页面适配补丁：当前 Kindle 正文字号与页长绑定为常规 18px／440 字、大字 20px／340 字、特大 22px／280 字。KPW6 真机反馈常规字号下 440 字合适，但字号放大后同样页长会溢出；因此放大时自动缩短。标题、导航、字数选项和冲突提示保持紧凑，只有正文放大。该 GET 选项不保存进度，翻页表单会签名并保留所选档位；旧的 360／400 字链接回落到当前默认值。完整阅读页为已配对设备显示固定的「返回 Kindle 阅读」链接，即使 Kindle 不执行 JavaScript 也可返回；原阅读器脚本会清理临时 `kindle=1` 与 `full=1` 参数。部署本版只需替换 `core/reader_server.py`，备份、原子替换并重启 `reader-web.service`。无需数据迁移；保留 `core/data/`、书库及会话文件。

2026-10-16 进度冲突补丁：当其他设备抢先写入同一身份的进度，Kindle 的翻页请求改为重定向到目标页并标记 `p=1`，不再展示阻断式冲突页。暂停状态下的点按翻页只移动本机阅读位置，不写共享进度；页面持续显示「暂未同步」及手动同步按钮。手动同步使用当前页服务端修订号和签名表单，若另一设备再次抢先保存，仍保持暂停且不覆盖其位置。只需替换 `core/reader_server.py` 并重启 `reader-web.service`；不修改数据库、`core/data/`、书库或设备会话。回滚只恢复备份的后端代码。

### 阅读进度增量

`GET /api/reading-progress?name=<书名>` 查询当前身份的进度；加 `reader_id=<身份 ID>` 可查询共读身份，加 `all=1` 返回该书所有已有记录。`GET /api/reading-progress/latest` 返回各身份跨书最近一次保存的进度；所有接口沿用身份令牌鉴权。MCP 的 `get_reading_progress(name, reader_id?)`、`list_reading_progress(name)` 对应结构化查询。总览不批量返回正文，也不写进度。

新版 HTTP `POST /api/progress` 要求 `book_revision` 与 `expected_revision`，冲突返回 409，缺少版本字段返回 428。进度仍保存在原有 `progress.json`，新增版本与定位字段采取懒迁移：读取旧记录不批量改盘，首次成功保存才补齐。因此无需数据库迁移。不要用旧备份的 `progress.json` 整文件回滚，否则会丢失上线后保存的进度。只回滚代码时应保留当前书籍、进度、划线、批注及身份文件。

新增隔离验收可运行 `python3 core/selftest-cloud.py`，在具备 Playwright 浏览器依赖的环境运行 `python3 core/selftest-browser.py` 与 `node core/selftest-progress-ui.js`。检查手机和桌面的进度显示、临时跳转不写盘、正文变更后拒绝错位续读，以及匿名请求被拒绝。`core/selftest-browser.py` 的 Chrome 路径是 Windows 测试机示例，Linux 上需按环境调整。

公开版默认显示服务端确认的真实身份 ID，不再将 user/op001/op004 映射成特定部署者的阅读昵称。已有部署可仅备份并替换同版 `core/web/reader.html`，刷新页面即可；无需重建令牌或修改数据。若仅回退显示修复，恢复原页面即可。不要跨版本替换整份数据目录。

阅读页先通过 `/api/auth-check` 验证现有身份令牌，验证成功后才启动阅读器并读取书架；没有令牌或令牌失效时只显示身份令牌输入门。它复用现有身份令牌，不另设访客口令，也不改变 API、MCP 的鉴权规则。`/api/ping` 仍供健康检查使用，不返回书籍或共读数据。

部署此门禁只需更新 `core/reader_server.py` 与 `core/web/reader.html`，无需数据库迁移。升级前备份这两个文件；若门禁页面或验证接口异常，将备份按原路径恢复并重启 `reader-web.service`。不要恢复或覆盖 `core/data/`、`core/书库/` 或 `state/`。

隔离测试包括 `python3 core/selftest-import.py`；再在私密环境下验证：阅读页要求有效身份令牌后才启动；书库页可按书名或文件名筛选并显示匹配数量；匿名 `/api/*` 为 401，裸 `/mcp` 为 404，通用带钥匙 URL 无 Bearer 为 401，正确 Bearer 能列出 MCP 工具并按章节读取划线；专用 URL 仅在指定灰云 Host 下以 `004` 身份工作。测试主人可导入 TXT 或 EPUB（后端转换为 UTF-8 TXT），普通共读身份不能导入或删除书籍。TXT 自动识别常见编码；重名书籍拒绝覆盖。调整字号和刷新后划线仍按原文定位。

升级前保存应用代码与数据的独立备份；失败时停服务并恢复旧代码，再启动。若变更了数据结构，需按该版本的迁移说明处理，不要简单用旧数据覆盖新数据。本版新增部署不需要数据库迁移；生产数据目录和密钥文件不应由源码包覆盖。

### 2026-10-20 手机选区朗读修复

本版 `core/web/reader.html` 将选区朗读按句接续，修复长段落时只读第一句、跟随滚动不前进的问题。替换前备份原页面，运行 `python3 core/selftest-tts.py`（需要 Playwright 与 Chrome）及原有浏览器/云端测试。部署只需原子替换页面并刷新浏览器，不需要重启服务或迁移数据。回滚仅恢复此前备份的页面；保留书库、进度、批注与令牌。

## notes兼容保护

升级前先备份并运行隔离验收。该修复只替换代码，首个正常notes修改会在data/notes-backups保存一份私有原件；不要把它、notes.json.lock或任何生产数据上传Git。无批量迁移，原TXT与进度不改写。损坏存储会返回503并保留原文件，管理员应核查可信副本，不要初始化空库。回滚不能用旧notes覆盖新评论；升级后已有新notes时，旧有损规范化代码也须拒绝回退。保留user不可删除；当前云端缺少其配置仍会拒绝启动，恢复时核对私有备份及原凭据，不能关闭鉴权。详见 [PRESERVATION.md](PRESERVATION.md)。


PR4 共读成员管理：管理员可添加人类读者或 Agent、生成独立凭据并撤销。旧 ID 与连接保留；部署配置、兼容存储和回退限制见 [MEMBERS.md](MEMBERS.md)。


管理员草稿编辑与快捷设章：选区编辑自动保存独立草稿，目录可直接新增/修改/删除；原书发布另行开发。使用、接口、限制与回退见 [DRAFTS.md](DRAFTS.md)。

## 2026-10-25 阅读进度保全

更新 MCP 版 `core/reader_server.py`，保留同目录已有 `member_store.py`。先暂停相关写入并备份代码和数据，再替换代码、重启与只读核验；部署不批量重写进度。首次正常保存会在私有 `data/progress-backups/` 备份原始字节。所有进度写入者都须使用新提交协议；损坏文件返回503，旧快照返回409，不得初始化空库或恢复旧整库覆盖新记录。

JSON 已提交但 Markdown 镜像失败时，保存仍成功，可从当前正本重建镜像。恢复命令、备份权限、旧代码回退风险及未验证边界见 [PROGRESS-PRESERVATION.md](PROGRESS-PRESERVATION.md)；逐项核对和测试见 [TAKEOVER-20261025.md](TAKEOVER-20261025.md)。本次开发未执行生产部署。

## 协作修订与人类审批

基于上述进度保全版本及 PR5 草稿，新增建议队列、3个MCP工具和阅读器内「修订审批」。部署需同时更新 Web、MCP、草稿模块、审批模块、前端和调用参考；完整文件清单、隔离启动步骤与代码回退说明见 [REVISIONS.md](REVISIONS.md)。保留原有鉴权和数据目录。接受建议只改变共享草稿，不发布正式 TXT；不要恢复旧数据来撤销代码升级。
