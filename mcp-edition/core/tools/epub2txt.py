#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
epub2txt.py — 把 epub 转成纯 txt，给「读伴（ReaderMate）· v1.0」用。

只准用标准库：zipfile / xml.etree / html.parser / re / os / sys / argparse。

用法：
    python epub2txt.py 1.epub
    python epub2txt.py 1.epub 2.epub -o all.txt      # 多个文件合并到 all.txt
    python epub2txt.py ./books/                       # 目录下所有 .epub
    python epub2txt.py 1.epub -o out.txt             # 单文件指定输出
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from html import unescape

# Windows 上默认 GBK 控制台会让我们 print 出去的 UTF-8 路径乱码。
# 强制让 stdout/stderr 走 UTF-8（不影响落盘编码，磁盘上一直是 UTF-8）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, io.UnsupportedOperation):
        pass


# ---------- 常量 ----------
OPF_NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}


# ---------- HTML 解析 ----------
class _HTMLToLines(HTMLParser):
    """
    把 HTML 流式解析成「带段落结构的纯文本」。

    段落分隔规则：
      - block 级元素（p / div / h1-h6 / li / blockquote / pre / tr 等）闭合时落空行。
      - 遇到 <br> 也强制换行（不额外加空行）。
    文本拼接：相邻文本之间不直接拼，留一个空格占位（结束时压成单空格），
    这样 <span>foo</span><span>bar</span> 不会变成 "foobar" 这种诡异的字符流。
    """

    BLOCK_TAGS = {
        "address", "article", "aside", "blockquote", "dd", "div", "dt",
        "fieldset", "figcaption", "figure", "footer", "form",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "header", "hr", "li", "main", "nav", "noscript",
        "ol", "p", "pre", "section", "table", "tbody", "tfoot", "thead",
        "tr", "td", "th", "ul",
    }
    SKIP_TAGS = {"script", "style", "nav"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._skip_depth = 0      # 处于 script/style/nav 里
        self._block_depth = 0     # 当前嵌套的 block 元素层数
        self._in_pre = False      # <pre> 内不做空白压合
        self._in_title = False    # <title> 通常是书名，跳过
        self._buf: list[str] = []         # 当前段落缓冲
        self._lines: list[str] = []       # 已完成的段落
        self._line_buf: list[str] = []    # 当前行（br 切分用）

    # ---- 工具方法 ----
    def _flush_line(self) -> None:
        """把当前行收进段落缓冲，再清空。"""
        if self._line_buf:
            self._buf.append("".join(self._line_buf).strip())
            self._line_buf = []

    def _flush_para(self) -> None:
        """把当前段落收进 _lines，再清空。"""
        # 段落内：行与行之间用换行，最终合并前后空白
        if self._buf or self._line_buf:
            tail = "".join(self._line_buf).strip()
            if tail:
                self._buf.append(tail)
            text = "\n".join(self._buf).strip()
            if text:
                self._lines.append(text)
            self._buf = []
            self._line_buf = []

    def _strip_nbsp_in_text(self, text: str) -> str:
        # 把普通空格留给后续 collapse；&nbsp; 解码后会变成 U+00A0，单独替换成普通空格
        return text.replace("\u00A0", " ")

    def _collapse_inline_ws(self, text: str) -> str:
        # 普通文本里把连续空白收成单空格；<pre> 里不动
        if self._in_pre:
            return text
        return re.sub(r"[ \t\f\v]+", " ", text)

    # ---- 回调 ----
    def handle_starttag(self, tag: str, attrs: list) -> None:
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "pre":
            # 进 pre 前先清掉段落，免得把 pre 之前的内容混进来
            self._flush_para()
            self._in_pre = True
        if tag in self.BLOCK_TAGS:
            # 进入 block 之前先把之前的内容封口
            if self._block_depth == 0:
                self._flush_para()
            self._block_depth += 1
        if tag == "br":
            self._flush_line()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if tag == "title":
            self._in_title = False
            return
        if tag == "pre":
            # pre 结束时整段作为一段保留
            self._flush_line()
            if self._buf:
                self._lines.append("\n".join(self._buf).rstrip())
                self._buf = []
            self._in_pre = False
            # pre 闭合算一个 block 段
            self._lines.append("")  # 段落分隔空行
            return
        if tag == "li":
            # 列表项闭合：把当前内容作为独立一段，再额外插一行做列表分隔
            self._flush_para()
            return
        if tag in self.BLOCK_TAGS:
            if self._block_depth > 0:
                self._block_depth -= 1
            if self._block_depth == 0:
                self._flush_para()

    def handle_data(self, data: str) -> None:
        if self._skip_depth or self._in_title:
            return
        if not data:
            return
        text = self._strip_nbsp_in_text(data)
        text = self._collapse_inline_ws(text)
        if not text:
            return
        self._line_buf.append(text)

    def handle_entityref(self, name: str) -> None:
        # 只走 handle_data 的路子，保持简单
        self.handle_data("&" + name + ";")

    def handle_charref(self, name: str) -> None:
        self.handle_data("&#" + name + ";")

    def close(self) -> list[str]:
        # 把残留的收掉
        self._flush_para()
        # 段落之间用空行隔开
        out: list[str] = []
        for line in self._lines:
            out.append(line)
            out.append("")
        # 去尾
        while out and out[-1] == "":
            out.pop()
        return out


def html_to_lines(html_bytes: bytes) -> list[str]:
    """bytes -> 行列表（已 unescape）。"""
    # 优先按 BOM / 头嗅探编码；嗅探失败就 utf-8，最后兜底 latin-1
    text: str
    if html_bytes.startswith(b"\xef\xbb\xbf"):
        text = html_bytes[3:].decode("utf-8", errors="replace")
    elif html_bytes.startswith(b"\xff\xfe") or html_bytes.startswith(b"\xfe\xff"):
        text = html_bytes.decode("utf-16", errors="replace")
    else:
        m = re.match(rb"<\?xml[^>]*encoding=[\"']([^\"']+)[\"']", html_bytes[:200], re.I)
        if m:
            try:
                text = html_bytes.decode(m.group(1).decode("ascii"), errors="replace")
            except (LookupError, UnicodeDecodeError):
                text = html_bytes.decode("utf-8", errors="replace")
        else:
            try:
                text = html_bytes.decode("utf-8")
            except UnicodeDecodeError:
                text = html_bytes.decode("latin-1", errors="replace")

    parser = _HTMLToLines()
    try:
        parser.feed(text)
    except Exception:
        # 部分 html 残破，不要整个 epub 挂掉
        pass
    lines = parser.close()
    # 行级 unescape（_HTMLToLines 已经 convert_charrefs=False，所以这里兜底）
    return [unescape(line) for line in lines]


# ---------- epub 解析 ----------
def find_opf_path(zf: zipfile.ZipFile) -> str:
    """META-INF/container.xml -> OPF 包内路径。"""
    try:
        data = zf.read("META-INF/container.xml")
    except KeyError as e:
        raise RuntimeError("不是 epub：缺少 META-INF/container.xml") from e
    root = ET.fromstring(data)
    ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
    # container.xml 里有 <rootfile full-path="..." media-type="application/oebps-package+xml"/>
    rfile = root.find("c:rootfiles/c:rootfile", ns)
    if rfile is None:
        # 兜底：找任何 rootfile
        rfile = root.find(".//c:rootfile", ns)
    if rfile is None or "full-path" not in rfile.attrib:
        raise RuntimeError("container.xml 找不到 rootfile")
    return rfile.attrib["full-path"]


def read_opf(zf: zipfile.ZipFile, opf_path: str):
    """
    读 opf，返回 (opf_dir, spine_order)。
    spine_order: 按阅读顺序排列的 href 列表（已 unquote、相对 opf 目录）。
    """
    try:
        data = zf.read(opf_path)
    except KeyError as e:
        raise RuntimeError(f"opf 文件不存在：{opf_path}") from e
    root = ET.fromstring(data)
    manifest = {}
    for item in root.findall("opf:manifest/opf:item", OPF_NS):
        href = item.attrib.get("href", "")
        if not href:
            continue
        manifest[item.attrib.get("id", href)] = href
    spine_hrefs: list[str] = []
    spine = root.find("opf:spine", OPF_NS)
    if spine is not None:
        for itemref in spine.findall("opf:itemref", OPF_NS):
            idref = itemref.attrib.get("idref", "")
            if idref in manifest:
                spine_hrefs.append(manifest[idref])
            else:
                # 兜底：有些 opf 里 itemref 直接写了 href（不规范但偶有）
                if idref:
                    spine_hrefs.append(idref)
    opf_dir = os.path.dirname(opf_path)
    # 用 posix 路径做拼接，避免 zipfile 在 Windows 上吐奇怪
    def _join(base: str, rel: str) -> str:
        # rel 里 url 编码过的字符要解
        rel = rel.replace("%20", " ")
        parts = []
        for seg in (base + "/" + rel).split("/") if base else rel.split("/"):
            if seg == "" or seg == ".":
                continue
            if seg == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(seg)
        return "/".join(parts)
    spine_hrefs = [_join(opf_dir, h) for h in spine_hrefs]
    return opf_dir, spine_hrefs


def extract_text(epub_path: str) -> tuple[str, list[str]]:
    """
    解析整个 epub，返回 (book_title, lines)。
    解析失败抛 RuntimeError，单章节解析失败跳过。
    """
    with zipfile.ZipFile(epub_path) as zf:
        opf_path = find_opf_path(zf)
        _opf_dir, spine_hrefs = read_opf(zf, opf_path)
        # 书名（尽力从 dc:title 取）
        try:
            opf_root = ET.fromstring(zf.read(opf_path))
            tnode = opf_root.find("opf:metadata/dc:title", OPF_NS)
            book_title = (tnode.text or "").strip() if tnode is not None and tnode.text else ""
        except Exception:
            book_title = ""
        out_lines: list[str] = []
        if book_title:
            out_lines.append(book_title)
            out_lines.append("")
        skip_files: list[str] = []
        for href in spine_hrefs:
            if not href:
                continue
            try:
                data = zf.read(href)
            except KeyError:
                skip_files.append(href)
                continue
            try:
                lines = html_to_lines(data)
            except Exception:
                skip_files.append(href)
                continue
            if lines:
                out_lines.extend(lines)
                out_lines.append("")  # 章节之间留空行
        if skip_files:
            sys.stderr.write(
                f"[warn] {os.path.basename(epub_path)}: 跳过 {len(skip_files)} 个解析失败的资源: "
                + ", ".join(skip_files) + "\n"
            )
    # 收尾：去掉文末多余的空行
    while out_lines and out_lines[-1] == "":
        out_lines.pop()
    return book_title, out_lines


# ---------- CLI ----------
def _collect_inputs(args_paths: list[str]) -> list[str]:
    """把入参展平成 epub 路径列表（支持多文件和目录）。"""
    out: list[str] = []
    for p in args_paths:
        if os.path.isdir(p):
            for name in sorted(os.listdir(p)):
                if name.lower().endswith(".epub"):
                    out.append(os.path.join(p, name))
        elif os.path.isfile(p):
            out.append(p)
        else:
            sys.stderr.write(f"[warn] 跳过不存在项：{p}\n")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="epub2txt",
        description="epub → txt 转换器（纯标准库）。",
    )
    ap.add_argument("inputs", nargs="+", help="一个或多个 .epub 文件，或一个目录")
    ap.add_argument(
        "-o", "--output",
        help="输出 txt 路径。单个输入时缺省=同目录同名的 .txt；多个输入时缺省=合并到同名 .txt（首个输入同名）。",
    )
    args = ap.parse_args(argv)

    inputs = _collect_inputs(args.inputs)
    if not inputs:
        sys.stderr.write("没有可处理的 epub。\n")
        return 1

    multi = len(inputs) > 1
    if args.output:
        out_path = args.output
    else:
        if multi:
            # 多个输入：合并写到第一个输入的同目录 .txt
            first = inputs[0]
            out_path = os.path.splitext(first)[0] + ".txt"
        else:
            out_path = os.path.splitext(inputs[0])[0] + ".txt"

    all_lines: list[str] = []
    any_written = False
    per_file_results: list[tuple[str, str, int]] = []  # (src, dst, char_count)

    for src in inputs:
        try:
            title, lines = extract_text(src)
        except Exception as e:
            sys.stderr.write(f"[fail] {src}: {e}\n")
            continue
        if multi:
            # 合并模式：每个文件之间隔空行
            if all_lines:
                all_lines.append("")
                all_lines.append("=" * 40)
                all_lines.append("")
            if title and title not in all_lines[:3]:
                all_lines.append(title)
                all_lines.append("")
            all_lines.extend(lines)
        else:
            # 单文件模式：直接落盘
            text = "\n".join(lines) + "\n"
            try:
                with open(out_path, "w", encoding="utf-8", newline="") as f:
                    f.write(text)
            except OSError as e:
                sys.stderr.write(f"[fail] 写 {out_path} 失败：{e}\n")
                return 2
            char_count = len(text)
            per_file_results.append((src, out_path, char_count))
            any_written = True
            print(f"{src} -> {out_path} ({char_count} chars)")

    if multi:
        text = "\n".join(all_lines) + "\n" if all_lines else ""
        try:
            with open(out_path, "w", encoding="utf-8", newline="") as f:
                f.write(text)
        except OSError as e:
            sys.stderr.write(f"[fail] 写 {out_path} 失败：{e}\n")
            return 2
        any_written = True
        per_file_results.append(("<merged>", out_path, len(text)))
        print(f"<{len(inputs)} files> -> {out_path} ({len(text)} chars)")

    return 0 if any_written else 3


if __name__ == "__main__":
    raise SystemExit(main())