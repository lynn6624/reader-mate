#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backup.py — 「读伴（ReaderMate）· v1.0」数据备份 / 迁移工具。

把 `书库/` 与 `data/`（progress.json、library.json、阅读进度.md）打包成
单一 zip（外加一份 manifest.json），方便换机、分享、归档；也能从包还原回去。

零第三方依赖，仅 zipfile / json / hashlib / argparse / shutil /
datetime / os / sys / tempfile。

用法：
    python backup.py export [-o 输出路径] [--books-only] [--progress-only] [--note 备注]
    python backup.py info 包.zip
    python backup.py import 包.zip [--overwrite] [--dry-run]

设计取舍：
- 工具路径全部以 tools/ 的父目录为锚（os.path.dirname(__file__) 上一层），
  不写死盘符，方便 Windows 与手机 proot Linux 共用同一份代码。
- manifest.json 同时存「书数 / 总字符数 / 进度条数」这类元数据，
  让 info 子命令不读正文也能给一份概要。
- 解包时严防 zip slip：绝对路径、含 `..`、指向锚目录之外，统统拒绝。
  顺手跳过 macOS / Windows 噪音：__MACOSX/、.DS_Store、Thumbs.db。
- import 冲突时默认保守：书按存在即跳过计入 skipped；进度文件按
  同书 updated 较新一条合并，加 --overwrite 才整份替换。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime

# Windows 控制台默认 GBK 会让 print 出来的中文路径乱码，
# 这里只动控制台编码，不动磁盘编码（磁盘上始终是 UTF-8）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, io.UnsupportedOperation):
        pass


# ---------- 常量 ----------

FORMAT_VERSION = 1
TOP_DIR_NAME = "阅读器备份"        # zip 里的顶层目录
PROGRESS_FILES = ("progress.json", "library.json", "阅读进度.md")
SKIP_NAMES = {"__MACOSX", ".DS_Store", "Thumbs.db"}
ALGO = "utf-8"


# ---------- 路径 ----------

def project_root() -> str:
    """tools/ 的上一层，即项目根目录。"""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(here)


def books_dir(root: str) -> str:
    return os.path.join(root, "书库")


def data_dir(root: str) -> str:
    return os.path.join(root, "data")


# ---------- manifest ----------

def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def source_os() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return sys.platform or "unknown"


def _read_json_or_default(path: str, default):
    try:
        with open(path, "r", encoding=ALGO) as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        # 损坏的 json 视作空，避免迁移工具自己挂掉
        return default


def manifest_of(root: str, include_books: bool, include_progress: bool) -> dict:
    """统计 books/ 与 progress.json 元信息；不读任何正文只做计数。"""
    book_count = 0
    book_chars = 0
    if include_books:
        bdir = books_dir(root)
        if os.path.isdir(bdir):
            for name in os.listdir(bdir):
                p = os.path.join(bdir, name)
                if not (os.path.isfile(p) and name.lower().endswith(".txt")):
                    continue
                book_count += 1
                try:
                    with open(p, "rb") as f:
                        # decode 用 utf-8 容错：长度按「解码后字符数」算
                        raw = f.read()
                    book_chars += len(raw.decode(ALGO, errors="replace"))
                except OSError:
                    pass

    progress_entries = 0
    if include_progress:
        prog = _read_json_or_default(
            os.path.join(data_dir(root), "progress.json"), {}
        )
        if isinstance(prog, dict):
            # 数 books 里的条目，不是数顶层值（顶层只有 version/updated/books 三个键，
            # 之前写成 sum(1 for v in prog.values() if isinstance(v, dict)) 永远得 1）
            books_map = prog.get("books")
            if isinstance(books_map, dict):
                progress_entries = len(books_map)

    return {
        "format_version": FORMAT_VERSION,
        "created": now_iso(),
        "note": "",
        "source_os": source_os(),
        "books": book_count,
        "book_chars": book_chars,
        "progress_entries": progress_entries,
        "files": [],   # 写 zip 时再回填
    }


# ---------- 安全：zip slip 防护 ----------

def _safe_member(rel_path: str, root: str) -> str:
    """校验 zip 成员路径：禁止绝对、禁止 ..、禁止跳出 root。"""
    # 1) 不接受绝对路径（POSIX / Windows 都拦）
    if os.path.isabs(rel_path) or os.path.splitdrive(rel_path)[0]:
        raise ValueError(f"成员路径为绝对路径，拒绝：{rel_path!r}")
    # 2) 规范化后仍出现 .. 一律拒
    norm = os.path.normpath(rel_path).replace("\\", "/")
    if norm == ".." or norm.startswith("../"):
        raise ValueError(f"成员路径含 .. 或跨越目录，拒绝：{rel_path!r}")
    # 3) 最终落点必须在 root 之内（防 zip slip）
    target = os.path.abspath(os.path.join(root, norm))
    root_abs = os.path.abspath(root) + os.sep
    if not (target + os.sep).startswith(root_abs):
        raise ValueError(f"成员路径跳出解压根目录，拒绝：{rel_path!r}")
    return norm


def _skip_noise(name: str) -> bool:
    base = os.path.basename(name.rstrip("/"))
    if base in SKIP_NAMES:
        return True
    # __MACOSX/ 是目录，目录名前缀匹配即可
    parts = name.replace("\\", "/").split("/")
    if any(p in SKIP_NAMES for p in parts):
        return True
    return False


# ---------- 写盘辅助 ----------

def _ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)


def _hash_file(path: str) -> tuple[str, int]:
    h = hashlib.sha256()
    total = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
            total += len(chunk)
    return h.hexdigest()[:16], total


# ---------- 子命令：export ----------

def cmd_export(args: argparse.Namespace) -> int:
    root = project_root()
    include_books = not args.progress_only
    include_progress = not args.books_only

    if not include_books and not include_progress:
        print("错误：--books-only 与 --progress-only 不能同时指定。", file=sys.stderr)
        return 2

    # 默认输出文件名（带时间戳）
    if args.output:
        out_path = os.path.abspath(args.output)
        out_dir = os.path.dirname(out_path) or "."
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        out_dir = os.getcwd()
        out_path = os.path.join(out_dir, f"阅读器备份-{stamp}.zip")

    _ensure_dir(out_dir)

    manifest = manifest_of(root, include_books, include_progress)
    manifest["note"] = args.note or ""

    # 收集要写入的源文件（相对 root 的路径）
    sources: list[tuple[str, str]] = []  # (abs_src, rel_in_zip)

    if include_books:
        bdir = books_dir(root)
        if os.path.isdir(bdir):
            for name in sorted(os.listdir(bdir)):
                src = os.path.join(bdir, name)
                if not (os.path.isfile(src) and name.lower().endswith(".txt")):
                    continue
                rel = os.path.join(TOP_DIR_NAME, "书库", name)
                sources.append((src, rel))

    if include_progress:
        ddir = data_dir(root)
        for name in PROGRESS_FILES:
            src = os.path.join(ddir, name)
            if os.path.isfile(src):
                rel = os.path.join(TOP_DIR_NAME, "data", name)
                sources.append((src, rel))

    # 把 manifest 写一份到临时文件，结束前追加 files 列表再并入
    files_meta: list[dict] = []

    # 写到临时文件，再原子改名；避免半成品 zip 被误以为备份成功
    fd, tmp_zip = tempfile.mkstemp(prefix="阅读器备份-", suffix=".zip.tmp")
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for abs_src, rel in sources:
                digest, size = _hash_file(abs_src)
                files_meta.append({
                    "path": rel,
                    "sha256_16": digest,
                    "bytes": size,
                })
                zf.write(abs_src, arcname=rel)

            manifest["files"] = files_meta
            zf.writestr(
                os.path.join(TOP_DIR_NAME, "manifest.json"),
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )

        shutil.move(tmp_zip, out_path)
    except Exception:
        # 失败清理临时 zip
        try:
            os.remove(tmp_zip)
        except OSError:
            pass
        raise

    print(f"已导出：{out_path}")
    print(
        f"  书 {manifest['books']} 本；总字符 {manifest['book_chars']}；"
        f"进度条目 {manifest['progress_entries']} 条；文件 {len(files_meta)} 个"
    )
    if manifest["note"]:
        print(f"  备注：{manifest['note']}")
    print("  提示：这个包里装着你书库里的全部书籍，别随手转发或传到公开位置。")
    return 0


# ---------- 子命令：info ----------

def _read_manifest_from_zip(zf: zipfile.ZipFile) -> dict | None:
    # zip 内部路径永远用 POSIX 风格；不要走 os.path.join（Windows 会变反斜杠）
    for cand in (
        f"{TOP_DIR_NAME}/manifest.json",
        "manifest.json",
    ):
        try:
            data = zf.read(cand)
        except KeyError:
            continue
        try:
            return json.loads(data)
        except ValueError:
            return None
    return None


def cmd_info(args: argparse.Namespace) -> int:
    path = os.path.abspath(args.zip)
    if not os.path.isfile(path):
        print(f"错误：找不到包：{path}", file=sys.stderr)
        return 2
    if not zipfile.is_zipfile(path):
        print(f"错误：不是有效的 zip 文件：{path}", file=sys.stderr)
        return 2

    with zipfile.ZipFile(path, "r") as zf:
        manifest = _read_manifest_from_zip(zf)
        names = zf.namelist()

    print(f"包路径：{path}")
    if not manifest:
        print("manifest.json 缺失或无法解析。")
    else:
        print(f"format_version：{manifest.get('format_version')}")
        print(f"created      ：{manifest.get('created')}")
        print(f"source_os    ：{manifest.get('source_os')}")
        print(f"note         ：{manifest.get('note') or '（无）'}")
        print(f"books        ：{manifest.get('books')}")
        print(f"book_chars   ：{manifest.get('book_chars')}")
        print(f"progress_entries：{manifest.get('progress_entries')}")

    print(f"\n文件清单（共 {len(names)} 项）：")
    for n in names:
        print(f"  {n}")
    return 0


# ---------- 子命令：import ----------

def _load_progress_map(path: str) -> dict:
    return _read_json_or_default(path, {})


def _save_progress_map(path: str, data: dict) -> None:
    _ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding=ALGO) as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _merge_progress(base: dict, incoming: dict) -> tuple[dict, int, int]:
    """合并进度：同书取 updated 较新一条；不同书并入。返回 (新表, 新增, 覆盖)。"""
    if not isinstance(base, dict):
        base = {}
    if not isinstance(incoming, dict):
        return dict(base), 0, 0
    added = replaced = 0
    for book, rec in incoming.items():
        if not isinstance(rec, dict):
            continue
        old = base.get(book)
        if not isinstance(old, dict):
            base[book] = rec
            added += 1
            continue
        old_up = str(old.get("updated", ""))
        new_up = str(rec.get("updated", ""))
        if new_up > old_up:
            base[book] = rec
            replaced += 1
    return base, added, replaced


def cmd_import(args: argparse.Namespace) -> int:
    root = project_root()
    zpath = os.path.abspath(args.zip)
    if not os.path.isfile(zpath):
        print(f"错误：找不到包：{zpath}", file=sys.stderr)
        return 2
    if not zipfile.is_zipfile(zpath):
        print(f"错误：不是有效的 zip 文件：{zpath}", file=sys.stderr)
        return 2

    overwrite = args.overwrite
    dry_run = args.dry_run
    verb = "预览" if dry_run else "导入"
    print(f"{verb}：{zpath}")

    # 第一遍：列成员 + 安全校验
    with zipfile.ZipFile(zpath, "r") as zf:
        manifest = _read_manifest_from_zip(zf)
        members = zf.infolist()

        # 过滤噪音；用局部列表存 (ZipInfo, norm_rel)
        kept: list[tuple[zipfile.ZipInfo, str]] = []
        skipped_noise: list[str] = []
        for info in members:
            name = info.filename.replace("\\", "/")
            if _skip_noise(name):
                skipped_noise.append(name)
                continue
            if name.endswith("/"):
                continue
            try:
                norm = _safe_member(name, root)
            except ValueError as e:
                print(f"错误：{e}", file=sys.stderr)
                return 3
            kept.append((info, norm))

        # 把 manifest 的「书 / 进度」分成两组
        book_members: list[tuple[zipfile.ZipInfo, str]] = []
        progress_members: list[tuple[zipfile.ZipInfo, str]] = []
        for info, norm in kept:
            rel_posix = norm.replace("\\", "/")
            if "/书库/" in rel_posix or rel_posix.startswith("书库/"):
                book_members.append((info, norm))
            elif "/data/" in rel_posix or rel_posix.startswith("data/"):
                progress_members.append((info, norm))

        if skipped_noise:
            print(f"已跳过噪音文件 {len(skipped_noise)} 项：")
            for n in skipped_noise:
                print(f"  - {n}")

        # ---- 书：逐本处理 ----
        bdir = books_dir(root)
        ddir = data_dir(root)
        books_added = books_skipped = books_overwritten = 0
        planned_actions: list[str] = []

        if dry_run:
            print("[dry-run] 不会写盘，仅打印计划。")

        for info, rel in book_members:
            # rel 形如 <TOP_DIR>/书库/<name>.txt
            parts = rel.replace("\\", "/").split("/")
            try:
                idx = parts.index("书库")
            except ValueError:
                continue
            fname = "/".join(parts[idx + 1:])
            if not fname:
                continue
            dst = os.path.join(bdir, fname)
            exists = os.path.isfile(dst)

            if exists and not overwrite:
                books_skipped += 1
                planned_actions.append(f"跳过（同名书已存在）：{os.path.relpath(dst, root)}")
                continue

            if exists and overwrite:
                books_overwritten += 1
                planned_actions.append(f"覆盖：{os.path.relpath(dst, root)}")
            else:
                books_added += 1
                planned_actions.append(f"新增：{os.path.relpath(dst, root)}")

            if not dry_run:
                _ensure_dir(bdir)
                with zf.open(info, "r") as src, open(dst, "wb") as out:
                    shutil.copyfileobj(src, out)

        # ---- 进度文件 ----
        prog_actions: list[str] = []
        if progress_members and not overwrite:
            # 合并模式：逐个读取并合并 progress.json；其他文件视为按覆盖或跳过
            for info, rel in progress_members:
                parts = rel.replace("\\", "/").split("/")
                try:
                    idx = parts.index("data")
                except ValueError:
                    continue
                fname = "/".join(parts[idx + 1:])
                if not fname:
                    continue
                dst = os.path.join(ddir, fname)
                _ensure_dir(ddir)

                if fname == "progress.json":
                    incoming_raw = zf.read(info)
                    try:
                        incoming = json.loads(incoming_raw)
                    except ValueError:
                        prog_actions.append(f"跳过（包内 progress.json 解析失败）：{fname}")
                        continue
                    base = _load_progress_map(dst) if os.path.isfile(dst) else {}
                    merged, added_n, replaced_n = _merge_progress(base, incoming)
                    prog_actions.append(
                        f"合并 progress.json：新增 {added_n}，覆盖 {replaced_n}（基于 updated 时间）"
                    )
                    if not dry_run:
                        _save_progress_map(dst, merged)
                else:
                    # library.json / 阅读进度.md 默认跳过（避免覆盖用户当前真实状态）
                    if os.path.isfile(dst):
                        prog_actions.append(f"跳过（同名已存在，加 --overwrite 才覆盖）：{fname}")
                        continue
                    prog_actions.append(f"新增：data/{fname}")
                    if not dry_run:
                        with zf.open(info, "r") as src, open(dst, "wb") as out:
                            shutil.copyfileobj(src, out)

        elif progress_members and overwrite:
            for info, rel in progress_members:
                parts = rel.replace("\\", "/").split("/")
                try:
                    idx = parts.index("data")
                except ValueError:
                    continue
                fname = "/".join(parts[idx + 1:])
                if not fname:
                    continue
                dst = os.path.join(ddir, fname)
                _ensure_dir(ddir)
                if os.path.isfile(dst):
                    prog_actions.append(f"覆盖：data/{fname}")
                else:
                    prog_actions.append(f"新增：data/{fname}")
                if not dry_run:
                    with zf.open(info, "r") as src, open(dst, "wb") as out:
                        shutil.copyfileobj(src, out)

        # ---- 汇总 ----
        print("\n书：")
        for line in planned_actions:
            print(f"  {line}")
        print(f"  小计：新增 {books_added}，跳过 {books_skipped}，覆盖 {books_overwritten}")

        if prog_actions:
            print("\n进度 / 数据：")
            for line in prog_actions:
                print(f"  {line}")

        if manifest:
            print("\n源 manifest：")
            print(f"  created    ：{manifest.get('created')}")
            print(f"  note       ：{manifest.get('note') or '（无）'}")
            print(f"  books      ：{manifest.get('books')}")
            print(f"  progress   ：{manifest.get('progress_entries')}")

        if dry_run:
            print("\n[dry-run] 未对磁盘做任何修改。")

    return 0


# ---------- CLI ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="backup.py",
        description="「读伴（ReaderMate）· v1.0」数据备份与迁移工具。",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pe = sub.add_parser("export", help="打包 书库/ 与 data/ 为一个 zip")
    pe.add_argument("-o", "--output", help="输出 zip 路径，默认 ./阅读器备份-YYYYMMDD-HHMM.zip")
    pe.add_argument("--books-only", action="store_true", help="只打包 书库/")
    pe.add_argument("--progress-only", action="store_true", help="只打包 data/")
    pe.add_argument("--note", default="", help="写进 manifest 的备注")
    pe.set_defaults(func=cmd_export)

    pi = sub.add_parser("info", help="查看一个备份包的 manifest 与文件清单")
    pi.add_argument("zip", help="要查看的 zip 路径")
    pi.set_defaults(func=cmd_info)

    pm = sub.add_parser("import", help="把备份包还原到 书库/ 与 data/")
    pm.add_argument("zip", help="要还原的 zip 路径")
    pm.add_argument("--overwrite", action="store_true", help="同名书与进度文件都覆盖")
    pm.add_argument("--dry-run", action="store_true", help="只打印计划，不写盘")
    pm.set_defaults(func=cmd_import)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())