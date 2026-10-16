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

## 2. 配置服务

以 `deploy/reader.env.example` 为模板创建 `/etc/reader-mate.env`，填自己的两个 MCP Host 和通用 URL 基址。文件权限设为 0600。默认路径钥匙文件位于 `/opt/reader-mate/state/`。若使用其他位置，需同时配置 `READER_MCP_PATH_KEY_FILE` 与 `READER_MCP_004_KEY_FILE`。

把 `deploy/reader-mate.slice`、`reader-web.service`、`reader-mcp.service`、`token-mcp.service` 安装到 `/etc/systemd/system/`，核对其中的安装路径，执行 `systemctl daemon-reload` 后启动三项服务。Web 端口 8831、MCP 端口 8832、令牌管理端口 8860，均只绑回环地址。当前 JSON 写锁只在单个 Web 进程内生效，不要让两个 Web 实例写同一数据目录。

## 3. HTTPS 与域名

`reader.example.com` 指向 Web 阅读页；其 `/mcp/*` 路由转发到 8832，其余路径到 8831。`token.example.com` 转发到 8860。两者可通过 Cloudflare Tunnel 橙云代理，示例见 `deploy/cloudflared-ingress.example.yml`。需要 ChatGPT 的无挑战直连入口时，把 `mcp.example.com` 设为灰云 DNS-only A 记录，使用 Caddy 将 HTTPS 443 转发到 8832，示例见 `deploy/Caddyfile.grey.example`。安全组只开放 443；不要开放 8831、8832、8860。

Kindle 的 `/k/<一次性码>` 也必须转发到 Web 8831。Web 服务使用 `READER_MCP_PUBLIC_HOST` 生成 `https://reader.example.com/k/...`；若阅读页使用不同域名，可设 `READER_KINDLE_PUBLIC_BASE=https://reader.example.com`。两者都必须指向同一 Web 服务并使用 HTTPS。不要记录完整配对路径。

通用 MCP URL 为 `https://reader.example.com/mcp/<state/mcp-path-key.txt 中的值>`；客户端还需填写自己的 Bearer 身份令牌。令牌管理页只会在管理员成功创建身份后同时返回这两项。示例 ChatGPT 专用 URL 为 `https://mcp.example.com/mcp/<state/mcp-004-url-key.txt 中的值>`，Auth 选 None；服务端会按现有 `004` 身份令牌鉴权。此 URL 本身可调用 004 的读写工具，应像口令一样保管；泄露时轮换专用路径钥匙并重启 MCP 服务。不要在代理访问日志中记录完整 MCP 路径。

## 4. 验收与回滚

### Kindle 设备配对

主人 `user` 用现有身份令牌进入阅读页，在书库抽屉底部生成配对链接；只在受信任的 Kindle 浏览器打开并点击确认。GET 只显示表单，POST 才一次性兑换。设备 Cookie 为 `Secure`、`HttpOnly`、`SameSite=Lax`，有效期 90 天；服务端只保存随机会话密钥的摘要。主人可列出并撤销 Kindle 会话，设备不能凭 Cookie 创建新配对、导入或删书、读取审计。设备的进度和批注仍归 `user（002）`，MCP 的 Bearer 令牌不变。

隔离验证运行 `python3 core/selftest-kindle.py`、`python3 core/selftest-cloud.py` 和浏览器测试。上线前备份 `core/reader_server.py` 与 `core/web/reader.html`；先核对哈希，再原子替换并重启 `reader-web.service`。新文件 `core/data/kindle_auth.json` 在首次生成链接时懒创建，权限应为 0600，目录归 `reader` 且为 0700；无需数据库迁移。代码回滚时保留此文件与其他数据；旧版忽略设备会话，恢复新版代码后会话可继续使用。若需立即使所有 Kindle 会话失效，应先在主人页面逐台撤销。

已配对设备访问 Web 根路径时自动转到 `/kindle`；该页不依赖 JavaScript、滑动或固定高度滚动容器，使用大触屏按钮翻上一页/下一页。它从 `user（002）` 的现有 UTF-16 进度接续；每次点按通过签名表单提交到 `/kindle/page`，复用 `POST /api/progress` 的正文版本与修订号检查，冲突时不覆盖手机的新进度。旧书正文变化时显示定位异常，不会静默移到另一段。目录可按书名搜索；正文只按页读取，内存里最多保留两本书的稀疏定位索引，不另建书籍副本或进度表。完整原阅读页可通过 `/?full=1&kindle=1` 手动打开。KPW6 是否能舒适点按翻页需真机验收。

### 阅读进度增量

`GET /api/reading-progress?name=<书名>` 查询当前身份的进度；加 `reader_id=<身份 ID>` 可查询共读身份，加 `all=1` 返回该书所有已有记录。`GET /api/reading-progress/latest` 返回各身份跨书最近一次保存的进度；所有接口沿用身份令牌鉴权。MCP 的 `get_reading_progress(name, reader_id?)`、`list_reading_progress(name)` 对应结构化查询。总览不批量返回正文，也不写进度。

新版 HTTP `POST /api/progress` 要求 `book_revision` 与 `expected_revision`，冲突返回 409，缺少版本字段返回 428。进度仍保存在原有 `progress.json`，新增版本与定位字段采取懒迁移：读取旧记录不批量改盘，首次成功保存才补齐。因此无需数据库迁移。不要用旧备份的 `progress.json` 整文件回滚，否则会丢失上线后保存的进度。只回滚代码时应保留当前书籍、进度、划线、批注及身份文件。

新增隔离验收可运行 `python3 core/selftest-cloud.py`，在具备 Playwright 浏览器依赖的环境运行 `python3 core/selftest-browser.py` 与 `node core/selftest-progress-ui.js`。检查手机和桌面的进度显示、临时跳转不写盘、正文变更后拒绝错位续读，以及匿名请求被拒绝。`core/selftest-browser.py` 的 Chrome 路径是 Windows 测试机示例，Linux 上需按环境调整。

进度页若要让身份昵称与令牌管理页对齐，只需替换 `core/web/reader.html`。例如 `op001（001）` 中前者是真实身份 ID，后者是阅读昵称；这项显示修复不改进度或身份数据，也无需重启会按请求读取 HTML 的 Web 进程。替换前备份原页面，失败时仅恢复该页面并重新读取网页即可。

阅读页先通过 `/api/auth-check` 验证现有身份令牌，验证成功后才启动阅读器并读取书架；没有令牌或令牌失效时只显示身份令牌输入门。它复用现有身份令牌，不另设访客口令，也不改变 API、MCP 的鉴权规则。`/api/ping` 仍供健康检查使用，不返回书籍或共读数据。

部署此门禁只需更新 `core/reader_server.py` 与 `core/web/reader.html`，无需数据库迁移。升级前备份这两个文件；若门禁页面或验证接口异常，将备份按原路径恢复并重启 `reader-web.service`。不要恢复或覆盖 `core/data/`、`core/书库/` 或 `state/`。

隔离测试包括 `python3 core/selftest-import.py`；再在私密环境下验证：阅读页要求有效身份令牌后才启动；书库页可按书名或文件名筛选并显示匹配数量；匿名 `/api/*` 为 401，裸 `/mcp` 为 404，通用带钥匙 URL 无 Bearer 为 401，正确 Bearer 能列出 MCP 工具并按章节读取划线；专用 URL 仅在指定灰云 Host 下以 `004` 身份工作。测试主人可导入 TXT 或 EPUB（后端转换为 UTF-8 TXT），普通共读身份不能导入或删除书籍。TXT 自动识别常见编码；重名书籍拒绝覆盖。调整字号和刷新后划线仍按原文定位。

升级前保存应用代码与数据的独立备份；失败时停服务并恢复旧代码，再启动。若变更了数据结构，需按该版本的迁移说明处理，不要简单用旧数据覆盖新数据。本版新增部署不需要数据库迁移；生产数据目录和密钥文件不应由源码包覆盖。
