#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读伴（ReaderMate）· v1.0 —— 本机单文件后端

单文件后端，纯标准库。页面在 web/reader.html，须与本脚本同在项目根下。

目录约定（AI 可直接读的固定路径）：
  书库/*.txt              只认 txt；epub 进门先转 txt
  data/library.json       书库索引（书名/字节/字符数/mtime/导入时间）
  data/progress.json      阅读进度（书/字符偏移/百分比/章节/锚文本/时间）—— 正本
  data/阅读进度.md        同内容的表格版，给人/给 AI 扫一眼用

启动：python reader_server.py           默认 127.0.0.1:8831
      可用环境变量 READER_HOST / READER_PORT 覆盖
"""
import hashlib
import hmac
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
BOOKS_DIR = os.path.join(BASE, "书库")
DATA_DIR = os.path.join(BASE, "data")
HTML_FILE = os.path.join(BASE, "web", "reader.html")
PROGRESS_FILE = os.path.join(DATA_DIR, "progress.json")
LIBRARY_FILE = os.path.join(DATA_DIR, "library.json")
NOTES_FILE = os.path.join(DATA_DIR, "notes.json")
MD_FILE = os.path.join(DATA_DIR, "阅读进度.md")

HOST = os.environ.get("READER_HOST", "127.0.0.1")
PORT = int(os.environ.get("READER_PORT", "8831"))
MAX_BODY = 64 * 1024 * 1024  # 单本 txt 上限 64MB，够用

# 可选 token 鉴权：设了 READER_TOKEN 才启用；启用后所有 /api/* 必须带 token。
# 接受 ?token=xxx 或请求头 X-Token: xxx。比对用 hmac.compare_digest 防时序攻击。
TOKEN = os.environ.get("READER_TOKEN", "").strip()

_lock = threading.Lock()


# ---------------------------------------------------------------- 基础工具
def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def mtime_tag(path: str) -> str:
    """文件改动时间的短标签（MM-DD HH:MM）。

    用途：页面右下角显示「内核 xx · 后端 xx」，一眼确认跑的是哪一版——
    专治「本地没更新 / 手机上是旧的」这类说不清的疑问。
    """
    try:
        return datetime.fromtimestamp(os.path.getmtime(path)).strftime("%m-%d %H:%M")
    except OSError:
        return "?"


# ---------------------------------------------------------------- 朗读（TTS，可选）
# 浏览器内置的语音合成（speechSynthesis）在部分 WebView 里根本不可用（Operit 就中招），
# 所以这里给一条「后端合成 → 前端用 <audio> 播」的兜底路：音频播放哪儿都支持。
#
# 这是**可选功能**：不配置就完全不启用（/api/tts 回 501，前端自动回落到内置语音）。
# 「纯标准库、零依赖」是这个项目的卖点，不能被它破坏。
#
# 配置来源（环境变量优先，其次 data/tts.json）：
#   READER_TTS        minimax | openai    （不设 = 关闭）
#   READER_TTS_KEY    API key
#   READER_TTS_MODEL  可选：默认 minimax=speech-02-hd / openai=tts-1
#   READER_TTS_VOICE  可选：默认 minimax=female-shaonv / openai=alloy
#   READER_TTS_BASE   可选：自定义 endpoint（openai 兼容的服务，如硅基流动）
TTS_TEXT_MAX = 800        # 单次合成的字数上限（朗读按段来，段本身最长 1200）
TTS_CACHE_MAX = 80        # 缓存多少段音频：同一段重听不再重复计费
_tts_cache = {}
_tts_cache_order = []


class TTSError(Exception):
    """朗读服务出错（没配置 / 上游返回错误 / 连不上）。"""


def load_tts_config() -> dict:
    """读朗读配置：环境变量优先，其次 data/tts.json。"""
    cfg = {}
    p = os.path.join(DATA_DIR, "tts.json")
    try:
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                cfg = d
    except Exception:
        cfg = {}

    def pick(env: str, key: str, default: str = "") -> str:
        v = (os.environ.get(env) or "").strip()
        if not v:
            v = str(cfg.get(key) or "").strip()
        return v or default

    return {
        "provider": pick("READER_TTS", "provider").lower(),
        "key": pick("READER_TTS_KEY", "key"),
        "model": pick("READER_TTS_MODEL", "model"),
        "voice": pick("READER_TTS_VOICE", "voice"),
        "base": pick("READER_TTS_BASE", "base").rstrip("/"),
    }


def tts_available() -> bool:
    """配了 provider 和 key 才算可用。"""
    c = load_tts_config()
    return bool(c["provider"] and c["key"])


def tts_minimax(text: str, voice: str, speed: float, cfg: dict):
    """MiniMax 同步语音合成。返回 (音频 bytes, content_type)。"""
    vid = voice or cfg["voice"] or "female-shaonv"
    body = {
        "model": cfg["model"] or "speech-02-hd",
        "text": text,
        "stream": False,
        "voice_setting": {"voice_id": vid, "speed": speed, "vol": 1.0, "pitch": 0},
        "audio_setting": {"format": "mp3", "sample_rate": 32000, "bitrate": 128000, "channel": 1},
        # 用 hex 而不是 url：我们解码后自己吐音频流，不依赖外部 CDN 在手机端可达
        "output_format": "hex",
    }
    base = cfg["base"] or "https://api.minimax.cn"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        base + "/v1/t2a_v2", data=data, method="POST",
        headers={"Authorization": "Bearer " + cfg["key"], "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        j = json.loads(r.read().decode("utf-8"))
    br = j.get("base_resp") or {}
    code = br.get("status_code")
    if code not in (0, None):
        raise TTSError("上游返回 %s：%s" % (code, br.get("status_msg") or ""))
    hexs = (j.get("data") or {}).get("audio") or ""
    if not hexs:
        raise TTSError("上游没返回音频")
    try:
        return bytes.fromhex(hexs), "audio/mpeg"
    except ValueError:
        raise TTSError("上游音频数据格式不对")


def tts_openai(text: str, voice: str, speed: float, cfg: dict):
    """OpenAI 兼容的 /v1/audio/speech（硅基流动一类服务都能接）。"""
    base = cfg["base"] or "https://api.openai.com"
    body = {
        "model": cfg["model"] or "tts-1",
        "input": text,
        "voice": voice or cfg["voice"] or "alloy",
        "speed": speed,
        "response_format": "mp3",
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        base + "/v1/audio/speech", data=data, method="POST",
        headers={"Authorization": "Bearer " + cfg["key"], "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read(), "audio/mpeg"


def tts_synthesize(text: str, voice: str, speed: float):
    """合成一段（带内存缓存：同一段重听不再计费）。"""
    cfg = load_tts_config()
    if not cfg["provider"]:
        raise TTSError("没配置朗读服务")
    ck = hashlib.sha256(
        ("%s|%s|%s|%s" % (cfg["provider"], text, voice, speed)).encode("utf-8")).hexdigest()
    with _lock:
        hit = _tts_cache.get(ck)
    if hit:
        return hit
    if cfg["provider"] == "minimax":
        out = tts_minimax(text, voice, speed, cfg)
    elif cfg["provider"] == "openai":
        out = tts_openai(text, voice, speed, cfg)
    else:
        raise TTSError("不认识的 provider：%s" % cfg["provider"])
    with _lock:
        if ck in _tts_cache:
            _tts_cache_order.remove(ck)
        _tts_cache[ck] = out
        _tts_cache_order.append(ck)
        while len(_tts_cache_order) > TTS_CACHE_MAX:
            old = _tts_cache_order.pop(0)
            _tts_cache.pop(old, None)
    return out


def decode_bytes(b: bytes) -> str:
    """txt 编码探测：utf-8 优先，再试 big5 / gb18030（国内老书多为 GBK）。"""
    for enc in ("utf-8-sig", "utf-8", "big5", "gb18030"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace")


def norm_text(t: str) -> str:
    return t.replace("\r\n", "\n").replace("\r", "\n")


def read_text(path: str) -> str:
    with open(path, "rb") as f:
        return norm_text(decode_bytes(f.read()))


def safe_name(name: str):
    """只允许书库根下的 .txt 文件名，挡掉路径穿越。"""
    name = os.path.basename((name or "").strip())
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name)
    name = name.strip(" .")
    if not name:
        return None
    if not name.lower().endswith(".txt"):
        name += ".txt"
    if name == ".txt":
        return None
    return name


def book_path(name: str) -> str:
    return os.path.join(BOOKS_DIR, name)


def atomic_write(path: str, text: str):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def dump_json(path: str, obj):
    atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- 索引 / 进度
def load_library() -> dict:
    lib = load_json(LIBRARY_FILE, {})
    if not isinstance(lib, dict):
        lib = {}
    lib.setdefault("version", 1)
    lib.setdefault("updated", now_iso())
    lib.setdefault("books", {})
    return lib


def load_progress() -> dict:
    pr = load_json(PROGRESS_FILE, {})
    if not isinstance(pr, dict):
        pr = {}
    pr.setdefault("version", 1)
    pr.setdefault("updated", now_iso())
    pr.setdefault("books", {})
    return pr


# ---------------------------------------------------------------- 划线/批注
# 划线（highlight）+ 来回批注（thread）。文件与 progress.json 同目录，独立存。
# 写盘走 atomic_write；ID 服务端生成（h_ + 12 位 hex）；字符偏移口径与 progress.offset
# 一致：对**规范化后的 \\n 文本**计数。
NOTES_VERSION = 1
HIGHLIGHT_STYLES = ("line", "wave", "dash", "bg")
HIGHLIGHT_TEXT_MAX = 4000   # 划线原文上限（按权重算）
COMMENT_WEIGHT_MAX = 300    # 单条批注上限（按**权重**算：CJK/全角 2 分、其余 1 分）
                            # 300 分 ≈ 纯中文 150 字 ≈ 纯英文 300 字符——两种语言"读起来一样长"，
                            # 跟推特同源（它的 280 也是权重，不是字符数）
COMMENT_CHARS_HARD_MAX = 600  # 字符数硬上限（只用于读旧数据时的清洗切片；校验一律按权重）
THREAD_MAX = 12             # 每条划线 thread 上限（= 6 轮来回）
CHAPTER_MAX = 80
NAME_AI_MAX = 32


def text_weight(s: str) -> int:
    """按显示宽度算权重：CJK / 全角字符 2 分，其余 1 分。

    于是"150 个汉字"和"300 个英文字符"上限相同——它们读起来一样长。
    """
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def _rand_id() -> str:
    import secrets
    return "h_" + secrets.token_hex(6)


def load_notes() -> dict:
    """读 notes.json，缺失/损坏都给空壳。"""
    nt = load_json(NOTES_FILE, {})
    if not isinstance(nt, dict):
        nt = {}
    nt.setdefault("version", NOTES_VERSION)
    nt.setdefault("updated", now_iso())
    nt.setdefault("books", {})
    books = nt["books"]
    if not isinstance(books, dict):
        nt["books"] = {}
    for name, b in list(nt["books"].items()):
        if not isinstance(b, dict):
            nt["books"][name] = {"highlights": []}
            continue
        b.setdefault("highlights", [])
        if not isinstance(b["highlights"], list):
            b["highlights"] = []
    return nt


def save_notes(nt: dict):
    nt["updated"] = now_iso()
    nt["version"] = NOTES_VERSION
    dump_json(NOTES_FILE, nt)


def _norm_style(s):
    return s if s in HIGHLIGHT_STYLES else "line"


def _norm_highlight(h: dict) -> dict:
    """规范化一条划线的字段：补默认、裁剪、收敛非法值。"""
    try:
        start = max(0, int(h.get("start") or 0))
    except Exception:
        start = 0
    try:
        end = max(start, int(h.get("end") or start))
    except Exception:
        end = start
    text = str(h.get("text") or "")[:HIGHLIGHT_TEXT_MAX]
    style = _norm_style(h.get("style") or "line")
    color = str(h.get("color") or "")[:32]
    chapter = str(h.get("chapter") or "")[:CHAPTER_MAX]
    thread = h.get("thread") or []
    if not isinstance(thread, list):
        thread = []
    norm_thread = []
    for c in thread:
        if not isinstance(c, dict):
            continue
        who = c.get("who") if c.get("who") in ("user", "ai") else None
        if who is None:
            continue
        msg = str(c.get("text") or "")[:COMMENT_CHARS_HARD_MAX]
        if not msg:
            continue
        item = {"who": who, "text": msg, "at": c.get("at") or now_iso()}
        if who == "ai" and c.get("name"):
            item["name"] = str(c.get("name"))[:NAME_AI_MAX]
        norm_thread.append(item)
    return {
        "id": str(h.get("id") or "") or _rand_id(),
        "start": start,
        "end": end,
        "text": text,
        "style": style,
        "color": color,
        "chapter": chapter,
        "created": str(h.get("created") or now_iso()),
        "thread": norm_thread,
    }


def scan_books() -> dict:
    """扫书库目录，返回 {name: 条目}，字符数带 mtime/size 缓存。"""
    lib = load_library()
    cache = lib.get("books") or {}
    out = {}
    if os.path.isdir(BOOKS_DIR):
        for fn in sorted(os.listdir(BOOKS_DIR)):
            if not fn.lower().endswith(".txt"):
                continue
            p = os.path.join(BOOKS_DIR, fn)
            if not os.path.isfile(p):
                continue
            st = os.stat(p)
            ent = cache.get(fn) or {}
            chars = ent.get("chars")
            if not chars or ent.get("mtime") != st.st_mtime or ent.get("bytes") != st.st_size:
                try:
                    chars = len(read_text(p))
                except Exception:
                    chars = 0
                ent = {
                    "file": fn,
                    "bytes": st.st_size,
                    "chars": chars,
                    "mtime": st.st_mtime,
                    "imported": ent.get("imported") or now_iso(),
                }
            out[fn] = {
                "name": fn,
                "title": os.path.splitext(fn)[0],
                "bytes": st.st_size,
                "chars": chars or 0,
                "mtime": st.st_mtime,
                "imported": ent.get("imported") or now_iso(),
            }
    lib["books"] = out
    lib["updated"] = now_iso()
    dump_json(LIBRARY_FILE, lib)
    return out


def write_progress_md(pr: dict, lib: dict):
    """给人/给 AI 一眼看的表格版。正本仍是 progress.json。"""
    def cell(s, limit=48):
        s = re.sub(r"\s+", " ", str(s or "")).replace("|", "｜").strip()
        return (s[:limit] + "…") if len(s) > limit else (s or "—")

    rows = []
    for name, p in sorted((pr.get("books") or {}).items(),
                          key=lambda kv: kv[1].get("updated") or "", reverse=True):
        entry = (lib.get("books") or {}).get(name) or {}
        chars = p.get("chars") or entry.get("chars") or 0
        pct = p.get("percent")
        pct_s = f"{pct * 100:.1f}%" if isinstance(pct, (int, float)) else "—"
        rows.append("| {} | {} | {}/{} 字 | {} | {} | {} |".format(
            cell(name, 40), pct_s, p.get("offset", 0), chars,
            cell(p.get("chapter")), cell(p.get("anchor")), p.get("updated") or "—"))

    md = [
        "# 读伴 · 阅读进度",
        "",
        "> 本文件由 `reader_server.py` 自动生成，**勿手改**；正本是同目录 `progress.json`。",
        "> 更新：{}　｜　书库：`书库/`".format(pr.get("updated") or now_iso()),
        "",
        "| 书 | 进度 | 字符偏移 | 章节 | 锚文本 | 更新时间 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    md.extend(rows or ["| （还没有阅读记录） | — | — | — | — | — |"])
    md.append("")
    atomic_write(MD_FILE, "\n".join(md))


def save_progress(pr: dict):
    pr["updated"] = now_iso()
    dump_json(PROGRESS_FILE, pr)
    write_progress_md(pr, load_library())


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "ReaderMate/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def handle_one_request(self):
        """浏览器/客户端断连是常态，别把 keep-alive 复位刷成 traceback。"""
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            self.close_connection = True

    # ---- 收发 ----
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, msg, code=400):
        self._json({"ok": False, "error": msg}, code)

    def _query(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return b""
        if n > MAX_BODY:
            raise ValueError("文件太大（上限 %d MB）" % (MAX_BODY // 1024 // 1024))
        return self.rfile.read(n)

    # ---- 鉴权 ----
    def _token_ok(self):
        """返回 True 表示当前请求带对了 token。未启用鉴权时永远返回 True。"""
        if not TOKEN:
            return True
        q = (self._query().get("token") or [""])[0].strip()
        h = (self.headers.get("X-Token") or "").strip()
        candidate = q or h
        if not candidate:
            return False
        # hmac.compare_digest 长度敏感；类型不对（比如 None / 数字）也直接拒
        if not isinstance(candidate, str):
            return False
        return hmac.compare_digest(candidate, TOKEN)

    def _require_token(self):
        """鉴权失败直接返回 401 响应；返回 True 表示已通过或未启用。"""
        if self._token_ok():
            return True
        body = json.dumps({"ok": False, "error": "未授权"}, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(401)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("WWW-Authenticate", 'Token realm="reader"')
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass
        return False

    # ---- GET ----
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path in ("/", "/index.html", "/reader.html"):
                return self._page()
            # 免鉴权的小路径：/api/ping 给保活脚本/健康检查用（只回一句"活着"，不吐任何数据）；
            # favicon 也无所谓。放在鉴权之前，否则保活脚本会拿到 401 误判服务没起。
            if path == "/api/ping":
                tc = load_tts_config()
                return self._json({"ok": True, "service": "reader-mate", "version": 1,
                                   "built": mtime_tag(HTML_FILE),
                                   "core": mtime_tag(os.path.abspath(__file__)),
                                   # 前端据此决定"用后端嗓子还是内置嗓子"（没配就是 false）
                                   "tts": bool(tc["provider"] and tc["key"]),
                                   "tts_provider": tc["provider"] or ""})
            if path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            # 其余 GET：鉴权（GET / 不要求，方便手机第一次打开）
            if not self._require_token():
                return
            if path == "/api/books":
                return self._api_books()
            if path == "/api/book":
                return self._api_book()
            if path == "/api/progress":
                return self._json({"ok": True, "progress": load_progress()})
            if path == "/api/notes":
                return self._api_notes()
            if path == "/api/notes/export":
                return self._api_notes_export()
            if path == "/api/tts":
                return self._api_tts()
            return self._err("没有这个路径", 404)
        except Exception as e:
            return self._err("服务端出错：%s" % e, 500)

    # ---- 朗读（TTS，可选功能）----
    def _api_tts(self):
        """GET /api/tts?text=&voice=&speed=  → 音频流。

        没配置朗读服务时回 501，前端据此回落到浏览器内置语音合成。
        注意：<audio> 标签带不了 X-Token 头，所以它只能靠 ?token= 过鉴权——
        _token_ok() 本来就认这个查询参数。
        """
        if not tts_available():
            return self._err("这个后端没配置朗读服务（可选功能）", 501)
        q = self._query()
        text = (q.get("text") or [""])[0].strip()
        if not text:
            return self._err("text 不能为空")
        if len(text) > TTS_TEXT_MAX:
            text = text[:TTS_TEXT_MAX]
        voice = (q.get("voice") or [""])[0].strip()
        try:
            speed = float((q.get("speed") or ["1"])[0])
        except (TypeError, ValueError):
            speed = 1.0
        speed = max(0.5, min(2.0, speed))
        try:
            data, ctype = tts_synthesize(text, voice, speed)
        except TTSError as e:
            return self._err("朗读服务出错：%s" % e, 502)
        except Exception as e:
            return self._err("朗读服务连不上：%s" % e, 502)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _page(self):
        try:
            with open(HTML_FILE, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            return self._err("找不到页面文件 web/reader.html", 500)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _api_books(self):
        lib = scan_books()
        pr = load_progress()
        books = []
        for name, b in lib.items():
            p = (pr.get("books") or {}).get(name) or {}
            b = dict(b)
            b["offset"] = p.get("offset", 0)
            b["percent"] = p.get("percent", 0)
            b["chapter"] = p.get("chapter", "")
            b["read_at"] = p.get("updated", "")
            books.append(b)
        books.sort(key=lambda x: (x.get("read_at") or "", x.get("name")))
        books.reverse()
        return self._json({"ok": True, "books": books, "dir": BOOKS_DIR,
                           "updated": lib.get("updated")})

    def _api_book(self):
        name = safe_name((self._query().get("name") or [""])[0])
        if not name:
            return self._err("书名不合法")
        p = book_path(name)
        if not os.path.isfile(p):
            return self._err("书库里没有这本书：%s" % name, 404)
        text = read_text(p)
        pr = load_progress()
        prog = (pr.get("books") or {}).get(name) or {}
        return self._json({"ok": True, "name": name, "text": text,
                           "chars": len(text), "progress": prog})

    # ---- POST ----
    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if not self._require_token():
                return
            if path == "/api/notes/highlight":
                return self._api_notes_highlight_delete()
            return self._err("没有这个路径", 404)
        except Exception as e:
            return self._err("服务端出错：%s" % e, 500)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            # 所有 POST 接口都在鉴权后面
            if not self._require_token():
                return
            if path == "/api/import":
                return self._api_import()
            if path == "/api/progress":
                return self._api_progress()
            if path == "/api/delete":
                return self._api_delete()
            if path == "/api/notes/highlight":
                return self._api_notes_highlight()
            if path == "/api/notes/comment":
                return self._api_notes_comment()
            return self._err("没有这个路径", 404)
        except Exception as e:
            return self._err("服务端出错：%s" % e, 500)

    def _api_import(self):
        name = safe_name((self._query().get("name") or [""])[0])
        if not name:
            return self._err("书名不合法")
        raw = self._body()
        if not raw:
            return self._err("文件是空的")
        text = norm_text(decode_bytes(raw))
        with _lock:
            existed = os.path.isfile(book_path(name))
            atomic_write(book_path(name), text)
            lib = load_library()
            lib.get("books", {}).pop(name, None)  # 让下次扫描重算字符数
            dump_json(LIBRARY_FILE, lib)
        return self._json({"ok": True, "name": name, "chars": len(text),
                           "overwrote": existed})

    def _api_delete(self):
        name = safe_name((self._query().get("name") or [""])[0])
        if not name:
            return self._err("书名不合法")
        p = book_path(name)
        if not os.path.isfile(p):
            return self._err("书库里没有这本书", 404)
        with _lock:
            os.remove(p)
            lib = load_library()
            lib.get("books", {}).pop(name, None)
            dump_json(LIBRARY_FILE, lib)
            pr = load_progress()
            pr.get("books", {}).pop(name, None)
            save_progress(pr)
        return self._json({"ok": True, "name": name})

    def _api_progress(self):
        raw = self._body()
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return self._err("进度不是合法 JSON")
        name = safe_name(data.get("name"))
        if not name:
            return self._err("书名不合法")
        try:
            offset = max(0, int(data.get("offset") or 0))
        except Exception:
            offset = 0
        pct = data.get("percent")
        try:
            pct = min(1.0, max(0.0, float(pct)))
        except Exception:
            pct = None
        with _lock:
            pr = load_progress()
            # tts_offset：给将来 TTS「朗读到哪」留的位置；老条目没这个字段也照常读。
            try:
                tts_offset = max(0, int(data.get("tts_offset") or 0))
            except Exception:
                tts_offset = 0
            pr.setdefault("books", {})[name] = {
                "offset": offset,
                "percent": pct if pct is not None else 0,
                "chars": int(data.get("chars") or 0),
                "chapter": str(data.get("chapter") or "")[:80],
                "anchor": str(data.get("anchor") or "")[:80],
                "tts_offset": tts_offset,
                "updated": now_iso(),
            }
            save_progress(pr)
        return self._json({"ok": True, "name": name, "offset": offset})

    # ---- notes ----
    def _api_notes(self):
        """GET /api/notes → 全表索引；GET /api/notes?name=xxx → 单本全部划线。"""
        q = self._query()
        name = safe_name((q.get("name") or [""])[0])
        nt = load_notes()
        if name:
            book = (nt.get("books") or {}).get(name) or {"highlights": []}
            highlights = [_norm_highlight(h) for h in (book.get("highlights") or [])]
            return self._json({"ok": True, "name": name, "highlights": highlights})
        books = {}
        for n, b in (nt.get("books") or {}).items():
            hl = [_norm_highlight(h) for h in (b.get("highlights") or [])]
            books[n] = {"count": len(hl), "highlights": hl}
        return self._json({"ok": True, "books": books})

    def _api_notes_highlight(self):
        """POST /api/notes/highlight —— 新建（不传 id）或更新（传 id）。"""
        raw = self._body()
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return self._err("划线请求不是合法 JSON")
        name = safe_name(data.get("name"))
        if not name:
            return self._err("书名不合法")
        # offset 合法性
        try:
            start = max(0, int(data.get("start") or 0))
        except Exception:
            return self._err("start 不是非负整数")
        try:
            raw_end = int(data.get("end") or 0)
        except Exception:
            return self._err("end 不是非负整数")
        if raw_end < start:
            return self._err("end 必须 >= start")
        end = raw_end
        text = str(data.get("text") or "")
        if not text:
            return self._err("划线原文 text 不能为空")
        if text_weight(text) > HIGHLIGHT_TEXT_MAX:
            return self._err("划线原文太长（上限 %d 分，约合 %d 个汉字）"
                             % (HIGHLIGHT_TEXT_MAX, HIGHLIGHT_TEXT_MAX // 2))
        style = _norm_style(data.get("style") or "line")
        color = str(data.get("color") or "")[:32]
        chapter = str(data.get("chapter") or "")[:CHAPTER_MAX]
        incoming_id = (data.get("id") or "").strip()
        with _lock:
            nt = load_notes()
            book = nt.setdefault("books", {}).setdefault(name, {"highlights": []})
            hl_list = book.setdefault("highlights", [])
            # 先 normalize 一遍旧的（防止历史脏数据让 thread 校验失效）
            hl_list = [_norm_highlight(h) for h in hl_list]
            book["highlights"] = hl_list
            target = None
            if incoming_id:
                for h in hl_list:
                    if h.get("id") == incoming_id:
                        target = h
                        break
            if target is None:
                if incoming_id:
                    return self._err("找不到这条划线：%s" % incoming_id, 404)
                # 新建
                new_h = {
                    "id": _rand_id(),
                    "start": start,
                    "end": end,
                    "text": text[:HIGHLIGHT_TEXT_MAX],
                    "style": style,
                    "color": color,
                    "chapter": chapter,
                    "created": now_iso(),
                    "thread": [],
                }
                hl_list.append(new_h)
                save_notes(nt)
                return self._json({"ok": True, "id": new_h["id"], "created": True})
            # 更新：保留 thread 与 created；其它字段覆盖
            target["start"] = start
            target["end"] = end
            target["text"] = text[:HIGHLIGHT_TEXT_MAX]
            target["style"] = style
            target["color"] = color
            target["chapter"] = chapter
            save_notes(nt)
            return self._json({"ok": True, "id": target["id"], "created": False})

    def _api_notes_comment(self):
        """POST /api/notes/comment —— 往一条划线追加批注。"""
        raw = self._body()
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return self._err("批注请求不是合法 JSON")
        name = safe_name(data.get("name"))
        if not name:
            return self._err("书名不合法")
        hid = (data.get("id") or "").strip()
        if not hid:
            return self._err("划线 id 不能为空")
        who = data.get("who")
        if who not in ("user", "ai"):
            return self._err("who 只能是 user 或 ai")
        text = str(data.get("text") or "")
        if not text:
            return self._err("批注内容不能为空")
        w = text_weight(text)
        if w > COMMENT_WEIGHT_MAX:
            return self._err(
                "批注太长：%d/%d（约合 %d 个汉字，上限 150 个汉字左右）"
                % (w, COMMENT_WEIGHT_MAX, (w + 1) // 2))
        name_ai = (data.get("name_ai") or "").strip() if who == "ai" else ""
        if who == "ai" and name_ai and len(name_ai) > NAME_AI_MAX:
            name_ai = name_ai[:NAME_AI_MAX]
        with _lock:
            nt = load_notes()
            book = (nt.get("books") or {}).get(name)
            if not isinstance(book, dict):
                return self._err("这本书还没划线：%s" % name, 404)
            hl_list = book.get("highlights") or []
            target = None
            for h in hl_list:
                if h.get("id") == hid:
                    target = h
                    break
            if target is None:
                return self._err("找不到这条划线：%s" % hid, 404)
            thread = target.get("thread") or []
            if not isinstance(thread, list):
                thread = []
            if len(thread) >= THREAD_MAX:
                return self._err("这条划线聊满了（最多 6 轮），换个地方继续吧")
            item = {"who": who, "text": text[:COMMENT_CHARS_HARD_MAX], "at": now_iso()}
            if who == "ai" and name_ai:
                item["name"] = name_ai
            thread.append(item)
            target["thread"] = thread
            save_notes(nt)
            return self._json({"ok": True, "id": hid, "count": len(thread)})

    def _api_notes_highlight_delete(self):
        """DELETE /api/notes/highlight?name=xxx&id=xxx —— 删一条划线（带 thread）。"""
        q = self._query()
        name = safe_name((q.get("name") or [""])[0])
        if not name:
            return self._err("书名不合法")
        hid = (q.get("id") or [""])[0].strip()
        if not hid:
            return self._err("划线 id 不能为空")
        with _lock:
            nt = load_notes()
            book = (nt.get("books") or {}).get(name)
            if not isinstance(book, dict):
                return self._err("这本书还没划线：%s" % name, 404)
            hl_list = book.get("highlights") or []
            kept = [h for h in hl_list if h.get("id") != hid]
            if len(kept) == len(hl_list):
                return self._err("找不到这条划线：%s" % hid, 404)
            book["highlights"] = kept
            if not kept:
                nt["books"].pop(name, None)
            save_notes(nt)
        return self._json({"ok": True})

    def _api_notes_export(self):
        """GET /api/notes/export?name=xxx —— 摘抄本（text/markdown）。"""
        q = self._query()
        name = safe_name((q.get("name") or [""])[0])
        if not name:
            return self._err("书名不合法")
        nt = load_notes()
        book = (nt.get("books") or {}).get(name) or {}
        hl_list = sorted(
            (_norm_highlight(h) for h in (book.get("highlights") or [])),
            key=lambda x: x.get("start", 0),
        )
        # 标题里把书名里的尖括号/方括号转义一下，免得 markdown 走形
        safe_title = name.replace("|", "｜")
        lines = [
            "# 《%s》摘抄本" % safe_title,
            "",
            "> 导出时间：%s　｜　共 %d 条" % (now_iso(), len(hl_list)),
            "",
        ]
        if not hl_list:
            lines.append("_（这本书还没有划线）_")
            lines.append("")
        for i, h in enumerate(hl_list, 1):
            chap = (h.get("chapter") or "").strip()
            if chap:
                lines.append("## %d. %s" % (i, chap))
            else:
                lines.append("## %d." % i)
            quote = (h.get("text") or "").strip()
            if quote:
                # 原文里有换行就整段塞引用块
                if "\n" in quote:
                    lines.append("")
                    lines.append("> " + quote.replace("\n", "\n> "))
                else:
                    lines.append("")
                    lines.append("> " + quote)
            lines.append("")
            for c in (h.get("thread") or []):
                who = c.get("who")
                msg = (c.get("text") or "").replace("\n", " ")
                if who == "user":
                    lines.append("- 你：%s" % msg)
                else:
                    label = c.get("name") or "ai"
                    lines.append("- %s：%s" % (label, msg))
            lines.append("")
        body = ("\n".join(lines)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main():
    os.makedirs(BOOKS_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    for p, d in ((PROGRESS_FILE, {"version": 1, "updated": now_iso(), "books": {}}),
                 (LIBRARY_FILE, {"version": 1, "updated": now_iso(), "books": {}})):
        if not os.path.exists(p):
            dump_json(p, d)
    print("读伴 · v1.0 · 书库 %s" % BOOKS_DIR)
    if TOKEN:
        # 鉴权已开启：访问任何 /api/* 都要带 token。
        # GET / 仍可匿名打开（前端会把 token 存进 localStorage 后调 API）。
        print("=" * 60)
        print("  鉴权已启用 (READER_TOKEN 已设置)")
        print("  访问地址：http://%s:%d/?token=<你的令牌>" % (HOST, PORT))
        print("  首次打开后 token 会存进浏览器 localStorage (rm_token)，")
        print("  之后所有 /api/* 请求自动带 X-Token 请求头。")
        if HOST not in ("127.0.0.1", "localhost", "::1"):
            print("  ⚠ 注意：你当前绑定在 %s，开放地址请使用完整 URL 带 token。" % HOST)
        print("=" * 60)
        # 跳板页：给「外壳 / 快捷方式」用，免得它们把 token 写死在自己的代码里。
        # 换 token 只要重启后端重写这个文件，外层（比如安卓壳）不用重新打包。
        try:
            probe_host = "127.0.0.1" if HOST in ("0.0.0.0", "::", "") else HOST
            entry = os.path.join(DATA_DIR, "带token的入口.html")
            tok = urllib.parse.quote(TOKEN, safe="")
            atomic_write(entry, (
                "<!doctype html>\n<meta charset=\"utf-8\">\n"
                "<meta http-equiv=\"refresh\" content=\"0;url=http://{h}:{p}/?token={t}\">\n"
                "<title>读伴</title>\n<p>正在打开读伴…</p>\n"
                "<p>没自动跳转就点：<a href=\"http://{h}:{p}/?token={t}\">http://{h}:{p}/</a></p>\n"
            ).format(h=probe_host, p=PORT, t=tok))
            print("  跳板页已写好：%s" % entry)
            print("  外壳/快捷方式指向它即可（换 token 只重启后端，不用改外壳代码）")
        except Exception as e:
            print("  ⚠ 跳板页写入失败：%s" % e)
    print("地址 http://%s:%d  （Ctrl+C 停）" % (HOST, PORT))
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


def _run():
    """启动/运行失败时把 traceback 落一份到 data/server.err.log。

    双击 pythonw 是无窗口启动，出错屏幕上什么都看不到，必须留痕。
    """
    try:
        main()
    except Exception:
        import traceback
        tb = traceback.format_exc()
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(os.path.join(DATA_DIR, "server.err.log"), "a", encoding="utf-8") as f:
                f.write("\n[%s] 启动/运行失败：\n%s\n" % (now_iso(), tb))
        except Exception:
            pass
        raise


if __name__ == "__main__":
    _run()
