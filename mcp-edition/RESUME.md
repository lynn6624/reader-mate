# PR2：MCP 跨书续读与目录进度身份一致性

扩展现有 `list_reading_progress`，工具总数仍为 12。指定 `name` 仍返回原按书 `progress` 数组及原鉴权；省略 `name` 返回跨书 `entries`，`reader_id` 缺省为服务端令牌确认的身份。没有新建人类端页面，不改变原前端书架和 `/api/reading-progress/latest`。

```json
{}
{"reader_id":"user"}
{"name":"list_books 返回的完整文件名.txt"}
```

三种参数分别表示本人跨书续读、其他身份跨书最近记录、旧版按书共读查询。MCP schema 的 name 为可选，get_reading_progress 的 name 保持必填。跨书底层入口为 `GET /api/reading-progress/resume?reader_id=...`，沿用身份门禁；所有认证身份仍属于当前一个共享书库，不增加跨空间能力。跨书入口拒绝 limit/page/offset/阈值开关及重复参数；MCP 客户端附加的未知参数即使被框架忽略，也无法改变后端数量与过滤规则。

- 本人最多最新 20 条有效保存记录，本期不分页，不表示完整历史。
- 其他身份最多最近 1 条；同一查询重复执行不会翻到下一条。
- 目标为 user 时，按已核验偏移计算实际百分比，先排除不足 1%，再选最近记录。恰好 1% 可返回；user 自查也遵循过滤。其他 Agent 没有此阈值。
- 基于 progress.json 的持久化记录聚合，不遍历 read_book 拉全书，不从 list_books 反推身份进度。按保存时间倒序验证候选，达到上限即停止；逐本读取并释放，不缓存整库文本。
- entries 包含 name/title、offset、percent、chapter、revision、last_read_at、total_units/chars、category、book_revision、location_status 及有效坐标。无可靠位置的记录不进入列表，不把缺失百分比当作 0%。
- 时间存储语义已在 ECS 核验为 Asia/Shanghai。旧 naive updated 原地解释为北京时间，返回 last_read_at 带 +08:00；已有偏移时间只转为同一时区，不二次加八小时。无法提供可靠时间的有效旧记录返回 null，排在已知时间之后；从不写回时间。

## 旧进度兼容结论

`progress_view` 原先用 `record.get('offset') or 0`，且无 anchor/无 offset 也标记 `legacy_anchor_verified`。因此空 user 初始化项会呈现坐标 (0,0)，该英文状态并不证明实际保存过。初始化来自新身份首次保存时 `setdefault(name, {'user':{}})`。这不是所有 user 记录的问题。

另一方面，旧版 004 的电梯记录具有真实 offset=4281、percent≈26.7%、chars、保存时间，revision 缺省为 0；旧 anchor 含原始换行，而现有检查仅规范化正文侧，所以被标 `legacy_unverified`。新跨书验证在持久化 offset 处规范化双方锚点，核对 UTF-16 范围、长度、内容哈希及已有段落坐标，确认后可纳入。没有以 revision>=1 作为通用条件，也不会寻找别处的相似锚点来移动用户位置。

无 offset 的旧锚点保留且不纳入；哈希变化、锚点不符、坐标冲突的记录待复核；显式保存的 offset=0 可有效。完整旧记录缺少 hash/anchor 时，需要可验证长度、百分比和保存证据，标为 legacy_position_verified。按书接口原有状态与形状兼容，跨书返回自身严格验证结果。

## list_books 不一致的源码与实测证据

原 `/api/books` 无论认证身份都优先用 user 子记录，即使只是 `{}`；无 user 时才取最近身份。其 offset/percent 缺省值为 0，read_at 为空；category 仅过滤书籍，并未切换进度来源。人类前端随后另取当前身份进度覆盖显示。

本次修复前，分别以 op001、004 认证，记录 viewer_id（不记录令牌）并查询全量及 humanities：乌合之众的目录字段均为 0/空；同一 op001 按书查询有真实保存位置。证实全量和分类都是旧默认语义导致，不能归因于 004 查他人。

MCP `list_books` 现在请求 `/api/books?progress_scope=self`，服务器按认证身份取已核验记录，响应明确 viewer_id、progress_scope=self、time_zone。本人没有有效保存则 offset/percent/read_at 为 null，绝不回退到 user 或其他人；分类和全量一致。旧 HTTP 默认、人类书架及其续读逻辑保持原样。调用参考工具和只读书库 TXT 同步说明这一区别。

## 验证与部署

测试：`python selftest-resume.py` 启动隔离 HTTP/MCP 服务器，覆盖 optional name schema、原 get 必填、本人 22→20、他人最近 1、user 先过滤后取最新、恰好1%、全不足1%返回空且无低进度书名、无分页绕过、旧 revision0 换行锚点、真实0、空初始化/缺失位置/内容变化/坐标冲突排除、读书/查询不写盘、北京时间及旧前端兼容。另用云上原书和原记录私有副本验证锁帝翎6260、电梯15915符合门槛，破云2吞海2636不足1%排除，004旧4281正常。

PR2 独立基于原主分支测试，不依赖 PR1 索引模块；组合版本另测章节、原 MCP、上传、手机/桌面书架、进度页面和 TTS。原进度和原书不上传仓库。

无需迁移进度数据。备份后部署 reader_server.py、mcp_server.py、新 progress_catalog.py；同步工具目录和只读书库中的 MCP工具标准调用参考_只读.txt，保留只读权限。停 MCP 再停 Web，替换文件，先启 Web 再启 MCP。不要安装 Kindle 扩展或增加服务。回滚恢复这两个服务代码与两份指南；新 helper 可保留为未引用文件。不要恢复任何旧 progress.json/notes.json 覆盖新的阅读数据。

MCP 客户端若缓存 schema，需要刷新工具定义；工具名称和已有 name 调用不变。新总览会同步返回已保存位置，不证明在线或推测阅读耗时。
