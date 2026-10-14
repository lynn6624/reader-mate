#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""decode_bytes 编码探测自测 —— 认得出 GBK、认得出 Big5、不把 GBK 读成花屏。

跑法：python3 selftest-encoding.py      退出码 0 = 全过，1 = 有失败

说明：用 ast 只从 reader_server.py 里取出 decode_bytes / _common_hanzi_rate，
不 import 整个后端（避免启动副作用）。脚本里同时留了一份修复前的旧实现，
用来对照证明「GBK 被读成 Big5」这个 bug 确实存在、且已被修掉。
"""
import ast
import os
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "reader_server.py")
WANTED = ("decode_bytes", "_common_hanzi_rate")

GBK_TEXT = "这是一段用 GBK 编码的旧书正文，标点也是全角的：「你好，世界！」"
BIG5_TEXT = "這是一段用 Big5 編碼的繁體舊書，標點同樣是全形：「你好，世界！」"
# 真实复现样本（取自 core/README.md 前 40 个汉字）：这 80 字节 GBK 恰好能被 Big5
# 整段吞下、不抛异常，于是修复前被静默读成「黍圈掛華堐…」——正是 002 遇到的花屏。
GBK_SHORT = "读伴本地阅读器进度落文件读得到读伴英文是一个零依赖的本地阅读器单文件后端单页前端"

CASES = [
    ("utf-8 中文（无 BOM）", GBK_TEXT.encode("utf-8"), GBK_TEXT),
    ("utf-8 中文（带 BOM）", GBK_TEXT.encode("utf-8-sig"), GBK_TEXT),
    ("GBK 老书正文", GBK_TEXT.encode("gb18030"), GBK_TEXT),
    ("GBK 短段（真实花屏样本）", GBK_SHORT.encode("gb18030"), GBK_SHORT),
    ("GBK 正文 + 英文数字", ("第 3 章 Chapter Three " + GBK_TEXT).encode("gb18030"),
     "第 3 章 Chapter Three " + GBK_TEXT),
    ("Big5 繁体正文", BIG5_TEXT.encode("big5"), BIG5_TEXT),
    ("纯 ASCII", b"Chapter 1\nHello world.\n", "Chapter 1\nHello world.\n"),
    ("空文件", b"", ""),
]


def load_new(path=SERVER):
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), path)
    ns = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in WANTED:
            exec(compile(ast.Module(body=[node], type_ignores=[]), path, "exec"), ns)
    missing = [n for n in WANTED if n not in ns]
    if missing:
        raise SystemExit("%s 里找不到：%s" % (os.path.basename(path), ", ".join(missing)))
    return ns["decode_bytes"]


def old_decode_bytes(b: bytes) -> str:
    """2026-10-05 修复前的顺序：big5 排在 gb18030 前面。"""
    for enc in ("utf-8-sig", "utf-8", "big5", "gb18030"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace")


def preview(t: str, n: int = 18) -> str:
    t = t.replace("\n", "\\n")
    return t if len(t) <= n else t[:n] + "…"


def _width(s: str) -> int:
    """按终端显示宽度算：全角字符占两格。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s: str, n: int) -> str:
    return s + " " * max(1, n - _width(s))


def main() -> int:
    new_decode = load_new()
    rows = []
    old_bad = new_bad = 0
    for name, raw, want in CASES:
        got_old = old_decode_bytes(raw)
        got_new = new_decode(raw)
        ok_old, ok_new = got_old == want, got_new == want
        old_bad += 0 if ok_old else 1
        new_bad += 0 if ok_new else 1
        rows.append((name, ok_old, ok_new, got_old, got_new, want))

    print("读伴 core · decode_bytes 编码自测  (%s)" % os.path.basename(SERVER))
    print(_pad("用例", 30) + _pad("修复前", 10) + "修复后")
    print("-" * 50)
    for name, ok_old, ok_new, *_ in rows:
        print(_pad(name, 30) + _pad("通过" if ok_old else "失败", 10)
              + ("通过" if ok_new else "失败"))
    for name, ok_old, ok_new, got_old, got_new, want in rows:
        if not ok_old:
            print("\n[对照] %s\n  期望：%s\n  修复前：%s" % (name, preview(want), preview(got_old)))
    for name, ok_old, ok_new, got_old, got_new, want in rows:
        if not ok_new:
            print("\n[回归] %s\n  期望：%s\n  修复后：%s" % (name, preview(want), preview(got_new)))
    print("\n合计：修复后 %d/%d 通过；修复前 %d/%d 失败。"
          % (len(CASES) - new_bad, len(CASES), old_bad, len(CASES)))
    return 1 if new_bad else 0


if __name__ == "__main__":
    sys.exit(main())
