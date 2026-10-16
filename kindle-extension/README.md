# Kindle 阅读扩展（可选）

此扩展只适用于带 `KINDLE_EXTENSION_*` 页面占位符的同版 ReaderMate MCP 核心。没有 Kindle 的用户不需要安装；基础阅读器、共读与 MCP 都可独立运行。安装后主人 `user` 可以生成一次性配对链接，已配对设备继续使用原来的共享进度。

## 安装

1. 先部署匹配版本的 `core/reader_server.py` 与 `core/web/reader.html`，备份这两个代码文件。不要覆盖 `core/data/`、`core/书库/` 或 `state/`。
2. 将扩展包中的 `kindle.py`、`kindle.css`、`kindle-panel.html`、`kindle-init.js`、`kindle.js` 一起放进 `core/extensions/`。缺一个文件就不要启动。`selftest-kindle.py` 放在 `core/`。
3. 重启 **Web 服务**。`GET /api/ping` 的 `extensions` 应含 `kindle`；主人登录后，书库底部出现 Kindle 配对入口。运行 `python3 core/selftest-kindle.py` 可用临时书库隔离检查配对、翻页、撤销及进度冲突。
4. 公网阅读域名的 `/k/*`、`/kindle` 与 `/kindle/page` 都转发到同一 Web 服务。云端应使用 HTTPS；如需单独设配对域名，配置 `READER_KINDLE_PUBLIC_BASE`。配对地址有效 10 分钟且仅可使用一次。

扩展不增加第三方 Python 依赖，也不修改 MCP 服务或进度数据格式。首次配对时才创建 `core/data/kindle_auth.json`；服务端只保存设备会话密钥摘要，不把主人身份令牌交给 Kindle。

## 卸载与回滚

先备份扩展目录和代码；停止 Web 服务后移走整个 `core/extensions/`，再启动。原阅读页不会显示配对入口；Kindle 路由对已授权请求返回 404。**保留** `core/data/kindle_auth.json`，重新安装同版扩展后原设备会话可继续使用。若需要让设备立刻失效，应在卸载前通过主人页面逐台撤销会话。

若新版本有问题，只恢复备份的代码与扩展文件并重启 Web。不要用旧数据目录回滚进度、书籍、批注、身份或设备会话。
