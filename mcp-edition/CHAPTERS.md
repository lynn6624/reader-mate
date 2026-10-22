# PR1：章节索引修复与历史章节重算

章节名原来由不区分目录的向后正则扫描产生，写入 progress.json；没有独立持久化索引，后续读取直接使用旧章名。新索引按标题密度、重复顺序和正文过渡排除目录，保留短章和重名，坐标统一 UTF-16。缓存版本 2、最多两本索引，不保留正文。现有前端分段直接使用服务器边界。

验收以云上同一版 TXT 为准；交接中的 700 / 4520 是近似线索。实际目录第五章在 388；600–750 已进入译者序；4500/4520 属于 1963 年版序言及注释，引言标题 4649。6100 的独立文本核对为 paragraph_idx=149、char_offset=84，原锚点一致。旧 6100 用隔离记录复测，不回写已继续阅读的线上记录。

|Offset|章节|区域|
|---:|---|---|
|0|目录|toc|
|388|目录|toc|
|600|译者序|body|
|700|译者序|body|
|750|译者序|body|
|1500|译者序|body|
|4500|1963年版序言|body|
|4520|1963年版序言|body|
|4522|1963年版序言|body|
|4649|引言 群体的时代|body|
|6100|引言 群体的时代|body|
|99000|第五章 议会|body|

目录 21 项，正文结构起点 20 项（序言/引言 4、篇 3、章 13）。差异仅「作家榜经典文库」为目录书目入口，没有正文标题。不得为符合预估数量补造章节。

|目录标题|目录 UTF-16|正文 UTF-16|
|---|---:|---|
|译者序|61|410|
|1902年第六版作者序言|66|1876|
|1963年版序言|80|4273|
|引言 群体的时代|90|4649|
|第一篇 群体的灵魂|100|9100|
|第一章 群体的普遍特征和群体思维的心理法则|114|9111|
|第二章 群体的情感与道德|142|14220|
|第三章 群体的观念、推理和想象力|161|26841|
|第四章 群体信念的宗教形式|184|32208|
|第二篇 群体的意见和信仰|199|35367|
|第一章 群体的信仰和意见的遥远成因|216|35381|
|第二章 群体意见的即时成因|240|46432|
|第三章 群体的领袖和他们的说服手段|260|54339|
|第四章 群体的信仰和意见的可变范围|284|66986|
|第三篇 群体的归类和描述|303|73526|
|第一章 群体的归类|320|73540|
|第二章 所谓的犯罪群体|336|75437|
|第三章 刑事法庭的陪审员|354|77852|
|第四章 选民群体|373|81880|
|第五章 议会|388|87098|
|作家榜经典文库|401|无对应正文标题|

|正文标题|start|end|检测依据|
|---|---:|---:|---|
|译者序|410|1876|body_heading+toc_match|
|1902年第六版作者序言|1876|4273|body_heading+toc_match|
|1963年版序言|4273|4649|body_heading+toc_match|
|引言 群体的时代|4649|9100|body_heading+toc_match|
|第一篇 群体的灵魂|9100|9111|body_heading+toc_match|
|第一章 群体的普遍特征和群体思维的心理法则|9111|14220|body_heading+toc_match|
|第二章 群体的情感与道德|14220|26841|body_heading+toc_match|
|第三章 群体的观念、推理和想象力|26841|32208|body_heading+toc_match|
|第四章 群体信念的宗教形式|32208|35367|body_heading+toc_match|
|第二篇 群体的意见和信仰|35367|35381|body_heading+toc_match|
|第一章 群体的信仰和意见的遥远成因|35381|46432|body_heading+toc_match|
|第二章 群体意见的即时成因|46432|54339|body_heading+toc_match|
|第三章 群体的领袖和他们的说服手段|54339|66986|body_heading+toc_match|
|第四章 群体的信仰和意见的可变范围|66986|73526|body_heading+toc_match|
|第三篇 群体的归类和描述|73526|73540|body_heading+toc_match|
|第一章 群体的归类|73540|75437|body_heading+toc_match|
|第二章 所谓的犯罪群体|75437|77852|body_heading+toc_match|
|第三章 刑事法庭的陪审员|77852|81880|body_heading+toc_match|
|第四章 选民群体|81880|87098|body_heading+toc_match|
|第五章 议会|87098|99090|body_heading+toc_match|

抽查：梦的解析无标记密集目录 30 项、正文结构 27 项；三处「注 释」没有独立对应正文标题，其余目录对应。电梯无目录/章节；网络文学按独立逐行边界验收，保留重复标题，排除紧跟「作者有话要说」的作者留言。全部真实书边界及抽样偏移通过；合成用例覆盖 emoji、英文标题、短章、重名。

测试命令（core 目录）：

```sh
python selftest-chapters.py
python selftest-cloud.py
python selftest-browser.py
```

私有原书验收另加 `--fixtures /private/fixtures --report /private/result.json`。不在公开仓库分发原书、身份令牌或用户进度。

安全重算（无需数据库表迁移）：

```sh
# 在线预览，只读
python tools/rebuild_chapters.py --root /opt/reader-mate/core --all --report /private/preview.json --export-index /private/indexes.json
# 停 MCP、再停 Web；使用新备份目录
python tools/rebuild_chapters.py --root /opt/reader-mate/core --all --apply --offline --backup-dir /private/chapter-backup --report /private/applied.json
# 启动 Web、再启动 MCP
```

只改 chapter、chapter_index_version、chapter_location_status；不调用保存进度接口，不改变 offset/revision/updated/anchor/有效坐标或实际 TXT。旧扁平进度形状保留。内容哈希不一致、位置冲突或不可验证锚点跳过并记入私有报告。MD 镜像同步派生，备份原始字节，写入前再次校验进度和书籍哈希。

回滚：停 MCP/Web 后执行 `python tools/rebuild_chapters.py --root /opt/reader-mate/core --rollback /private/chapter-backup --offline`，再恢复原代码并启动 Web/MCP。只有进度仍等于重算后摘要才允许整份恢复；已有新阅读写入会拒绝覆盖，应只回滚代码并逐项复核章节元数据。不要用历史备份覆盖新的阅读位置。

边界：纯文本结构仍是启发式；不自动更改书籍正文、旧批注内容或其 chapter_id。无可靠标题时返回未分章。大型书籍重算逐本进行，不增加常驻服务。
