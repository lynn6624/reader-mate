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
# 身份令牌（D1）：{身份: 钥匙}。文件不存在 → 视为空（只有 user 能写），
# 绝不能因为缺文件就报错——缺它就是「还没配」，行为退回到 v1.0。
IDENTITIES_FILE = os.path.join(DATA_DIR, "identities.json")
# 写入审计：data/audit.log（JSONL，每行一条）。**只增不改**——它不防伪，
# 只让「谁、什么时候、以什么身份、写了什么」留痕，事后可查。
# 详见《V2-数据格式与限制》第六节 D1 的已知边界。
AUDIT_FILE = os.path.join(DATA_DIR, "audit.log")
AUDIT_ROTATE_BYTES = 2 * 1024 * 1024  # 2MB → 改名为 .1（覆盖旧的）

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
        "group": pick("READER_TTS_GROUP", "group"),
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
    url = base + "/v1/t2a_v2"
    # MiniMax 的 TTS 按「GroupId」定位账号：不带它，有的账号直接报 401 / token is unusable。
    # 官方文档与两个独立实现（hermes-agent、Open-LLM-VTuber）都把它放在 URL 查询参数上。
    gid = cfg.get("group") or ""
    if gid and "GroupId=" not in url:
        url += ("&" if "?" in url else "?") + "GroupId=" + urllib.parse.quote(gid)
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
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


def _common_hanzi_rate(text: str, enc: str, lead_lo: int, lead_hi: int) -> float:
    """算「常用字命中率」：按 enc 编回去、首字节落在该编码常用区的汉字占比。

    GB2312 一级字（简体常用字）首字节 0xB0–0xD7；Big5 常用字首字节 0xA4–0xC6。
    同一段字节用错编码解出来，得到的多是生僻字/罕用字，命中率会明显偏低。
    """
    hanzi = [c for c in text if "\u4e00" <= c <= "\u9fff"]
    if not hanzi:
        return 0.0
    hit = 0
    for c in hanzi:
        try:
            bs = c.encode(enc)
        except UnicodeEncodeError:
            continue
        if len(bs) == 2 and lead_lo <= bs[0] <= lead_hi:
            hit += 1
    return hit / len(hanzi)


def decode_bytes(b: bytes) -> str:
    """txt 编码探测：utf-8 优先，其余在 gb18030 / big5 之间按「像不像常用字」择优。

    不能只按「解码报不报错」定序：Big5 的字节表能吞下绝大多数 GBK 字节，
    把 big5 排在 gb18030 前面，GBK 老书会被静默读成花屏（不报错、不崩）。
    """
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    cands = []
    for enc, lo, hi in (("gb18030", 0xB0, 0xD7), ("big5", 0xA4, 0xC6)):
        try:
            text = b.decode(enc)
        except UnicodeDecodeError:
            continue
        cands.append((text, _common_hanzi_rate(text, enc, lo, hi)))
    if cands:
        # 国内老书以 GBK 为主：平局归 gb18030，big5 要明显更像常用繁体才改判
        pick = cands[0]
        for cand in cands[1:]:
            if cand[1] > pick[1] + 0.15:
                pick = cand
        return pick[0]
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
    """读 progress.json，懒兼容老格式（books[name] 是单份进度而不是 {身份: 进度}）。

    老文件**不动**——只在内存里规范成新形状，下次保存才升到 {身份: 进度} 形态。
    """
    pr = load_json(PROGRESS_FILE, {})
    if not isinstance(pr, dict):
        pr = {}
    pr.setdefault("version", 1)
    pr.setdefault("updated", now_iso())
    pr.setdefault("books", {})
    books = pr["books"]
    if not isinstance(books, dict):
        pr["books"] = {}
        return pr
    # 内存里把所有书的进度规范成 {身份: 进度}——不动 PROGRESS_FILE
    norm = {}
    for name, d in books.items():
        norm[name] = book_progress(d)
    pr["books"] = norm
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
WHO_MAX = 32
WHO_RE = re.compile(r"[^A-Za-z0-9_-]")


def norm_who(s) -> str:
    """把 who 字段清洗成合法身份字符串。

    规则：只保留 [A-Za-z0-9_-]、长度上限 WHO_MAX；清洗后为空则回落 "user"。
    """
    if s is None:
        return "user"
    cleaned = WHO_RE.sub("", str(s)).strip()[:WHO_MAX]
    return cleaned or "user"


# ---------------------------------------------------------------- 身份令牌（D1）
# 钥匙文件：{身份: 钥匙}。文件不存在 → 空 dict（一切照旧，不报错）。
# 钥匙是**本机信任环**内的字符串，本机任何进程都能读它——D1 不防这个；
# 真正的防线是「不绑 0.0.0.0」（见 §5.3）。本机内别让不受信任的进程能读就行。
def load_identities() -> dict:
    """读 data/identities.json。文件不存在或损坏 → 空 dict。"""
    try:
        if not os.path.exists(IDENTITIES_FILE):
            return {}
        # 用 utf-8-sig 容忍 BOM：PowerShell 的 Out-File -Encoding utf8 会写 BOM，
        # 用户手改也可能在文件头加了 BOM——保持宽容；JSON 体本身不带 BOM 也照样能读
        with open(IDENTITIES_FILE, "r", encoding="utf-8-sig") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return {}
        # 只保留 字符串键:字符串值，其它一律丢（防止 schema 飘）
        return {str(k): str(v) for k, v in d.items()
                if isinstance(k, str) and isinstance(v, str) and v}
    except Exception:
        return {}


# ---------------------------------------------------------------- 写入审计
# 写入审计：data/audit.log = JSONL（一行一条 JSON），UTF-8，**只增不改**。
#
# 设计边界（照规格书第六节，**不防伪**）：
#   - 记的是「谁、什么时候、以什么身份、写了什么」+ 钥匙前 4 位指纹。
#   - **绝不写完整钥匙**，只看前 4 位当指纹（泄露后无法凭此冒充身份）。
#   - 审计失败绝不能影响业务：任何异常一律吞掉，写请求照常返回。
#   - 简单轮转：audit.log > 2MB → 改名为 audit.log.1（覆盖旧的）→ 新建空文件继续。
def _audit_token_hint(token: str) -> str:
    """钥匙指纹：前 4 位 + 省略号；没有则返空串。"""
    t = (token or "").strip()
    if not t:
        return ""
    return t[:4] + "…"


def audit(who: str, token_hint: str, ip: str, act: str, target: str, extra: str = ""):
    """追加一条审计记录。**审计失败一律吞掉，绝不影响业务**。

    调用方在「写盘成功之后」才调；调用前应已确定身份 / 鉴权通过。
    """
    try:
        entry = {
            "at": now_iso(),
            "who": who or "",
            "token_hint": token_hint or "",
            "ip": ip or "",
            "act": act or "",
            "target": target or "",
            "extra": extra or "",
        }
        line = json.dumps(entry, ensure_ascii=False)
        with _lock:
            # 轮转：超过 2MB 就改名 → 新建空文件继续
            try:
                if os.path.exists(AUDIT_FILE) and os.path.getsize(AUDIT_FILE) >= AUDIT_ROTATE_BYTES:
                    rot = AUDIT_FILE + ".1"
                    try:
                        os.remove(rot)
                    except OSError:
                        pass
                    try:
                        os.replace(AUDIT_FILE, rot)
                    except OSError:
                        # rename 失败就清空原文件继续（宁可丢旧数据也不阻塞业务）
                        try:
                            os.remove(AUDIT_FILE)
                        except OSError:
                            pass
            except OSError:
                pass
            # 追加写（用 "a" + utf-8，避免读全文的开销）
            with open(AUDIT_FILE, "a", encoding="utf-8", newline="\n") as f:
                f.write(line + "\n")
    except Exception:
        # **审计失败一律吞掉**——绝不能让审计出错把写业务也带崩。
        pass


def read_audit(limit: int = 100):
    """倒序读最近 N 条审计记录。损坏行静默跳过。

    返回 list[dict]。空文件返 []。
    """
    if limit <= 0:
        return []
    rows = []
    try:
        if not os.path.exists(AUDIT_FILE):
            return []
        # 全文件读、按行解析、倒序挑 N 条——日志量小（2MB 上限轮转），够用。
        # 真要支持海量再去想分页。
        with open(AUDIT_FILE, "r", encoding="utf-8", newline="\n") as f:
            data = f.read()
    except Exception:
        return []
    for line in data.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    rows.reverse()
    return rows[:limit]


def resolve_who(claimed, token: str) -> str:
    """D1 判定：定身份。

    返回值 = 真正应该使用的身份字符串。空串 "" 表示**拒绝**（调用方回 403）。

    规则（照抄规格第六节）：
      1. 带令牌且与某个身份的钥匙 hmac.compare_digest 相等 → 返回**那个身份**（忽略 claimed）
      2. 带令牌但没人匹配 → 返回 ""（拒绝）
      3. 不带令牌 → 用 norm_who(claimed)；**结果必须是 user 才放行**，
         自称 op001 / ai1 之类一律返回 ""（拒绝）
    """
    token = (token or "").strip()
    if token:
        ids = load_identities()
        for who, key in ids.items():
            if isinstance(key, str) and hmac.compare_digest(token, key):
                # 钥匙匹配：服务端说了算，客户端自报的 claimed 一律丢
                return who
        return ""
    # 没带令牌：默认信任本机，只能是 user
    w = norm_who(claimed)
    return w if w == "user" else ""


def book_progress(d) -> dict:
    """把 books[name] 的「老/新」两种形状都规范成 {身份: 进度}。

    老形状：name 直接含 offset/updated 等字段 → 视作 {"user": 那一份}
    新形状：已经是 {身份: 进度} → 过滤掉非字典身份项后原样返回
    这层规范化**只在内存里做**，不动 progress.json 文件本体；
    下一次保存时新版才会以 {身份: 进度} 形态落盘——安全、自然迁移。
    """
    if not isinstance(d, dict):
        return {"user": {}}
    # 老形状识别：有 offset/percent/updated 等"进度字段"在**最外层** → 视为单身份（user）
    flat_keys = {"offset", "percent", "chars", "chapter", "anchor",
                 "tts_offset", "updated"}
    if any(k in d for k in flat_keys):
        return {"user": dict(d)}
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out[norm_who(k)] = v
    return out or {"user": {}}


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
    """给人/给 AI 一眼看的表格版。正本仍是 progress.json。

    同一本书的不同身份各占一行（按 updated 倒序），方便一眼看清「书商读到哪里、001 读到哪里」。
    """
    def cell(s, limit=48):
        s = re.sub(r"\s+", " ", str(s or "")).replace("|", "｜").strip()
        return (s[:limit] + "…") if len(s) > limit else (s or "—")

    # 展平成 (name, who, p, updated) 列表，再整体按 updated 倒序
    flat = []
    for name, by_who in (pr.get("books") or {}).items():
        for who, prog in (by_who or {}).items():
            flat.append((name, who, prog, prog.get("updated") or ""))
    flat.sort(key=lambda t: t[3], reverse=True)

    rows = []
    for name, who, p, _u in flat:
        entry = (lib.get("books") or {}).get(name) or {}
        chars = p.get("chars") or entry.get("chars") or 0
        pct = p.get("percent")
        pct_s = f"{pct * 100:.1f}%" if isinstance(pct, (int, float)) else "—"
        rows.append("| {} | {} | {} | {}/{} 字 | {} | {} | {} |".format(
            cell(name, 40), cell(who, 20), pct_s, p.get("offset", 0), chars,
            cell(p.get("chapter")), cell(p.get("anchor")), p.get("updated") or "—"))

    md = [
        "# 读伴 · 阅读进度",
        "",
        "> 本文件由 `reader_server.py` 自动生成，**勿手改**；正本是同目录 `progress.json`。",
        "> 更新：{}　｜　书库：`书库/`".format(pr.get("updated") or now_iso()),
        "",
        "| 书 | 身份 | 进度 | 字符偏移 | 章节 | 锚文本 | 更新时间 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    md.extend(rows or ["| （还没有阅读记录） | — | — | — | — | — | — |"])
    md.append("")
    atomic_write(MD_FILE, "\n".join(md))


def save_progress(pr: dict):
    """写 progress.json 前再做一遍规范化，保证落盘的就是新形状。"""
    pr["updated"] = now_iso()
    # 把每条 books[name] 收紧成 {身份: 进度}
    if isinstance(pr.get("books"), dict):
        norm = {}
        for name, d in pr["books"].items():
            norm[name] = book_progress(d)
        pr["books"] = norm
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

    def _client_ip(self) -> str:
        """取来源 IP。BaseHTTPRequestHandler 只给 client_address，直接用即可。"""
        try:
            return (self.client_address[0] if self.client_address else "") or ""
        except Exception:
            return ""

    def _who_token(self) -> str:
        """取身份令牌（D1）：优先 X-Who-Token 头，其次 ?who_token= 查询参数。

        之所以要支持查询参数：sendBeacon 带不了自定义请求头（fetch 也不行），
        最后一笔进度只能走 URL（关页前的 sendBeacon）。读端不查这个头。
        """
        h = (self.headers.get("X-Who-Token") or "").strip()
        if h:
            return h
        q = (self._query().get("who_token") or [""])[0].strip()
        return q

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
                pr = load_progress()
                who_raw = (self._query().get("who") or [""])[0]
                if who_raw:
                    who = norm_who(who_raw)
                    out = {}
                    for name, by_who in (pr.get("books") or {}).items():
                        # 旧数据读时规范成 {"user": ...}，所以 by_who.get(who) 永远拿得到 user 那份
                        sub = by_who.get(who)
                        if isinstance(sub, dict):
                            out[name] = sub
                    return self._json({"ok": True, "progress": out, "who": who})
                return self._json({"ok": True, "progress": pr})
            if path == "/api/notes":
                return self._api_notes()
            if path == "/api/notes/export":
                return self._api_notes_export()
            if path == "/api/tts":
                return self._api_tts()
            if path == "/api/audit":
                return self._api_audit()
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

    def _api_audit(self):
        """GET /api/audit?limit=N —— 读审计记录（倒序）。本机读，不要求身份令牌。

        默认 limit=100，上限 1000（防误拉全表）。
        """
        try:
            limit = int((self._query().get("limit") or ["100"])[0])
        except (TypeError, ValueError):
            limit = 100
        if limit < 1:
            limit = 1
        if limit > 1000:
            limit = 1000
        rows = read_audit(limit)
        return self._json({"ok": True, "count": len(rows), "entries": rows})

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
            # load_progress 已把 books[name] 规范成 {身份: 进度}
            by_who = (pr.get("books") or {}).get(name) or {}
            # 默认兜底取 user 那份；它不存在就取「最近更新的身份」
            p = by_who.get("user") if isinstance(by_who.get("user"), dict) else None
            if p is None and by_who:
                p = max(by_who.values(), key=lambda x: x.get("updated") or "")
            p = p or {}
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
        by_who = (pr.get("books") or {}).get(name) or {}
        # 默认返 user；都没有就给空
        prog = by_who.get("user") if isinstance(by_who.get("user"), dict) else {}
        return self._json({"ok": True, "name": name, "text": text,
                           "chars": len(text), "progress": prog,
                           "by_who": by_who})

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
            if path == "/api/notes/comment/edit":
                return self._api_notes_comment_edit()
            if path == "/api/notes/comment/delete":
                return self._api_notes_comment_delete()
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
        # who：D1——带令牌以令牌为准；不带令牌只能是 user（自称 AI 一律拒）
        who = resolve_who(data.get("who"), self._who_token())
        if not who:
            return self._err("身份令牌不对，或者自称 AI 却没带令牌", 403)
        with _lock:
            pr = load_progress()
            # tts_offset：给将来 TTS「朗读到哪」留的位置；老条目没这个字段也照常读。
            try:
                tts_offset = max(0, int(data.get("tts_offset") or 0))
            except Exception:
                tts_offset = 0
            # **绝不覆盖同一本书的其他身份**——这就是要修的那个 bug。
            books = pr.setdefault("books", {})
            by_who = books.setdefault(name, {"user": {}})
            # load_progress 已规范化，这里理论上一定是 {身份: 进度}
            # 保险起见过一遍：万一 setdefault 拿到的是旧形状（不可能），丢给 book_progress 兜底
            if not isinstance(by_who, dict) or any(
                    k in by_who for k in ("offset", "percent", "updated")):
                by_who = book_progress(by_who)
                books[name] = by_who
            by_who[who] = {
                "offset": offset,
                "percent": pct if pct is not None else 0,
                "chars": int(data.get("chars") or 0),
                "chapter": str(data.get("chapter") or "")[:80],
                "anchor": str(data.get("anchor") or "")[:80],
                "tts_offset": tts_offset,
                "updated": now_iso(),
            }
            save_progress(pr)
        # 审计：写盘成功才记，**失败（403）不记**——已由上面 return 提前出。
        audit(who=who, token_hint=_audit_token_hint(self._who_token()),
              ip=self._client_ip(), act="POST /api/progress",
              target=name)
        return self._json({"ok": True, "name": name, "who": who, "offset": offset})

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
        # who：D1——划线本身不存 who，但需要确认是哪个身份在写
        who = resolve_who(data.get("who"), self._who_token())
        if not who:
            return self._err("身份令牌不对，或者自称 AI 却没带令牌", 403)
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
                audit_extra = "created"
                audit_target_id = new_h["id"]
            else:
                # 更新：保留 thread 与 created；其它字段覆盖
                target["start"] = start
                target["end"] = end
                target["text"] = text[:HIGHLIGHT_TEXT_MAX]
                target["style"] = style
                target["color"] = color
                target["chapter"] = chapter
                save_notes(nt)
                audit_extra = "updated"
                audit_target_id = target["id"]
        # 审计放外面：audit() 内部也要 _lock，**同一线程重入会死锁**。
        audit(who=who, token_hint=_audit_token_hint(self._who_token()),
              ip=self._client_ip(), act="POST /api/notes/highlight",
              target="%s#%s" % (name, audit_target_id),
              extra=audit_extra)
        return self._json({"ok": True, "id": audit_target_id,
                            "created": (audit_extra == "created")})

    def _api_notes_comment(self):
        """POST /api/notes/comment —— 往一条划线追加批注。

        D1：身份令牌决定**真身**，thread 项里 who 仍按 user / ai 写，name 用真身。
        校验矩阵（token 真身 × claimed who）：
          - 真身=user, claimed=user → 允许；thread 项 {who:user}
          - 真身=user, claimed=ai   → 403（你持 user 令牌却自称 AI）
          - 真身=AI身份, claimed=user → 403（持 AI 令牌却以 user 名义写——令牌语义=谁来写就是谁）
          - 真身=AI身份, claimed=ai  → 允许；thread 项 {who:ai, name:真身}
          - 不带 token → 真身=user；只允许 claimed=user；否则 403
        """
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
        claimed_who = data.get("who")
        if claimed_who not in ("user", "ai"):
            return self._err("who 只能是 user 或 ai")
        true_who = resolve_who(claimed_who, self._who_token())
        if not true_who:
            return self._err("身份令牌不对，或者自称 AI 却没带令牌", 403)
        # 持 user 令牌却以 ai 名义写
        if true_who == "user" and claimed_who == "ai":
            return self._err("身份令牌不对，或者自称 AI 却没带令牌", 403)
        # 持 AI 令牌却以 user 名义写
        if true_who != "user" and claimed_who == "user":
            return self._err("身份令牌不对，或者自称 AI 却没带令牌", 403)
        text = str(data.get("text") or "")
        if not text:
            return self._err("批注内容不能为空")
        w = text_weight(text)
        if w > COMMENT_WEIGHT_MAX:
            return self._err(
                "批注太长：%d/%d（约合 %d 个汉字，上限 150 个汉字左右）"
                % (w, COMMENT_WEIGHT_MAX, (w + 1) // 2))
        # thread 项里 who 字段写 user / ai（数据 schema 限定），name 字段写真身
        # claimed_name_ai：客户端给的 ai 名；与 token 真身不一致就拒（防止冒名）
        name_ai = (data.get("name_ai") or "").strip() if claimed_who == "ai" else ""
        if claimed_who == "ai" and name_ai and len(name_ai) > NAME_AI_MAX:
            name_ai = name_ai[:NAME_AI_MAX]
        if claimed_who == "ai" and name_ai and name_ai != true_who:
            # 客户端给的 ai 名跟 token 真身对不上：拒绝
            return self._err("身份令牌不对，或者自称 AI 却没带令牌", 403)
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
            # thread 项的 who 字段照规范只用 user / ai；具体身份（op001/ai1…）写进 name
            item = {"who": claimed_who, "text": text[:COMMENT_CHARS_HARD_MAX], "at": now_iso()}
            if claimed_who == "ai":
                item["name"] = true_who
            thread.append(item)
            target["thread"] = thread
            save_notes(nt)
        # 审计：写盘成功才记（403 在前面已 return 拒绝）
        audit(who=true_who, token_hint=_audit_token_hint(self._who_token()),
              ip=self._client_ip(), act="POST /api/notes/comment",
              target="%s#%s" % (name, hid),
              extra="index=%d" % (len(thread) - 1))
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

    def _api_notes_comment_edit(self):
        """POST /api/notes/comment/edit —— 改 thread 里某条批注的文本。

        body: {name, id, index, text}
        校验沿用现有规则：text 非空、按权重 ≤ COMMENT_WEIGHT_MAX（300）。
        保留原 who / name / at —— 改的是话本身，不是说话人。
        """
        raw = self._body()
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return self._err("编辑批注请求不是合法 JSON")
        name = safe_name(data.get("name"))
        if not name:
            return self._err("书名不合法")
        hid = (data.get("id") or "").strip()
        if not hid:
            return self._err("划线 id 不能为空")
        try:
            index = int(data.get("index"))
        except Exception:
            return self._err("index 必须是整数")
        # D1：编辑也要通过身份令牌——避免被任何进程冒充他人改话。
        # 编辑本身不存 who，但需要确认写权限；不带令牌 → 走默认 user 路径
        # （私有工具本机信任；body 里带个 who 字段方便兼容原接口语义）
        who = resolve_who(data.get("who"), self._who_token())
        if not who:
            return self._err("身份令牌不对，或者自称 AI 却没带令牌", 403)
        text = str(data.get("text") or "")
        if not text:
            return self._err("批注内容不能为空")
        w = text_weight(text)
        if w > COMMENT_WEIGHT_MAX:
            return self._err(
                "批注太长：%d/%d（约合 %d 个汉字，上限 150 个汉字左右）"
                % (w, COMMENT_WEIGHT_MAX, (w + 1) // 2))
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
            if index < 0 or index >= len(thread):
                return self._err("index 越界：%d（当前 %d 条）" % (index, len(thread)))
            item = thread[index]
            item["text"] = text[:COMMENT_CHARS_HARD_MAX]
            # 不动 who / name / at —— 改的是话本身
            save_notes(nt)
        # 审计：写盘成功才记（403 已在前面 return 拒绝）
        audit(who=who, token_hint=_audit_token_hint(self._who_token()),
              ip=self._client_ip(), act="POST /api/notes/comment/edit",
              target="%s#%s" % (name, hid),
              extra="index=%d" % index)
        return self._json({"ok": True, "id": hid, "index": index, "count": len(thread)})

    def _api_notes_comment_delete(self):
        """POST /api/notes/comment/delete —— 删 thread 里某条批注。

        body: {name, id, index}
        返回 {"ok": true, "count": <删除后 thread 长度>}。
        """
        raw = self._body()
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return self._err("删除批注请求不是合法 JSON")
        name = safe_name(data.get("name"))
        if not name:
            return self._err("书名不合法")
        hid = (data.get("id") or "").strip()
        if not hid:
            return self._err("划线 id 不能为空")
        try:
            index = int(data.get("index"))
        except Exception:
            return self._err("index 必须是整数")
        # D1：删除也走身份令牌——避免任何进程冒充他人删话。
        who = resolve_who(data.get("who"), self._who_token())
        if not who:
            return self._err("身份令牌不对，或者自称 AI 却没带令牌", 403)
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
            if index < 0 or index >= len(thread):
                return self._err("index 越界：%d（当前 %d 条）" % (index, len(thread)))
            del thread[index]
            target["thread"] = thread
            save_notes(nt)
        # 审计：写盘成功才记（403 已在前面 return 拒绝）
        audit(who=who, token_hint=_audit_token_hint(self._who_token()),
              ip=self._client_ip(), act="POST /api/notes/comment/delete",
              target="%s#%s" % (name, hid),
              extra="index=%d" % index)
        return self._json({"ok": True, "id": hid, "count": len(thread)})

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
