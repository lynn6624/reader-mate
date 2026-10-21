# PR3：章节目录导航与 EPUB 导入

目录是绑定正文哈希的导航元数据，正文仍统一为 TXT。本次复用 PR1 索引、原阅读器分段/选区/临时跳转、原身份门禁、上传分类及存储；没有另建阅读器或常驻服务，没有新前端依赖。

## 实现与文件

- core/navigation.py：自动目录、合集层级、元数据校验、正文窗口切片。
- core/reader_server.py：目录读取及管理员修正、按需持久化索引、窗口阅读及全书搜索、串行 EPUB 上传事务；不改变原 MCP 协议。
- core/web/reader.html：顶部“目录”入口、折叠/分页、JSON 修正预览确认；复用原选区、划线、TTS、滚动与续读保存。
- core/tools/epub_navigation.py：纯标准库严格 EPUB 文字/原生目录提取、UTF-16 映射及受限转换子进程。
- core/tools/epub2txt.py：原 CLI 保留，extract_text 复用同一严格提取器，消除跳过损坏章节的旧行为。
- core/selftest-navigation.py：专项 HTTP、EPUB、手机/桌面与数据保护测试；原 selftest-cloud/browser/import/tts/chapters/resume/progress-ui 等同步隔离复制依赖。

## 目录结构

按书按需保存为 `data/navigation/<SHA256(完整文件名)>.json`，只创建本次打开目录的书的元数据，不扫描或迁移整库。schema_version=1；book_revision 是最终 TXT 原始字节的 SHA-256；revision 是目录自身的 CAS 版本，与阅读进度 revision 无关；source 为 automatic、epub 或 manual。

每项包含 `id, title, offset, parent_id, order, quote, basis`。offset 为既有阅读引擎内存文本视图中的 UTF-16 单位；原引擎读入时把 CRLF/CR 规范为 LF，这一既有坐标约定保持不变，绝不写回旧 TXT。quote 是该位置起最多40个 UTF-16 单位原文，避免在 emoji 中间结束。标题可与正文标题不同；跳转依据始终为坐标和正文哈希，不按颜色、书名猜测或再次搜索相似句子。

自动目录复用 PR1 的真实标题和目录排除，并补充局部目录、独立“序”及有证据的作品/章节层级。局部目录须出现密集条目、首条重现及正文过渡；单纯标题列表没有正文则不生成目录。合法短章和不同位置的同名章节保留。合集先按局部目录与正文标题映射；首篇若没有独立书名正文行，只有总目录和局部目录至少三项按顺序对应时，才将作品名映射到首章正文。无法确定的层级保持平级，允许无目录。

不覆盖人工目录或 EPUB 原生目录。正文哈希变化则停止使用旧目录，返回 content_changed；元数据无效则返回 invalid_metadata，不静默跳往别处。自动目录最多8000项，达到上限会明确提示；前端/API每页200项（API最大500），目录面板同时最多创建200个条目。

## EPUB 转换

按 OPF spine 顺序提取 XHTML，manifest 顺序不参与排序。EPUB3 优先 epub:type=toc / doc-toc；其次 EPUB2 NCX；没有原生目录时按正文章节元素生成。嵌套目录保留父子关系，无链接的分组映射到首个有效子项。

逐个正文资源生成文字，同时记录元素 id/xml:id 和标题在该资源最终文字中的 UTF-16 位置；拼接时加上最终书名及前面资源的实际文本长度。目录 href/fragment 映射到这些真实坐标，完全不依赖标题字面相同。文字和目录在导入时一次确定、一起提交。块级元素、段落、列表和 br 保留必要断行，忽略脚本/样式/原生目录页面；不保留图片或 CSS，纯图片页面会明确返回提示，纯扫描书不能伪装成功导入。

纯图片扉页没有文字坐标时，无fragment的目录入口映射至spine中后续首个文字资源，并逐项返回明确提示；带fragment的图片目标仍拒绝，绝不猜测片段。

资源缺失、spine 引用错误、损坏 XML、不存在的 fragment、目录指向未导入资源、外部目录 URL、实体声明、重复路径/ID、无法唯一定位及越限均拒绝，不跳过章节凑出成功结果。兼容旧 manifest 省略 media-type、但明确指向 HTML/XHTML 的情况。未支持的资源/编码会报错，不用替换字符掩盖解码失败。

限制：压缩包12MiB、解压总量32MiB、单资源4MiB、最多2048资源、UTF-8输出8MiB、8000目录项、层级8层。服务器一次只接收一个导入；并发请求返回409。转换为一次性子进程，25秒超时并终止；Linux另限160MiB地址空间及22秒CPU。失败清理临时目录、新 TXT 和新目录元数据，不产生孤立分类；同名旧书继续拒绝覆盖。无新增生产依赖或常驻进程。

## 前端操作

打开书后点顶部“目录”，用箭头折叠或展开层级，条目多时翻目录页。点击条目临时跳到正文，自动关闭目录；继续滚动即可阅读。打开、关闭、临时跳转都不保存进度，只有明确点击“将当前位置设为续读点”才把跳转位置保存。人工目录指向段落中间时会定位到具体文字，并保存同一 UTF-16 偏移。字号/宽度变化不改变索引。

大于128000 UTF-16单位的书，现有阅读器只加载目标附近最多60000单位，滚动继续切换窗口；选区/划线/批注保持全书坐标。搜索由服务器返回有界全书命中，不把全文补发到浏览器。朗读提前预取下一窗口，并在 end 回调内同步接续；若网络仍未返回后续窗口，会明确提示稍后点朗读继续，不静默跳过文字。小书原阅读路径保留。

管理员在目录底部展开“目录修正”：导出 JSON，修改标题、offset、parent_id 或数组顺序，可增删项；修改位置需同步 quote。选入文件先预览，确认后保存；普通共读身份只能查看。校验正文哈希、目录版本、唯一ID、父项先于子项、无循环、最多8层、合法UTF-16边界及精确quote；未预览的提交返回428，正文/目录变化返回409。JSON上限8MiB，保存前保留上一版元数据 `.revision-N.bak`。删除书籍时仅把对应目录移到 navigation/deleted 留档，不让新导入同名书继承旧目录。

## HTTP/API 与 MCP

- `GET /api/navigation?name=完整文件名.txt&offset=0&limit=200`：经既有鉴权读取目录。这里查询参数 offset 是条目分页下标，返回项 offset 才是正文 UTF-16 偏移。
- `POST /api/navigation`：管理员提交 `{name,book_revision,expected_revision,items,dry_run:true}` 预览；确认时去掉 dry_run，并带返回的 preview_digest 作为 confirm_digest。只写导航元数据，不写阅读进度。
- `GET /api/book?name=...&window=1&offset=...`：新前端窗口模式；返回 text_base/window_start/window_end/total_units。省略 window 的原接口响应及 MCP read_book 行为不变。
- `GET /api/book/search?name=...&q=...`：已鉴权的有界搜索，最多800条命中和片段。
- 原管理员 `POST /api/import` 及分类参数保持兼容，新增 navigation_items/warnings 响应字段。

MCP 工具仍为12个，没有新增工具或参数声明；read_book、进度协议、reader_id/身份归属、user跨书1%规则均保持。无需因PR3刷新ChatGPT工具Schema。没有数据库表/阅读记录迁移，只增加导航sidecar文件。

## 样本与验证

真实原书与结构化定位检查：

- 乌合之众：PR3目录20项逐项匹配独立核查的正文边界；目录第五章388不作正文入口，正文第五章87098，引言4649、原6100仍在引言。
- 普通网络小说：原书验证165个有正文内容的导航项；没有把作者留言或空的重复外层标题当作章节正文；重复且各有正文的同名章节保留。
- 李碧华全集：在 ECS 原地只读验证73个导航项、5个作品级入口；青蛇308324、其下41个子项；霸王别姬808映射到首章正文，其下11个子项。对应原文quote及所有UTF-16位置校验通过，原书字节哈希不变。私人原书未下载或发布。
- 电梯：无可靠章节时返回空目录，正常阅读不受影响。
- EPUB：用户提供的真实李碧华全集 EPUB（1,851,827字节）隔离HTTP上传成功，按spine提取60个文字资源、726525个UTF-16单位、35项原生目录。全部35项quote逐项核对通过；霸王别姬入口443、11个直接子项，青蛇入口308084、3个直接子项（遵从原生目录结构，未强行扩充）。手机/桌面实际点击跳转及字号调整通过，目录操作不写阅读进度，输入EPUB哈希不变。纯图片页11个明确忽略，5个作品扉页入口定位到其后的文字并提示。没有把该EPUB导入生产或替换旧TXT，两个格式生成的offset不可混用。另用构造的EPUB3/nav和EPUB2/NCX覆盖反序manifest、正确spine、层级、标签与正文标题不同、fragment、emoji/实体/段落，以及缺章节/损坏/越限/外部引用/图片fragment失败。

`python selftest-navigation.py` 通过真实HTTP上传、JSON预览/CAS、权限、错误清理、并发导入拒绝、长书窗口与搜索；手机/桌面目录打开关闭不写进度、跳转后显式保存、段落中间精确保存、450项分页、原生目录折叠、管理员修正界面及TTS跨窗口衔接。原TXT含CRLF的原始字节、旧revision0记录、已有划线/他人批注字节对比保持，且跳转到长书末章后仍能显示原有划线。

原 selftest-cloud、selftest-resume（含私有原书验收）、selftest-chapters、selftest-browser、selftest-import、selftest-tts、selftest-progress-ui 回归通过。构造样本在 ECS 隔离临时书库真实运行约3.27秒，子进程观测峰值39040KiB；这不是最大EPUB的峰值承诺，160MiB为保护上限。没有向生产书库上传测试EPUB。

Kindle：保留既有独立可选扩展，没有重新安装到ECS；新面板无动画/大型依赖，手机触屏模拟通过。可选扩展的隔离接口兼容测试通过；已停用的独立Kindle阅读页本轮不加目录入口。本轮无KPW6真机，不把真机兼容列为已完成验收。

## 部署、失败与回滚

先在隔离目录运行测试，再备份并校验旧源码哈希，停MCP→Web，替换 reader_server.py、web/reader.html、navigation.py、tools/epub_navigation.py、tools/epub2txt.py；原MCP代码、鉴权文件、书籍和阅读存储不替换。启动 Web→MCP，执行只读目录/窗口/MCP检查。首开目录才生成对应sidecar，无整库迁移。

回滚停MCP→Web，恢复原 reader_server.py、reader.html、epub2txt.py，再启动Web→MCP。新增模块和导航目录可以保留为未引用文件，尤其保留人工修正。禁止把备份progress.json/notes.json/library.json/identities.json覆盖当前数据；备份只用于完整性核对。目录单独回退需先比较当前目录revision与book_revision，确认没有后续修改，再备份当前sidecar并恢复目标目录版本；不回退正文或阅读位置。

尚存边界：TXT结构识别仍保守，缺少充分证据的标题/层级需管理员修正；不提取扫描图片文字、不支持DRM；原生目录指向非spine资源会明确拒绝；超限EPUB需要先处理后再导入。网络极慢时跨窗口朗读可能需要再次点击继续。真实用户EPUB已补验；KPW6真机仍待补验。

## 上线核验（2026-10-22）

最终代码已完成备份、哈希守卫与部署。原166本TXT逐本SHA-256一致；停服期间progress/notes/library/identities四个存储字节一致，没有记录迁移或覆盖。线上乌合之众正文目录、李碧华作品入口、60000单位窗口、空目录降级及鉴权检查通过。原op001常规通道和004映射通道均通过12工具Schema、本人/他人进度权限、分类目录、调用指南与read_book提醒回归；只读检查前后进度字节一致。Web/MCP均active，单次观测约35.8/51.1MiB，非峰值承诺。真实EPUB仅在隔离书库验收，没有自动添加到生产或替换原TXT。
