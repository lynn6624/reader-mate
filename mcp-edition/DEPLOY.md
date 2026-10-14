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

通用 MCP URL 为 `https://reader.example.com/mcp/<state/mcp-path-key.txt 中的值>`；客户端还需填写自己的 Bearer 身份令牌。令牌管理页只会在管理员成功创建身份后同时返回这两项。示例 ChatGPT 专用 URL 为 `https://mcp.example.com/mcp/<state/mcp-004-url-key.txt 中的值>`，Auth 选 None；服务端会按现有 `004` 身份令牌鉴权。此 URL 本身可调用 004 的读写工具，应像口令一样保管；泄露时轮换专用路径钥匙并重启 MCP 服务。不要在代理访问日志中记录完整 MCP 路径。

## 4. 验收与回滚

先运行隔离测试，再在私密环境下验证：匿名 `/api/*` 为 401，裸 `/mcp` 为 404，通用带钥匙 URL 无 Bearer 为 401，正确 Bearer 能列出 MCP 工具并按章节读取划线；专用 URL 仅在指定灰云 Host 下以 `004` 身份工作。测试主人可导入 TXT，普通共读身份不能导入或删除书籍。调整字号和刷新后划线仍按原文定位。

升级前保存应用代码与数据的独立备份；失败时停服务并恢复旧代码，再启动。若变更了数据结构，需按该版本的迁移说明处理，不要简单用旧数据覆盖新数据。本版新增部署不需要数据库迁移；生产数据目录和密钥文件不应由源码包覆盖。
