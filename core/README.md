# 读伴 · ReaderMate

> 本地 txt 阅读器，进度落文件，AI 读得到。

读伴（英文 **ReaderMate**）是一个零依赖的本地 txt 阅读器：单文件 Python 后端 + 单页 HTML 前端，跑在 `127.0.0.1`，数据全部落在项目目录下。**它的设计目标不是替代 Kindle，而是让你和 AI 都能直接读到「我在读什么、读到哪了」。**

- 仓库名：`reader-mate`
- 许可证：MIT
- 后端：Python 3.8+ 标准库（**不装任何第三方包**）
- 前端：单页 HTML + 原生 JavaScript（无构建步骤）

---

## 它解决什么问题

- **不被任何平台绑住**：txt 本地文件，进度本地 JSON。你换设备、换系统、关掉云账号，书和阅读记录都还在自己手里。
- **进度你能自己掌控**：阅读进度写在 `data/progress.json`，表格版同步写在 `data/阅读进度.md`。想备份就拷文件，想迁移就用自带的 `tools/backup.py`。
- **AI 能直接读你的进度**：把项目目录交给一个能读文件的 AI，它看 `progress.json` 就能回答「你上次读到哪了」「读到百分之多少」「当时是哪一章」。不需要导出、不需要轮询、不需要插件。
- **零依赖、零配置**：后端纯标准库，前端是单页 HTML。解压即跑，不装 pip 包，不装 npm 包，不装数据库。

---

## 功能清单

### 阅读体验

- **长文滚动阅读**：窗口宽度自适应（中英文皆宜），大长篇也流畅（懒渲染：每次只画 200 段）。
- **主题切换**：纸色（暖色） / 暗色 双主题，点击顶栏 `☾` 切换，偏好自动记住。
- **字号调节**：`A-` / `A+` 按钮实时调，范围 13px–34px。
- **章节自动识别**：识别 `第 X 章 / 第 X 节 / Chapter N` 形式的标题，展示在顶栏、写入进度；不做目录树。
- **键盘快捷键**（在阅读区生效）：

  | 按键 | 动作 |
  | --- | --- |
  | `Space` / `PageDown` / `j` | 下翻一屏 |
  | `PageUp` / `k` | 上翻一屏 |
  | `Home` | 回到开头 |
  | `/` | 打开搜索 |
  | `Esc` | 关闭抽屉 / 清除高亮 |

### 进度与书库

- **自动续读**：滚动停下 1.2 秒自动存进度；关页面时用 `sendBeacon` 最后存一次。下次点开同一本书，直接跳回上次位置，并显示当时所在段落开头 40 字作为「锚文本」。
- **书库侧栏**：列出所有 txt，按「最近阅读时间」倒序，显示百分比条与最近章节。
- **多种导入方式**：侧栏点「＋ 导入 txt」多选文件；或直接把 txt 文件拖进浏览器窗口。
- **离线导入**：把 txt 直接拷到项目目录下的 `书库/`，然后点侧栏 `↻` 重新扫描即可。文件原本是 GBK / Big5 的老书会自动识别，落盘统一存为 UTF-8。
- **删除**：书库侧栏每本右上角的 `×` 删除该本，同时清掉这本书的阅读进度。

### 搜索

- **全文搜索**：点 `🔍 搜索` 或按 `/`，输入关键词回车，返回所有命中位置（带前后文片段），最多 800 条。
- **跳转到命中处**：点击搜索结果，页面跳到该位置，并把关键词用黄色高亮框出。
- **同一本书内搜索**：搜索范围是当前打开的那一本；切书后自动切到新书的全文。

### 工具链

- **epub → txt 转换**：`tools/epub2txt.py`，纯标准库，支持单文件、多文件合并、目录批量。
- **数据备份与迁移**：`tools/backup.py`，把 `书库/` 与 `data/` 打成单一 zip（含 `manifest.json` 与校验信息），可在另一台机器原样还原。
- **HTTP API**（后端 `reader_server.py` 直接提供）：

  | 方法 | 路径 | 作用 |
  | --- | --- | --- |
  | GET | `/api/books` | 书库列表（含每本进度） |
  | GET | `/api/book?name=<文件名>` | 取一本书的正文与当前进度 |
  | GET | `/api/progress` | 读完整进度对象 |
  | GET | `/api/ping` | 存活检查（**免鉴权**，给保活脚本 / 健康检查用，不返回任何数据） |
  | POST | `/api/import?name=<文件名>` | 上传一个 txt（请求体即文件内容） |
  | POST | `/api/progress` | 写一本的进度（JSON 体） |
  | POST | `/api/delete?name=<文件名>` | 删除一本书（连带进度） |

> **鉴权**：设了环境变量 `READER_TOKEN` 后，所有 `/api/*` 都需要 token，接受 `?token=xxx` 或请求头 `X-Token: xxx`，比对用 `hmac.compare_digest`；不设则不校验（向后兼容）。`GET /` 与 `GET /api/ping` 始终允许无 token 访问（前者方便手机第一次打开，后者给保活脚本做健康检查）。详见 [INSTALL.md §5.3](INSTALL.md)。

---

## 快速开始

读伴是「解压即跑」的。三步走：

### 1. 准备 Python

- 需要 **Python 3.8 或更高**。
- Windows：从 [python.org](https://www.python.org/downloads/) 下载安装包，安装时勾上「Add Python to PATH」。
- Linux / macOS：一般系统自带；没有请用发行版包管理器装（apt / dnf / brew）。

### 2. 解压项目

把 `reader-mate` 项目目录放到你电脑的任意位置。**避免放在需要管理员权限才能写入的目录**（如 `C:\Program Files\`）。

### 3. 启动

**Windows**：

双击 `start-windows.cmd`。它会后台拉起服务并自动打开浏览器到 <http://127.0.0.1:8831>。
想停就双击 `stop-windows.cmd`。

**Linux / macOS**：

在项目目录下执行：

```bash
bash start.sh
```

默认端口 **8831**。停止：

```bash
bash stop.sh
```

服务起来后，浏览器访问 <http://127.0.0.1:8831>。

> 想要实时看后端输出而不是后台模式？直接 `python reader_server.py`（默认 `127.0.0.1:8831`）。`Ctrl+C` 停。

### 4. 开始用

1. 浏览器打开后会看到空状态页。点左上「☰ 书库」→「＋ 导入 txt」选文件，或直接把 txt 拖进窗口。
2. 列表里点一本开读。
3. 读到哪算哪；下次打开自动续上。

---

## 目录结构

```
reader-mate/
├─ reader_server.py          ← 后端（唯一必需的 Python 文件）
├─ web/
│  └─ reader.html            ← 前端单页（所有 UI 与逻辑都在里面）
├─ tools/
│  ├─ epub2txt.py            ← epub 转 txt 转换器（独立可执行）
│  └─ backup.py            ← 数据备份 / 还原工具（独立可执行）
├─ start.sh / stop.sh        ← Linux / macOS 启停脚本
├─ start-windows.cmd / stop-windows.cmd   ← Windows 启停脚本
├─ README.md                 ← 本文件
├─ INSTALL.md                ← 高级安装指引（端口、locale、容器环境等）
├─ LICENSE                   ← MIT 许可证
│
├─ 书库/                     ← 你的 txt 放在这里（首启动自动创建）
│  └─ *.txt                  ← 文件名即书名
└─ data/                     ← 数据目录（首启动自动创建，**别手改 JSON**）
   ├─ progress.json          ← 阅读进度正本（AI 读的）
   ├─ 阅读进度.md             ← 同进度的表格版（人 / AI 扫一眼用）
   ├─ library.json           ← 书库索引（字节数、字符数、mtime）
   ├─ server.log             ← 后端 stdout（start.sh 后台模式才有）
   └─ server.err.log         ← 后端 stderr（同上）
```

**哪些是自动生成的**：`书库/`、`data/` 全部由首次启动创建；`data/server.log`、`data/server.err.log` 仅在用 `start.sh` / 启动脚本后台模式运行时产生。

**哪些不要手改**：`data/progress.json` 与 `data/library.json` 是后端管理的，手改会被下一次写入覆盖。要修内容请改 `data/阅读进度.md` 或直接编辑 `书库/` 里的 txt。

---

## 让 AI 读你的进度

这是读伴相对其他阅读器最不一样的地方——**进度落文件、AI 直接读**。

### 进度字段说明（`data/progress.json`）

```json
{
  "version": 1,
  "updated": "2026-09-30T15:22:10",
  "books": {
    "某本书.txt": {
      "offset": 12345,        // 当前读到第几个字符（从 0 开始）
      "percent": 0.4218,      // 0~1，已读比例
      "chars": 29283,         // 本书总字符数（来自书库索引）
      "chapter": "第七章       旧事新词",  // 当前所在章节标题（最长 80 字）
      "anchor": "那年冬天，她第一次走进了那间...",  // 当前段落开头 40 字（锚文本）
      "updated": "2026-09-30T15:22:10"  // 本次保存时间
    }
  }
}
```

`offset` 是「按规范化后的 `\n` 文本」计算的字符偏移——同一本书在不同设备间迁移时，**只要两边的文本一致（导入方式相同），offset 就精确指向同一个位置**。

### 表格版（`data/阅读进度.md`）

后端在写 `progress.json` 的同时，会自动重生成一份 Markdown 表格版：

```markdown
| 书 | 进度 | 字符偏移 | 章节 | 锚文本 | 更新时间 |
| --- | --- | --- | --- | --- | --- |
| 某本书.txt | 42.2% | 12345/29283 字 | 第七章       旧事新词 | 那年冬天，她第一次走进了那间… | ... |
```

`阅读进度.md` 是给人看和给 AI 一眼扫的；**正本始终是 `progress.json`**。

### 一个用法示例

把读伴的项目目录交给一个能读文件的 AI（你常用的对话 AI），让它读 `data/progress.json`，直接问：

> 「我最近在读什么？读到百分之多少了？上一章的锚文本接着讲什么？」

它读 `progress.json` 就能回答。如果想要最近读的几本汇总，让它读 `data/阅读进度.md`（表格）更省 token。

---

## 数据备份与迁移

用 `tools/backup.py`。三个子命令：

```bash
# 导出：把 书库/ 与 data/ 打成 zip（含 manifest.json + 每文件 sha256 摘要）
python tools/backup.py export
python tools/backup.py export -o 我的备份.zip
python tools/backup.py export --books-only        # 只打包书，不打包进度
python tools/backup.py export --progress-only     # 只打包进度，不打包书
python tools/backup.py export --note "搬去新电脑"

# 看一眼备份包里有什么
python tools/backup.py info 我的备份.zip

# 还原：默认按"updated 时间"合并进度（同一本两边都有时取较新的一条），
#       同名书默认跳过，加 --overwrite 才覆盖
python tools/backup.py import 我的备份.zip
python tools/backup.py import 我的备份.zip --overwrite
python tools/backup.py import 我的备份.zip --dry-run   # 只打印计划，不写盘
```

包里有一份 `manifest.json`，记了：

- 导出时间（ISO 格式）
- 源操作系统（`windows` / `macos` / `linux` 等）
- 书数 / 总字符数 / 进度条目数
- 每个文件的 sha256 前 16 位与字节数

`info` 子命令只读 manifest 就能给概要，不会把整本书加载进内存。`import` 子命令有 zip-slip 防护（拒绝绝对路径、含 `..`、跳出项目根的成员），并自动跳过 `__MACOSX/`、`.DS_Store`、`Thumbs.db` 这类平台噪音。

---

## epub 怎么转 txt

读伴只认 txt。epub 先用 `tools/epub2txt.py` 转：

```bash
# 单文件：输出到与 epub 同名的 .txt
python tools/epub2txt.py 某本书.epub

# 单文件，指定输出路径
python tools/epub2txt.py 某本书.epub -o 书库/某本书.txt

# 多文件：合并到一个文件
python tools/epub2txt.py a.epub b.epub -o 合集.txt

# 批量转一个目录下所有 epub
python tools/epub2txt.py ./待转目录/
```

转换器按 `container.xml → opf → spine` 的顺序抽正文，剥掉 `script` / `style` / `nav`，反转义实体，段落之间留空行。**纯标准库实现**，不需要 `pip install`。

### 已知局限

这是**有意做轻**的代价，不打算在 V1 修复：

- **不做 CSS 渲染**：epub 里用 CSS `display:none` 藏起来的内容也会被抽进 txt（多数情况无害，但偶有注释混入）。
- **图片、表格、数学公式全部丢失**：只输出文字。
- **目录模式不递归**：命令行传目录时只扫顶层 `.epub`，不进子目录。
- **`<pre>` 内的等宽对齐不保留**：`pre` 内的内容会保留换行，但不再按等宽字体对齐显示。
- **章节标题不加编号**：从 `dc:title` 拿到的书名原样写到 txt 开头；正文里的章节识别由阅读器归前端做。

如果你的 epub 对这些情况敏感，建议用其他工具（Calibre 等）转一遍再导入。

---

## 已知局限与路线图

### V1 故意没做的

- **没有划线、批注**：选中文字不会有浮动菜单。
- **没有目录树**：侧栏只列书名，不进章节。要翻到指定内容请用搜索（`/`）。
- **没有翻页模式**：只能滚动阅读。
- **没有多套配色**：只有纸色与暗色两套。
- **没有 txt 编辑 / 广告清理**：脏文本只能整本替换。

### V2 计划

见本仓库 issue 列表。设计思路上参考过 [watersalt0305/CoRead](https://github.com/watersalt0305/CoRead)（AGPL-3.0）的若干做法（划线批注交互、配色偏好模型、AI 协作通路等），**但本仓库不含其任何代码**——参考只用于设计取舍，所有实现都是自己写的。

---

## 设计参考声明

读伴在设计思路上参考了 [watersalt0305/CoRead](https://github.com/watersalt0305/CoRead) v2.6.0（**AGPL-3.0**）项目。

- **参考的内容**：交互设计思路、数据建模取舍、AI 协作通路的设计模式。
- **不参考的内容**：实现代码。CoRead 是纯前端 + localStorage 的宿主插件；读伴是 Python 标准库后端 + 文件落盘。两者在「如何让 AI 读到用户的阅读数据」这一核心命题上走了完全不同的路。
- **许可证说明**：本仓库的代码与文档均为 MIT 许可证，与 CoRead 的 AGPL-3.0 不构成派生关系。如果你需要引用 CoRead 的具体代码片段，请遵循其 AGPL-3.0 条款。

---

## 许可证

MIT。详见 [LICENSE](LICENSE)。