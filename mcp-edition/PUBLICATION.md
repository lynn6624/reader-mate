# 共享草稿发布、位置迁移与回滚（B5）

2026-11-02已完成正式替换部署，原书未自动发布。已完成保持256MiB额度的内存优化：Linux/glibc Web服务须同时安装 deploy/reader-web-memory.conf 中的两个分配器环境设置。近上限HTTP Web峰值降至约101MiB，256MiB/192MiB软限加模拟其他服务占用验证通过。无该设置的原基准会超正式总预算，不能只部署代码。详见 PUBLICATION-VALIDATION.md。

本功能沿用 TXT 正文、共享管理员草稿、现有身份及 JSON 数据文件。接受建议只修改草稿；主人在书库抽屉打开「发布与回滚」，检查影响并勾选确认后，才发布整份当前草稿。没有面向 Agent 的发布工具。不会自动发布、批量改书或清理历史。

## 操作

1. 管理员先完成共享草稿的正文及目录修订，解决待核对章节。
2. 在当前书的书库抽屉打开「发布与回滚」，点击「校验共享草稿」。预检显示字符数、编辑数、划线、批注、进度和目录影响；阻断项显示记录位置及原因。预检不写正式正文和阅读位置。
3. 无阻断项后，勾选确认并发布。服务器重新读取最新批注和进度；若其无法迁移，整次拒绝。草稿、正文或目录变化则要求重新预检。响应丢失时保留同一个 request_id 重试，不另建一次发布。
4. 成功后页面重载正文及迁移后的记录，保持临时查看状态，不自动把弹窗位置设为续读点。另一设备轮询发现正文版本变更时停止旧坐标写入并重新打开本书。
5. 历史区可校验并回滚最近一次正式发布。回滚会迁移当前记录，而非用旧 notes/progress 整份覆盖；新版期间新增的安全位置批注和进度也会保留。
6. 历史清理必须先选记录、查看精确范围，再明确确认。不能清理当前、未完成或恢复所需历史。达到配额时拒绝继续发布，等待管理员处理；不自动销毁唯一恢复材料。

仅服务端认证的 user 管理员可调用全部发布端点；普通成员、匿名客户端和仅持 Kindle 设备 Cookie 的客户端均拒绝。身份与令牌格式不变。只读调用参考不能发布或回滚。

## 位置与数据

所有位置仍为规范化阅读正文中的 UTF-16 单位。逐个重放草稿中的编辑，按每次编辑发生时的坐标迁移；不排序编辑、不按第一段相同引文猜位置。遇到代理对截断、原文不匹配、被删除/替换的引用、引用范围内插入或失效章节时阻断。

- 划线起止位置、正文 hash 和章节随正文迁移；稳定 highlight_id、annotation_id、原作者、评论及未知扩展字段保留。缺少稳定 ID 的旧记录先按现有规范化规则冻结旧 ID，再移动位置。
- 每个身份的 offset、TTS 位置、段落坐标、锚点、长度、百分比和章节更新；保留原阅读时间，进度修订号递增。可核验的 revision=0 记录仍允许迁移，无法核验则阻断。未受影响的其他书条目保留。
- 正文版本 book_revision 是原始文件字节的 sha256: 值；历史划线 book_hash 是规范化阅读文本的 UTF-8 SHA，两者用途不同。
- 没有文字操作、仅发布目录时保留原 TXT 字节；有文字操作时一次性写成 UTF-8/LF。原文件名、书籍标识、分类、身份和建议队列不改。人工目录保留层级；自动索引随正文版本按需重算。
- 回滚保存原文件编码和换行字节。若正式目录或共享草稿又被编辑，先拒绝回滚，避免覆盖未发布工作。若新批注引用了仅存在于新版的插入文字，回滚也拒绝，不删除该批注。

无需数据库迁移。新增文件只在按需使用时建立：

| 位置 | 作用 |
| --- | --- |
| data/publications/.barrier.lock | 跨进程数据屏障；同一库的 Web 请求与维护工具共用 |
| data/publications/active.json | 未完成发布的恢复指针 |
| data/publications/cleanup-active.json | 未完成清理的恢复指针 |
| data/publications/&lt;书名SHA&gt;/current.json | 当前 publication_id、generation、book_revision、上一记录及动作 |
| 同目录 requests.json / bindings.json | 请求收据、ID 与输入绑定；历史清理后仍保留小型幂等凭据 |
| 同目录 &lt;request_id&gt;/ | manifest、旧/新候选内容、COMMIT 和 APPLIED 标记 |
| notes.json 的可选 publication_revision | 稳定的轮询缓存失效标识，不是身份或令牌 |

## 一致性与恢复

正常请求持共享屏障，发布、回滚和清理持独占屏障；其他书的普通写入也等待，避免全库 JSON 互相覆盖。POST 在屏障外读完有界请求体；小请求总接收限时 10 秒、大请求 60 秒，socket 读写超时 30 秒。客户端慢传不会无限占用发布锁。POSIX 使用 flock，Windows 使用跨进程字节锁。

先落盘全部候选和清单，COMMIT 是持久提交决定：之前终止只丢弃预备内容，原数据保留；之后终止必须向前恢复到完整新状态，不能恢复一半旧数据。启动在初始化和监听前恢复；运行中的读请求发现待恢复事务也先恢复。目标若既不匹配旧 SHA 也不匹配新 SHA，停止并报错，保留材料供检查。不能手动删 active/COMMIT 或只复制部分候选文件。

每次确认重新迁移最新 notes/progress。幂等 ID 绑定动作、确认摘要及载荷；相同 ID 换操作会拒绝。提交后响应丢失和历史清理后重试均返回原收据。目录和正文的预检摘要不把正常阅读写入算成草稿过期。

## HTTP 与 MCP

所有 HTTP 端点沿用已有鉴权。发布 POST JSON 上限 64KiB。

| 方法与路径 | 输入/行为 |
| --- | --- |
| POST /api/publication/preview | name；返回 can_commit、summary、issues、preview_digest |
| POST /api/publication/commit | name、preview_digest、request_id |
| GET /api/publication/history?name=… | 返回 items，含可回滚/可清理状态 |
| POST /api/publication/rollback-preview | name、publication_id |
| POST /api/publication/rollback | name、publication_id、preview_digest、request_id |
| POST /api/publication/cleanup-preview | name、publication_ids 数组；返回 digest 和精确字节范围 |
| POST /api/publication/cleanup | name、publication_ids、confirm_digest |

409 表示版本/预检变化或不能迁移；缺少已发布书划线的正文版本返回 428；损坏数据或不能恢复返回 503，原件保留。客户端不得自动换新版本后重放旧坐标。

MCP 仍为 15 个工具，新增两个可选 Schema 参数，均追加在旧参数之后：

- add_highlight(..., book_revision="")
- update_progress(..., expected_revision=-1, book_revision="")

未发布书维持旧调用兼容。发布或回滚过的书必须携带实际 read_book 返回的 book_revision。expected_revision 仍是独立的进度 CAS；不是正文版本。read_book 继续只读，返回 publication_generation 并提醒显式保存。get_calling_guide 与书库只读 TXT 使用同一份更新指南。ChatGPT 等缓存工具 Schema 的客户端应刷新工具列表；不需要更换身份或 URL。

## 资源及维护

保持既有上限：正文 8MiB/200万 UTF-16 单位，200 次编辑，单次原文与替换各 4000 单位，草稿 4MiB；元数据文档每份 8MiB。发布串行，无新依赖、模型或常驻服务。恢复历史全库上限 256MiB、每书最多 3 份；达到上限拒绝，不自动清理。应另外保留独立完整冷备份，内置历史不是异机备份。

维护程序必须使用相同屏障或停全部 Web/MCP 写入。tools/rebuild_chapters.py 仍要求 apply/rollback 时离线，并增加共享/独占屏障。旧 tools/backup.py 只备份其既有部分文件，不含全部协作状态，不能当作完整恢复包；其导出持共享屏障，已有发布记录的库拒绝旧式部分导入。完整备份应停服务后一起打包书库、完整 data、私有 state 与部署配置，按原权限保存。

## 部署与代码回退

先在隔离书库运行下列自测并检查资源报告；不要用唯一正式原本作实验。升级时停 Web/MCP，创建完整冷备份，成套替换 core 中四个 publication_*.py、reader_server.py、mcp_server.py、web/reader.html、web/editor.js、web/publication.js、维护工具和调用参考；只读参考副本经确认后同步。私有路径钥匙、身份文件、代理 Host 配置及 token 管理目录原样保留。启动先看恢复日志再做只读检查。不需要改 DNS、代理或 Kindle 扩展。

当前Linux/glibc部署需把 deploy/reader-web-memory.conf 安装为 reader-web.service 的 systemd drop-in（例如 /etc/systemd/system/reader-web.service.d/20-publication-memory.conf），daemon-reload后重启Web，并核对该进程环境包含 MALLOC_ARENA_MAX=1、MALLOC_TRIM_THRESHOLD_=131072。保留原 reader-mate.slice 的 MemoryHigh=192MiB、MemoryMax=256MiB，不改MCP/管理页配置。不适用的系统应先重复资源测试，不能套用Windows数值。移除该优化会恢复原高内存风险；已有发布代次时仍必须保留B5兼容代码。

尚无正式发布时可以停服回退到此前完整代码，保留新增元数据。已有发布或回滚代次时，代码回退也必须保留 B5 的屏障、恢复和版本校验；正文回滚不解除此要求。即使正文回到旧 hash，generation 仍递增，旧无版本调用仍拒写。事故完整冷备恢复会回退备份之后的全部写入，必须明确确认；不能只恢复 TXT 或旧 notes/progress。

## 验证入口

```sh
python core/selftest-publication-mapping.py
python core/selftest-publication-plan.py
python core/selftest-publication-store.py
python core/selftest-publication-integration.py
python core/selftest-publication-ui.py
python core/selftest-publication-realcopy.py --book /private/copy.txt
python core/selftest-publication-resources.py --output /private/resource-report.json
python core/selftest-publication-http-resources.py --output /private/http-resource-report.json
```

UI 自测需现有 Playwright/Chrome 测试环境；运行服务不增加这些依赖。真实书副本测试只输出校验和与数量，所加多身份记录均为合成验收数据，不等同于真实用户记录迁移验收。测试执行结果与首轮失败记录见随版本交付的验收报告；不得把未执行的真机或生产迁移列为通过。
