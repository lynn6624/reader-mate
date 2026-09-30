# 读伴 ReaderMate

把阅读器变成「AI 看得到你在读什么」的地方：进度落成文件、划线可批注，AI 读得到，也能在你划的那句下面回话。

## 仓库结构

| 目录 | 内容 |
|---|---|
| `core/` | 通用内核：`reader_server.py` + `web/reader.html`。Windows / Linux / Android 都能跑，只监听本机（默认 `127.0.0.1:8831`）。 |
| `operit/` | Operit 特供壳：把 `core/` 的页面挂进 Operit 主侧边栏（ToolPkg）。 |

## 它做什么

- 本地 txt 阅读器（手动导入、全文搜索、退出自动存进度）
- 进度与书库落成结构化 JSON，**AI 可读**
- 划线 + 批注：AI 能在你划的那句下面留话（批注按「权重」限长，中英文公平）
- 摘抄本可导出 markdown

## 它不是

- 不是网盘 / 云书架（不上传你的书）
- 不是书评工具（整本书级的感想交给文档）

## 快速开始

1. `core/`：`python3 reader_server.py`（纯标准库，无需 pip）
2. 浏览器打开 `http://127.0.0.1:8831/`
3. 想挂进 Operit 侧边栏 → 看 `operit/README.md`

## License

MIT
