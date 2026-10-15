# ReaderMate MCP 版（云端共读）

这是小书桌的 **MCP 版**源码包：沿用原 TXT 阅读器，增加云端多人共读、结构化划线和批注 MCP，以及身份令牌管理页。仓库原有 `core/` 桌面版保持独立；本目录可单独部署。

- `core/reader_server.py` 与 `core/web/reader.html`：书库、阅读进度、多人划线和批注。
- `core/mcp_server.py`：Streamable HTTP MCP，按身份令牌读取和写入共读数据。
- `token-mcp/`：管理员创建身份令牌的网页；创建成功时同时显示通用 MCP URL。
- `deploy/`：systemd、代理和密钥初始化示例；步骤见 [DEPLOY.md](DEPLOY.md)。

普通 MCP 客户端使用 `https://reader.example.com/mcp/<通用路径钥匙>`，并单独填写该身份的 Bearer 令牌。示例 ChatGPT 入口使用另一把 URL 钥匙，经 `https://mcp.example.com/mcp/<专用路径钥匙>` 映射到服务端的 `004` 身份，客户端 Auth 选 None。两种钥匙和身份令牌必须分别生成；不要把真实值提交到仓库。

源码中只保留示例域名和测试身份，不含生产书籍、划线、身份凭证、管理员口令、路径钥匙、服务器 IP 或部署配置。部署前替换 `*.example.com`，并在服务器上生成凭证。`004` 只是专用 URL 网关的示例身份名；若使用该网关，需先在令牌管理页创建此身份。

隔离测试：在 Linux 上安装 `core/requirements-mcp.txt` 后运行 `python3 core/selftest-cloud.py`、`python3 core/selftest-import.py`、`python3 token-mcp/selftest.py`，可按需运行原编码与 EPUB 转 TXT 自测。测试只使用临时书库和临时身份文件，不会读取生产数据。

阅读页先验证已有身份令牌，通过后才启动阅读器；MCP 和读写权限仍按原身份鉴权。书库页面支持按书名或文件名即时筛选，也支持导入 TXT 和 EPUB。服务端检查文件类型及内容，TXT 自动识别 UTF-8、带 BOM 的 UTF-16/UTF-32、GB18030 和 Big5；EPUB 使用原有转换器提取正文后以 UTF-8 TXT 入库。仅主人身份可以上传。单文件上限 64 MB，重名书籍返回 409，不会覆盖已有阅读进度或批注。

2026-10-15 增量：同一共享书库的身份可按书查询彼此的阅读进度；书库抽屉中的「共读进度」显示当前书的记录，「总览」跨书显示每个身份最近一次保存的进度。总览只读，不改变续读点。前端只有在用户明确点击「将当前位置设为续读点」时才保存进度，临时查看搜索结果或他人进度不会写盘。`GET /api/reading-progress/latest` 提供聚合查询，MCP 提供 `get_reading_progress` 与 `list_reading_progress`。旧版 MCP `update_progress` 仍可调用；直接写入 HTTP `/api/progress` 的客户端需要携带正文版本和预期修订号。详见部署文档。

进度卡片同时显示真实身份 ID 与阅读昵称，例如 `op001（001）`、`user（002）`；令牌管理页使用真实 ID。`001` 是 `op001` 的阅读昵称，并非另一名共读者。颜色与昵称仅用于展示，数据归属仍由服务端鉴权确认的 ID 决定。

当前 TTS 在未配置服务端供应商时使用设备浏览器的语音合成；可选服务端在线合成按段调用，音频只在进程内作有限临时缓存。本包不含本地语音模型，也不附带供应商密钥。
