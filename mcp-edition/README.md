# ReaderMate MCP 版（云端共读）

这是小书桌的 **MCP 版**源码包：沿用原 TXT 阅读器，增加云端多人共读、结构化划线和批注 MCP，以及身份令牌管理页。仓库原有 `core/` 桌面版保持独立；本目录可单独部署。

- `core/reader_server.py` 与 `core/web/reader.html`：书库、阅读进度、多人划线和批注。
- `core/mcp_server.py`：Streamable HTTP MCP，按身份令牌读取和写入共读数据。
- `token-mcp/`：管理员创建身份令牌的网页；创建成功时同时显示通用 MCP URL。
- `deploy/`：systemd、代理和密钥初始化示例；步骤见 [DEPLOY.md](DEPLOY.md)。

普通 MCP 客户端使用 `https://reader.example.com/mcp/<通用路径钥匙>`，并单独填写该身份的 Bearer 令牌。示例 ChatGPT 入口使用另一把 URL 钥匙，经 `https://mcp.example.com/mcp/<专用路径钥匙>` 映射到服务端的 `004` 身份，客户端 Auth 选 None。两种钥匙和身份令牌必须分别生成；不要把真实值提交到仓库。

源码中只保留示例域名和测试身份，不含生产书籍、划线、身份凭证、管理员口令、路径钥匙、服务器 IP 或部署配置。部署前替换 `*.example.com`，并在服务器上生成凭证。`004` 只是专用 URL 网关的示例身份名；若使用该网关，需先在令牌管理页创建此身份。

隔离测试：在 Linux 上安装 `core/requirements-mcp.txt` 后运行 `python3 core/selftest-cloud.py`、`python3 token-mcp/selftest.py`，可按需运行原编码与 EPUB 转 TXT 自测。测试只使用临时书库和临时身份文件，不会读取生产数据。
