# v0.3 简化协作修订

本次以用户最新简化要求为准：每本书沿用 PR5 的一份共享管理员草稿；Agent 提交建议，人类管理员审批后才能应用。基线为 PR1～PR5 已合并的 `fb89a03` 加阶段0进度保全 PR #6 的 `f3a33d3`。本功能叠加在 PR #6 上，不重复施工阶段0。2026-10-26 合并前修复审批键盘隔离和手机顶栏布局；部署需要另行执行。

## 复用与最小增量

| 能力 | 复用位置 | 本次增量 |
| --- | --- | --- |
| 身份与管理员权限 | `core/reader_server.py:resolve_who`、`member_store.py` | 服务端确定提交者；仅 `user` 可预览草稿、审批 |
| 唯一共享草稿 | `core/book_drafts.py:create/load/edit/change_chapter/save` | 抽出有界元数据读取；接受记录随原草稿保存 |
| 目录与原文坐标 | `core/navigation.py`、既有 UTF-16 坐标及草稿 operations | 按正式原文定位，顺序映射到当前草稿，重叠则拒绝 |
| MCP 鉴权和 HTTP 客户端 | `core/mcp_server.py:api` | 新增3工具，原12个工具签名保持 |
| 阅读器与草稿入口 | `core/web/reader.html`、`editor.js` | 新增按钮与 `review.js` 审批弹层 |

新增 `core/revision_proposals.py` 保存建议、检测冲突和组合审批操作；`reader_server.py` 只增加对应路由与原子提交编排。没有新编辑器、身份系统、独立 Agent 草稿或常驻服务，没有新增运行依赖。

## 实际操作

1. Agent 使用自己的现有凭据，先 `read_book` 取得正式正文与 `book_revision`，找到准确片段。阅读工具仍只读；校对建议不会调用 `update_progress`。
2. 调用 `propose_edit` 或 `propose_chapter`。返回 `proposal.id`、`proposal.status=pending`。建议保存书名、认证身份、正式版本、UTF-16 位置、原文及短上下文、建议内容、理由和时间；重复提交同一内容会返回同一个ID与当前状态。
3. 管理员登录阅读器、打开目标书，进入「书库 → 修订审批」。按待审/接受/拒绝/冲突筛选，分页每次20条。可查看原文、修改后文本、提交者、理由和上下文，点「预览并审批」。可手动修改替换文字或章名后接受，也可拒绝。
4. 接受后点现有「草稿」查看结果；若审批前已在草稿模式，页面刷新同一草稿窗口。拒绝只记录状态。章节调整改变草稿目录；删除章节条目不会删除正文，父项的子项按 PR5 规则提升一级。

普通身份只查询自己的建议，没有审批入口，也不能读取管理员草稿；管理员可查询本书全部建议。Kindle 配对会话不能提交或审批。提交者不能用参数伪装成其他身份。Agent 应持独立成员凭据，不应持主人 `user` 凭据。

## 工具与 HTTP

| MCP 工具 | 参数要点 | HTTP |
| --- | --- | --- |
| `propose_edit` | `name, book_revision, operation, quote`；可选 `replacement, reason, start, context_before, context_after` | `POST /api/proposals/edit` |
| `propose_chapter` | `name, book_revision, operation`；可选 `quote, title, chapter_id, target_title, parent_title, reason, start, context_before, context_after` | `POST /api/proposals/chapter` |
| `list_proposals` | `name`；可选 `status, offset=0, limit=20`，最多50条 | `GET /api/proposals` |

正文 operation 为 `replace/delete/insert_before/insert_after`。插入必须用非空原文片段作锚点；`replacement` 是新增文字。删除不带替换文字。章节 operation 为 `add/update/delete`；新增用 quote 定位，修改/删除用准确 ID 或唯一 `target_title`。修改时省略 quote 保留原章节位置；`parent_title` 省略保留原上级、空字符串改为顶层，非空选择唯一上级标题。

quote 必须逐字匹配。重复片段须加紧邻前后文，或指定 UTF-16 `start`；不猜测模糊匹配，不能截断 emoji。所有坐标和版本均取正式正文；Agent 无需读取私有草稿。

仅管理员可调用 `POST /api/proposals/preview`（name、id）与 `POST /api/proposals/review`（name、id、action）。接受需带预览返回的 `expected_revision`，可选手工 `replacement` 或 `title`；拒绝只需 `action=reject`。也提供只读 `GET /api/proposals/preview?name=...&id=...`，它不会落盘冲突状态。MCP 不暴露审批工具。

## 顺序应用、冲突和存储

- 建议队列为 `data/proposals/<SHA256书名>.json`，按需创建；提交和拒绝不创建草稿JSON。原 TXT、阅读进度、划线、批注、身份凭据及正式目录均不参与建议写入。
- 正式正文版本不符、原文不匹配、与已有草稿编辑重叠、插入边界已被编辑、目标或上级章节已变化时，明确标记冲突并阻止接受。列表读取时也展示正式版本过期；点击预览或接受会保存检测到的冲突。单纯草稿修订号过期返回409，刷新预览后仍可审批，不会将无重叠建议永久判冲突。
- 不重叠的建议按草稿已有 operations 顺序换算坐标，再调用 PR5 的编辑/目录逻辑。共享同一个线程锁和跨进程草稿锁；保存前复核正式版本。管理员直接编辑草稿后，旧审批预览也必须重新检查。
- 接受时只写一个文件：`data/drafts/<SHA256书名>.json` 中的编辑和 `proposal_receipts[id]` 一起通过 PR5 的唯一临时文件、fsync、原子替换提交。审批记录包含原建议摘要、提交者、管理员、决定时间、实际接受内容、决定摘要及草稿修订号。队列中的原始记录仍为 pending，查询时以草稿审批记录为准返回 accepted；不要单独读取原始队列判断接受状态。
- 相同决定重试会返回 `already_applied=true`，不会重复编辑；已接受建议不能改为拒绝或换内容重审。拒绝或冲突记录保留；要改方案，应读取当前原文后提交修改过的建议，重叠问题由管理员在现有草稿中核对处理，不自动重定位。
- 队列或草稿损坏返回503，保留原文件；写入失败保留前一完整版本。进程在原子替换前后终止已测试；任意硬断电、网络文件系统及正式发布的多文件事务未验证。

每书最多500条建议、队列4MB；原书最多8MB/200万UTF-16单位，每次选区及替换最多4000单位，理由2000单位，章名160单位，请求64KB。PR5 原有200次文字编辑/4MB草稿上限继续生效。超限拒绝并保留记录，本期不做自动清理或归档。

## 隔离部署及代码回退

可用于隔离测试环境。新建独立目录、数据目录及凭据，仅放合成/公开测试书；不与生产共用路径、端口或代理配置。以本分支完整 `mcp-edition/` 为代码来源，在该目录内初始化：

```sh
python3 -m venv .venv
.venv/bin/pip install -r core/requirements-mcp.txt
mkdir -m 700 -p core/data core/书库 state
.venv/bin/python deploy/init-identities.py
READER_MCP_STATE_DIR="$PWD/state" .venv/bin/python deploy/init-mcp-keys.py
```

凭据仅在测试环境私下读取。测试 Agent 通过既有身份管理页面创建；该服务的初始化、独立端口及连接地址配置见 [DEPLOY.md](DEPLOY.md) 和 [MEMBERS.md](MEMBERS.md)，不要指向生产身份文件。

在两个终端分别启动，以下命令在 `mcp-edition/` 内运行：

```sh
READER_CLOUD=1 READER_HOST=127.0.0.1 READER_PORT=18831 .venv/bin/python core/reader_server.py
```

```sh
READER_CLOUD=1 READER_MCP_PORT=18832 READER_API_URL=http://127.0.0.1:18831 .venv/bin/python core/mcp_server.py
```

使用隔离环境的身份登录 Web，验证匿名请求拒绝、MCP 三个新工具可见、提交不会建草稿、管理员接受后只有一份草稿、拒绝及冲突不改正文。使用现有成员页面建立测试 Agent。需要 token 管理服务时，按 DEPLOY 的单独端口配置启动；不需要更换旧成员连接格式。

已有隔离实例升级时，暂停 Web/MCP 写入并分别备份代码和当前数据。成套替换运行文件：`core/book_drafts.py`、`revision_proposals.py`、`reader_server.py`、`mcp_server.py`、`web/reader.html`、`web/editor.js`、`web/review.js` 和 `core/MCP工具标准调用参考_只读.txt`；确认书库调用参考为部署者维护的只读副本后同步该副本。再一起重启 Web/MCP。无需批量迁移数据。

回退本功能时停 Web/MCP，恢复阶段0 PR #6 的 `f3a33d3` 代码并重启；保留当前 `data/proposals/`、`data/drafts/` 及其他数据，不用旧数据覆盖。PR5 仍可查看已接受的草稿修改，旧代码保留未知 `proposal_receipts` 字段；建议入口暂不显示，重新部署本版后可继续查询。不要回退到缺少阶段0进度保护的旧代码。这里仅指代码回退，本期没有正式 TXT 发布或正文回滚功能。

## 验证记录（2026-10-25）

测试均使用临时合成书籍、身份与数据。命令在仓库根目录运行；浏览器为 Linux Chromium，生产数据未接触。

| 命令 | 结果与范围 |
| --- | --- |
| `python mcp-edition/core/selftest-proposals.py` | 通过：定位歧义/上下文/emoji、四种正文建议、章节增改删、顺序应用、HTTP权限与身份、并发审批CAS、重复重试、冲突/拒绝、重启、MCP真实调用、桌面/手机预览与手动调整、HTML转义、共享草稿及原文/进度/批注/身份字节保全 |
| 同上测试中的终止与失败注入 | 通过：草稿与接受记录在替换前后同时为旧或新；队列/草稿写入失败保留原文件 |
| `python mcp-edition/core/selftest-drafts.py --no-browser` | 通过：原草稿、版本冲突、目录与操作上限回归 |
| `python mcp-edition/core/selftest-members.py --no-browser` | 通过：成员与权限回归；工具数量更新为15 |
| `python mcp-edition/core/selftest-cloud.py` | 通过：真实云端HTTP/MCP与旧工具回归 |
| `python mcp-edition/core/selftest-progress-preservation.py` | 不改代码重跑通过：进度保全、跨进程冲突、损坏拒写、镜像恢复。首次运行在临时HTTP服务启动处失败，单元及进程测试已通过；启动失败原因未复现，保留此记录 |
| `python mcp-edition/core/selftest-preservation.py --no-browser` | 通过：历史批注与稳定身份保全 |
| `python mcp-edition/core/selftest-navigation.py --no-browser` | 通过：原目录和EPUB回归 |
| `python mcp-edition/core/selftest-resume.py` | 通过：原跨书续读及隔离测试 |
| `node mcp-edition/core/selftest.js` | 通过：原阅读器基础自测 |

新审批浏览器测试覆盖桌面和手机；上表显式 `--no-browser` 的旧测试未执行各自的浏览器部分。真实 ChatGPT/Operit 客户端新增工具刷新、实际部署代理和真实设备仍需在隔离环境验收。已有 MCP 连接不需重建，但客户端可能需要刷新工具列表。

未实施：多 Agent 独立草稿、独立校对进度、正式 TXT 发布与回滚、大规模协作文档系统。旧交接稿中的这些扩展以本次用户简化范围为准暂缓；既有共读、成员、管理员直接编辑能力保留。

## 合并前 UI 回归（2026-10-26）

审批入口移至书库抽屉，避免增加手机顶栏宽度。窄屏收紧按钮间距及划线选择器，窄屏隐藏数字百分比，顶部进度条保留。审批弹窗隔离阅读快捷键并约束 Tab 焦点；原生滚动、输入和按钮操作保留，关闭后返回书库按钮并恢复阅读快捷键。

新增 `python mcp-edition/core/selftest-review-ui.py`：320/390/480/1280px 真实按钮操作、弹窗内 PageDown/空格/方向键/字母快捷键、编辑、Tab/Escape、正式正文位置和进度字节保全，以及关闭后阅读/进度保存恢复。使用临时合成数据、本机 Chrome；不代表真实手机或 Kindle 验收。完整审批、目录与草稿回归另行记录在合并留档中。

本轮最终验证：`selftest-review-ui.py`、完整 `selftest-proposals.py`、完整 `selftest-navigation.py`、完整 `selftest-drafts.py` 和 `node core/selftest-progress-ui.js` 均通过。全部使用 Windows / Python 3.14 / MCP 1.30.0 / 本机 Chrome 与临时合成数据，未读写生产数据。目录测试原先无法点击手机字号按钮的回归已恢复；弹窗测试确认正式进度字节不变。关闭后的进度保存按既有平滑滚动加 1200ms 节流等待验收，不修改保存协议。
