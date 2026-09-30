# -*- coding: utf-8 -*-
"""epub2txt.py 自测：自己造一个 epub，跑完校验输出。
用法：python selftest-epub2txt.py make   # 造测试 epub 并自动调 epub2txt.py 转成 verify.txt
      python selftest-epub2txt.py check  # 校验输出
"""
import io, os, subprocess, sys, tempfile, zipfile

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(tempfile.gettempdir(), "reader-mate-epub-selftest")
EPUB = os.path.join(TMP, "verify.epub")
OUT = os.path.join(TMP, "verify.txt")

CONTAINER = '''<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>'''

OPF = '''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
 <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
   <dc:title>自测书·铁盒</dc:title><dc:creator>Tester</dc:creator>
 </metadata>
 <manifest>
   <item id="c2" href="ch2.xhtml" media-type="application/xhtml+xml"/>
   <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
   <item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
 </manifest>
 <spine>
   <itemref idref="c1"/><itemref idref="c2"/>
 </spine>
</package>'''

NAV = '<html><body><nav><ol><li><a href="ch1.xhtml">导航不该出现</a></li></ol></nav></body></html>'

CH1 = '''<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>第一章</title>
<script>var bad = 1;</script><style>p{color:red}</style></head>
<body>
<h1>第一章 敲门</h1>
<p>这是<em>第一段</em>，含 &amp; 和 &lt;符号&gt;，还有&nbsp;一个 nbsp。</p>
<p>跨页 A<br/>B<br/>C 三行。</p>
<ul><li>列表一</li><li>列表二</li></ul>
</body></html>'''

CH2 = '<html><body><h1>第二章 龙骨</h1><p>第二章正文，龙骨被搬走了。</p><blockquote>引文独立成段。</blockquote></body></html>'


def make():
    os.makedirs(TMP, exist_ok=True)
    if os.path.exists(EPUB):
        os.remove(EPUB)
    with zipfile.ZipFile(EPUB, "w") as z:
        # mimetype 必须是第一个条目且不压缩
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                   compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/content.opf", OPF)
        z.writestr("OEBPS/nav.xhtml", NAV)
        z.writestr("OEBPS/ch1.xhtml", CH1)
        z.writestr("OEBPS/ch2.xhtml", CH2)
    print("EPUB:", EPUB)

    # 造完直接调 epub2txt.py 转出 verify.txt，省得 check 之前还得手动跑一遍
    epub2txt = os.path.join(BASE, "tools", "epub2txt.py")
    if os.path.exists(OUT):
        os.remove(OUT)
    cmd = [sys.executable, epub2txt, EPUB, "-o", OUT]
    print("RUN:", " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.stdout:
        print(r.stdout.rstrip())
    if r.returncode != 0:
        if r.stderr:
            print(r.stderr.rstrip(), file=sys.stderr)
        raise SystemExit("epub2txt.py 转换失败（exit %d）" % r.returncode)
    if not os.path.exists(OUT):
        raise SystemExit("epub2txt.py 未生成 %s" % OUT)
    print("OUT:", OUT)

def check():
    if not os.path.exists(OUT):
        print("× 没有输出文件", OUT); return 1
    txt = open(OUT, encoding="utf-8").read()
    print("--- 输出正文 ---")
    print(txt.strip())
    print("--- 校验 ---")
    i1, i2 = txt.find("第一章"), txt.find("第二章")
    tests = [
        ("spine 顺序（第一章在第二章前）", i1 >= 0 and i2 >= 0 and i1 < i2),
        ("实体反转义 & 和 <>", "&" in txt and "<符号>" in txt),
        ("nbsp 反转义成不换行空格", "\xa0" in txt),
        ("script 被剔除", "var bad" not in txt),
        ("style 被剔除", "color:red" not in txt),
        ("nav（不在 spine）未混入", "导航不该出现" not in txt),
        ("列表项保留", "列表一" in txt and "列表二" in txt),
        ("br 断行后 ABC 都在", all(s in txt for s in ("跨页", "A", "B", "C 三行"))),
        ("blockquote 保留", "引文独立成段" in txt),
        ("段落之间有空行", "\n\n" in txt),
    ]
    bad = 0
    for name, ok in tests:
        print(("  ✓ " if ok else "  × ") + name)
        bad += 0 if ok else 1
    print("失败项：%d" % bad)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "make"
    sys.exit(make() if mode == "make" else check())
