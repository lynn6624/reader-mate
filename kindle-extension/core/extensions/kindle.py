"""Optional Kindle reader extension for ReaderMate; install with the matching core."""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
from bisect import bisect_right
from collections import OrderedDict, deque
from email.utils import formatdate
from html import escape as html_escape
from http.cookies import SimpleCookie

from reader_server import (
    BOOKS_DIR, BOOK_CATEGORIES, CATEGORY_IDS, CLOUD, DATA_DIR, PORT, _lock, book_path, file_revision,
    load_progress, progress_view, read_text, resolve_who, safe_name, scan_books,
)

KINDLE_AUTH_FILE = os.path.join(DATA_DIR, "kindle_auth.json")
KINDLE_PAIR_TTL = 600
KINDLE_SESSION_TTL = 90 * 86400
KINDLE_COOKIE = "rm_kindle_device"
_kindle_attempts = {}
KINDLE_PAGE_CHARS = 440
KINDLE_PAGE_CHOICES = (440, 340, 280)
KINDLE_PAGE_FONTS = {440: 18, 340: 20, 280: 22}
_kindle_index_lock = threading.Lock()
_kindle_indexes = OrderedDict()

def _kindle_store():
    """独立于书籍数据；损坏时拒绝操作，绝不重置现有会话。"""
    if not os.path.exists(KINDLE_AUTH_FILE):
        return {"version": 1, "pairings": {}, "sessions": {}}
    with open(KINDLE_AUTH_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if (not isinstance(data, dict) or data.get("version") != 1 or
            not isinstance(data.get("pairings"), dict) or
            not isinstance(data.get("sessions"), dict)):
        raise ValueError("Kindle 会话文件格式异常")
    return data


def _save_kindle_store(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    temp = KINDLE_AUTH_FILE + "." + secrets.token_hex(8) + ".tmp"
    try:
        with open(temp, "x", encoding="utf-8", newline="\n") as f:
            os.chmod(temp, 0o600)
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, KINDLE_AUTH_FILE)
    finally:
        if os.path.exists(temp):
            os.remove(temp)


def _kindle_prune(data, now):
    for name in ("pairings", "sessions"):
        data[name] = {key: value for key, value in data[name].items()
                      if isinstance(value, dict) and value.get("expires", 0) > now}


def _kindle_digest(value):
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _kindle_create_pairing():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    with _lock:
        data = _kindle_store()
        now = int(time.time())
        _kindle_prune(data, now)
        if len(data["pairings"]) >= 3:
            raise ValueError("已有 3 个待使用链接，请等 10 分钟后再生成")
        code = "".join(secrets.choice(alphabet) for _ in range(10))
        data["pairings"][_kindle_digest(code)] = {"expires": now + KINDLE_PAIR_TTL}
        _save_kindle_store(data)
    return code, now + KINDLE_PAIR_TTL


def _kindle_redeem(code):
    if not re.fullmatch(r"[A-HJ-NP-Z2-9]{10}", code):
        return ""
    with _lock:
        data = _kindle_store()
        now = int(time.time())
        _kindle_prune(data, now)
        if not data["pairings"].pop(_kindle_digest(code), None):
            return ""
        if len(data["sessions"]) >= 12:
            raise ValueError("Kindle 设备已达上限，请先撤销旧设备")
        session_id = secrets.token_hex(8)
        secret = secrets.token_urlsafe(32)
        data["sessions"][session_id] = {
            "digest": _kindle_digest(secret), "reader_id": "user",
            "created": now, "expires": now + KINDLE_SESSION_TTL}
        _save_kindle_store(data)
        return session_id + "." + secret


def _kindle_session(cookie_value):
    if not re.fullmatch(r"[0-9a-f]{16}\.[A-Za-z0-9_-]{40,50}", cookie_value or ""):
        return None
    session_id, secret = cookie_value.split(".", 1)
    try:
        session = _kindle_store()["sessions"].get(session_id)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if (not isinstance(session, dict) or session.get("expires", 0) <= time.time() or
            session.get("reader_id") != "user" or
            not hmac.compare_digest(session.get("digest", ""), _kindle_digest(secret))):
        return None
    return session


def _kindle_index(path):
    """Sparse UTF-16 to file-position map; keeps no complete book in memory."""
    stat = os.stat(path)
    fingerprint = (stat.st_size, stat.st_mtime_ns)
    with _kindle_index_lock:
        cached = _kindle_indexes.get(path)
        if cached and cached["fingerprint"] == fingerprint:
            _kindle_indexes.move_to_end(path)
            return cached
        offsets, cookies = [0], [0]
        units = 0
        # Imported books are UTF-8 TXT. TextIOWrapper's seek cookie also handles
        # a BOM and newline normalization, matching book_snapshot() positions.
        with open(path, "r", encoding="utf-8-sig", newline=None) as stream:
            cookies[0] = stream.tell()
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    break
                units += len(chunk.encode("utf-16-le")) // 2
                offsets.append(units)
                cookies.append(stream.tell())
        revision = file_revision(path)
        after = os.stat(path)
        if (after.st_size, after.st_mtime_ns) != fingerprint:
            raise ValueError("书籍正文正在变化，请重试")
        result = {"fingerprint": fingerprint, "offsets": offsets,
                  "cookies": cookies, "units": units, "revision": revision}
        _kindle_indexes[path] = result
        _kindle_indexes.move_to_end(path)
        while len(_kindle_indexes) > 2:
            _kindle_indexes.popitem(last=False)
        return result


def _kindle_page_text(path, index, start, page_chars=KINDLE_PAGE_CHARS):
    if start < 0 or start > index["units"]:
        raise ValueError("阅读位置超出正文")
    point = bisect_right(index["offsets"], start) - 1
    units = index["offsets"][point]
    with open(path, "r", encoding="utf-8-sig", newline=None) as stream:
        stream.seek(index["cookies"][point])
        while units < start:
            ch = stream.read(1)
            if not ch:
                raise ValueError("阅读位置超出正文")
            units += 2 if ord(ch) > 0xffff else 1
        if units != start:
            raise ValueError("阅读位置落在 emoji 中间")
        chars = []
        while len(chars) < page_chars:
            ch = stream.read(1)
            if not ch:
                break
            chars.append(ch)
            units += 2 if ord(ch) > 0xffff else 1
    return "".join(chars), units


def _kindle_previous(path, index, start, page_chars=KINDLE_PAGE_CHARS):
    if start <= 0:
        return None
    point = bisect_right(index["offsets"], start) - 1
    if point > 0:
        point -= 1
    units = index["offsets"][point]
    recent = deque(maxlen=page_chars)
    with open(path, "r", encoding="utf-8-sig", newline=None) as stream:
        stream.seek(index["cookies"][point])
        while units < start:
            ch = stream.read(1)
            if not ch:
                break
            recent.append(units)
            units += 2 if ord(ch) > 0xffff else 1
    if units != start:
        raise ValueError("阅读位置落在 emoji 中间")
    return recent[0] if recent else 0


class KindleMixin:
    def _kindle_cookie_session(self):
        return _kindle_session(self._kindle_cookie_value())

    def _kindle_cookie_value(self):
        try:
            cookie = SimpleCookie()
            cookie.load(self.headers.get("Cookie", ""))
            return cookie[KINDLE_COOKIE].value if KINDLE_COOKIE in cookie else ""
        except Exception:
            return ""

    def _kindle_cookie_only(self):
        return (not self.headers.get("X-Who-Token") and
                not (self._query().get("who_token") or [""])[0] and
                bool(self._kindle_cookie_session()))

    def _kindle_write_origin_ok(self):
        if not self._kindle_cookie_only():
            return True
        origin = self.headers.get("Origin") or self.headers.get("Referer") or ""
        parsed = urllib.parse.urlparse(origin)
        public_host = os.environ.get("READER_MCP_PUBLIC_HOST", "").strip().lower()
        allowed = {public_host} if CLOUD else {self.headers.get("Host", "").lower()}
        if not CLOUD:
            allowed.update({"127.0.0.1:%d" % PORT, "localhost:%d" % PORT})
        return parsed.scheme in ("https", "http") and parsed.netloc.lower() in allowed

    def _require_kindle_owner(self):
        token = (self.headers.get("X-Who-Token") or "").strip()
        if not token or resolve_who(None, token) != "user":
            self._err("仅主人令牌可管理 Kindle 设备", 403)
            return False
        return True

    def _kindle_page(self, code):
        if not re.fullmatch(r"[A-HJ-NP-Z2-9]{10}", code):
            return self._err("链接无效", 404)
        body = ("<!doctype html><html lang=zh><meta charset=utf-8>"
                "<meta name=viewport content='width=device-width,initial-scale=1'>"
                "<title>小书桌 Kindle 配对</title>"
                "<style>body{font:20px/1.7 serif;max-width:36em;margin:2em auto;padding:0 1em}"
                "button{font:inherit;padding:.6em 1.4em;background:#eee;border:1px solid #777}"
                "</style><h1>小书桌 Kindle 配对</h1>"
                "<p>确认将这台设备作为 002 的阅读设备接入？这不会显示或保存主人令牌。</p>"
                "<form method=post action='/k/" + html_escape(code) + "'>"
                "<button type=submit>确认配对并打开书桌</button></form>"
                "<p>链接十分钟有效，且只能使用一次。</p></html>").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _kindle_pair_post(self, code):
        if not re.fullmatch(r"[A-HJ-NP-Z2-9]{10}", code):
            return self._err("配对链接无效或已使用", 410)
        # 表单不需要正文；不在持久连接上留下未读的攻击者输入。
        if int(self.headers.get("Content-Length") or "0") > 0:
            self.close_connection = True
            return self._err("配对请求不需要正文", 400)
        ip = self._client_ip()
        now = time.time()
        with _lock:
            attempts = [stamp for stamp in _kindle_attempts.get(ip, []) if now - stamp < 600]
            if len(attempts) >= 8:
                return self._err("尝试过多，请稍后重试", 429)
            attempts.append(now)
            _kindle_attempts[ip] = attempts
            if len(_kindle_attempts) > 256:
                _kindle_attempts.clear()
        cookie_value = _kindle_redeem(code)
        if not cookie_value:
            return self._err("配对链接无效或已使用", 410)
        self.send_response(303)
        self.send_header("Location", "/kindle")
        cookie = (KINDLE_COOKIE + "=" + cookie_value +
                  "; Path=/; HttpOnly; SameSite=Lax; Max-Age=" + str(KINDLE_SESSION_TTL) +
                  "; Expires=" + formatdate(time.time() + KINDLE_SESSION_TTL, usegmt=True))
        if CLOUD:
            cookie += "; Secure"
        self.send_header("Set-Cookie", cookie)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _api_kindle_sessions(self):
        if not self._require_kindle_owner():
            return
        data = _kindle_store()
        now = time.time()
        rows = [{"session_id": key, "created": value["created"], "expires": value["expires"]}
                for key, value in data["sessions"].items()
                if isinstance(value, dict) and value.get("expires", 0) > now]
        return self._json({"ok": True, "sessions": rows})

    def _api_kindle_pairing(self):
        if not self._require_kindle_owner():
            return
        base = os.environ.get("READER_KINDLE_PUBLIC_BASE", "").strip().rstrip("/")
        if not base:
            host = os.environ.get("READER_MCP_PUBLIC_HOST", "").strip().lower()
            if CLOUD and not re.fullmatch(r"[a-z0-9.-]+(?::[0-9]{1,5})?", host):
                return self._err("请先配置 READER_MCP_PUBLIC_HOST", 503)
            base = "https://" + host if CLOUD else "http://127.0.0.1:%d" % PORT
        if not re.fullmatch(r"https://[a-zA-Z0-9.-]+(?::[0-9]{1,5})?", base) and not (
                not CLOUD and re.fullmatch(r"http://(?:127\.0\.0\.1|localhost):[0-9]{1,5}", base)):
            return self._err("Kindle 公网地址配置不合法", 503)
        code, expires = _kindle_create_pairing()
        return self._json({"ok": True, "url": base + "/k/" + code, "expires": expires}, 201)

    def _api_kindle_revoke(self, session_id):
        if not self._require_kindle_owner():
            return
        if not re.fullmatch(r"[0-9a-f]{16}", session_id):
            return self._err("设备编号无效", 400)
        with _lock:
            data = _kindle_store()
            if session_id not in data["sessions"]:
                return self._err("设备不存在", 404)
            del data["sessions"][session_id]
            _save_kindle_store(data)
        return self._json({"ok": True})

    def _kindle_html(self, title, content, code=200, font_size=18):
        body = ("<!doctype html><html lang=zh><head><meta charset=utf-8>"
                "<meta name=viewport content='width=device-width,initial-scale=1'>"
                "<title>" + html_escape(title) + " · 小书桌</title>"
                "<style>body{margin:0 auto;max-width:44em;padding:7px 10px 20px;"
                "background:#f5efe3;color:#2f2a24;font:18px/1.4 serif}"
                "h1{font-size:1.14em;margin:0 0 4px}a{color:#704a2d}"
                ".meta{font-size:.78em;color:#665c50;margin:2px 0 5px}"
                ".nav{margin:5px 0 7px;white-space:nowrap}"
                ".nav form{display:inline-block;width:49%;vertical-align:top}"
                ".nav button{display:block;width:98%;min-height:46px;font:inherit;"
                "border:2px solid #8b735b;background:#fffaf0;color:#2f2a24}"
                ".text{white-space:pre-wrap;word-break:break-word;font:" +
                str(font_size) + "px/1.4 serif}"
                ".density{font-size:.77em;margin:3px 0 6px}"
                ".density a{display:inline-block;padding:3px 6px;margin-right:4px;border:1px solid #b8aa95}"
                ".density .on{background:#e8ddc9;color:#2f2a24;font-weight:bold}"
                ".sync-note{padding:5px 7px;border:1px solid #b8aa95;background:#eee4d2;font-size:.78em}"
                ".sync-note form{display:inline;margin:0 0 0 5px}"
                ".sync-note button{padding:4px 7px}"
                ".book{display:block;padding:9px 2px;border-bottom:1px solid #cfc3b1}"
                ".shelf-title{display:block;padding:14px 3px;border-bottom:2px solid #8a7964;text-decoration:none}"
                "input{font:inherit;width:68%;padding:6px}button{font:inherit}"
                "</style></head><body><h1>" + html_escape(title) + "</h1>" + content +
                "</body></html>").encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def _kindle_book_url(self, name, at=None, revision=None, page_chars=None, paused=False):
        query = {"name": name}
        if at is not None:
            query["at"] = str(at)
        if revision:
            query["v"] = revision
        if page_chars is not None:
            query["n"] = str(page_chars)
        if paused:
            query["p"] = "1"
        return "/kindle?" + urllib.parse.urlencode(query)

    def _kindle_form_mac(self, cookie_value, name, target, expected, revision, page_chars, paused):
        secret = cookie_value.split(".", 1)[1]
        payload = "\x00".join((name, str(target), str(expected), revision,
                              str(page_chars), "1" if paused else "0")).encode("utf-8")
        return hmac.new(secret.encode("ascii"), payload, hashlib.sha256).hexdigest()

    def _kindle_turn_form(self, label, name, target, expected, revision, cookie_value, page_chars, paused=False):
        mac = self._kindle_form_mac(cookie_value, name, target, expected, revision, page_chars, paused)
        fields = {"name": name, "target": target, "expected_revision": expected,
                  "book_revision": revision, "page_chars": page_chars,
                  "paused": "1" if paused else "0", "csrf": mac}
        hidden = "".join("<input type=hidden name='" + key + "' value='" +
                         html_escape(str(value), quote=True) + "'>" for key, value in fields.items())
        return ("<form method=post action='/kindle/page'>" + hidden +
                "<button type=submit>" + html_escape(label) + "</button></form>")

    def _kindle_reader(self):
        cookie_value = self._kindle_cookie_value()
        if not _kindle_session(cookie_value):
            return self._kindle_html("需要重新配对", "<p>这台设备尚未配对或会话已撤销。</p>", 401)
        q = self._query()
        try:
            page_chars = int((q.get("n") or [str(KINDLE_PAGE_CHARS)])[0])
        except ValueError:
            page_chars = KINDLE_PAGE_CHARS
        if page_chars not in KINDLE_PAGE_CHOICES:
            page_chars = KINDLE_PAGE_CHARS
        paused = (q.get("p") or [""])[0] == "1"
        raw_name = (q.get("name") or [""])[0]
        if not raw_name:
            search = (q.get("q") or [""])[0].strip()[:60]
            pr = load_progress().get("books") or {}
            books = scan_books()
            names = [n for n in books if not search or search.casefold() in n.casefold()]
            names.sort(key=lambda n: ((pr.get(n) or {}).get("user") or {}).get("updated") or "", reverse=True)
            expanded = set(q.get("open") or []) & CATEGORY_IDS
            closed_search = set(q.get("closed") or []) & CATEGORY_IDS

            def shelf_url(opened, **updates):
                params = {key: values for key, values in q.items() if key.startswith("page_")}
                params.update(q=search, open=sorted(opened), closed=sorted(closed_search))
                params.update(updates)
                return "/kindle?" + html_escape(urllib.parse.urlencode(params, doseq=True), quote=True)

            content = ("<p class=meta>触屏点按书名阅读；打开时接续 002 已保存的位置。</p>"
                       "<form method=get action='/kindle'><input name=q value='" +
                       html_escape(search, quote=True) + "' placeholder='搜索书名'>"
                       "<button type=submit>查找</button></form>")
            for category, label in BOOK_CATEGORIES:
                group = [n for n in names if books[n]["category"] == category]
                if search and not group:
                    continue
                opened = category not in closed_search if search else category in expanded
                heading = ("▾ " if opened else "▸ ") + label + "（" + str(len(group)) + "）"
                toggled = expanded ^ {category}
                toggle_url = shelf_url(toggled, closed=sorted(closed_search ^ {category})) if search else shelf_url(toggled)
                content += "<a class=shelf-title href='" + toggle_url + "'>" + html_escape(heading) + "</a>"
                if not opened:
                    continue
                try:
                    page = max(0, int((q.get("page_" + category) or ["0"])[0]))
                except ValueError:
                    page = 0
                page = min(page, max(0, (len(group) - 1) // 8))
                for name in group[page * 8:(page + 1) * 8]:
                    prog = ((pr.get(name) or {}).get("user") or {})
                    pct = prog.get("percent")
                    title = books[name]["title"]
                    if isinstance(pct, (int, float)):
                        title += " · %.1f%%" % (pct * 100)
                    content += "<a class=book href='" + html_escape(self._kindle_book_url(name), quote=True) + "'>" + html_escape(title) + "</a>"
                if not group:
                    content += "<p>暂无书籍</p>"
                if page:
                    content += "<a href='" + shelf_url(expanded, **{"page_" + category: page - 1}) + "'>← 上一批书籍</a> "
                if (page + 1) * 8 < len(group):
                    content += "<a href='" + shelf_url(expanded, **{"page_" + category: page + 1}) + "'>下一批书籍 →</a>"
            if search and not names:
                content += "<p>没有找到书籍。</p>"
            return self._kindle_html("书库", content)
        name = safe_name(raw_name)
        if name != raw_name or not os.path.isfile(book_path(name)):
            return self._kindle_html("书籍不存在", "<p><a href='/kindle'>返回书库</a></p>", 404)
        path = book_path(name)
        try:
            index = _kindle_index(path)
        except (UnicodeError, ValueError):
            return self._kindle_html("无法读取", "<p>这本书尚未转成 UTF-8 TXT，或正文正在变化。</p><p><a href='/kindle'>返回书库</a></p>", 409)
        record = ((load_progress().get("books") or {}).get(name) or {}).get("user") or {}
        if record.get("book_revision"):
            status = progress_view(record, "user", None, index["revision"])["location_status"]
        elif record and os.path.getsize(path) <= 8 * 1024 * 1024:
            status = progress_view(record, "user", read_text(path), index["revision"])["location_status"]
        else:
            status = "legacy_unverified" if record else "none"
        warning = ""
        if "v" in q and (q.get("v") or [""])[0] != index["revision"]:
            return self._kindle_html("正文已变化", "<p>书籍正文已变化，旧链接不能继续定位。</p><p><a href='" + html_escape(self._kindle_book_url(name), quote=True) + "'>重新打开本书</a></p>", 409)
        if "at" in q:
            try:
                start = int((q.get("at") or [""])[0])
            except ValueError:
                return self._kindle_html("位置无效", "<p><a href='/kindle'>返回书库</a></p>", 400)
        elif status in ("ok", "legacy_anchor_verified"):
            start = int(record.get("offset") or 0)
        else:
            start = 0
            if record:
                warning = "<p class=meta>旧进度与当前正文无法可靠对应，已停在开头，未改动原进度。</p>"
        try:
            page_text, end = _kindle_page_text(path, index, start, page_chars)
            previous = _kindle_previous(path, index, start, page_chars)
        except ValueError:
            return self._kindle_html("位置无效", "<p>该位置无法对应当前正文。</p><p><a href='" + html_escape(self._kindle_book_url(name), quote=True) + "'>重新打开本书</a></p>", 409)
        expected = max(0, int(record.get("revision") or 0))
        nav = "<div class=nav>"
        nav += self._kindle_turn_form("← 上一页", name, previous, expected, index["revision"], cookie_value, page_chars, paused) if previous is not None else "<form><button disabled>已到开头</button></form>"
        if start >= index["units"]:
            nav += "<form><button disabled>已读完</button></form>"
        else:
            nav += self._kindle_turn_form("下一页 →" if end < index["units"] else "读完本书 ✓", name, end, expected, index["revision"], cookie_value, page_chars, paused)
        nav += "</div>"
        density = "<p class=density>阅读字号："
        for count, label in ((440, "常规·440"), (340, "大字·340"), (280, "特大·280")):
            selected = " class=on" if count == page_chars else ""
            url = self._kindle_book_url(name, start, index["revision"], count, paused)
            density += "<a" + selected + " href='" + html_escape(url, quote=True) + "'>" + label + "</a>"
        density += "</p>"
        sync_note = ""
        if paused:
            sync_note = ("<div class=sync-note>本机位置暂未同步；停用其他设备后" +
                         self._kindle_turn_form("同步此处进度", name, start, expected,
                                                index["revision"], cookie_value, page_chars) + "</div>")
        pct = 100 * start / index["units"] if index["units"] else 100
        location_label = "本机位置" if paused else "002 的位置"
        content = ("<p class=meta><a href='/kindle'>书库</a> · %s %.1f%% · 本页点按翻页</p>" % (location_label, pct) +
                   warning + sync_note + nav + density + "<div class=text>" +
                   (html_escape(page_text) if page_text else "本书已读完。") + "</div>" +
                   "<p class=meta><a href='/?full=1&amp;kindle=1'>打开完整阅读页</a></p>")
        return self._kindle_html(os.path.splitext(name)[0], content,
                                 font_size=KINDLE_PAGE_FONTS[page_chars])

    def _kindle_turn(self):
        cookie_value = self._kindle_cookie_value()
        if not _kindle_session(cookie_value):
            return self._kindle_html("需要重新配对", "<p>设备会话已失效。</p>", 401)
        if self.headers.get("Transfer-Encoding") or int(self.headers.get("Content-Length") or 0) > 2048:
            self.close_connection = True
            return self._kindle_html("请求无效", "<p>表单过大。</p>", 400)
        data = urllib.parse.parse_qs(self._body().decode("utf-8"), keep_blank_values=True)
        get = lambda key: (data.get(key) or [""])[0]
        name, target, expected, revision = (get("name"), get("target"),
                                            get("expected_revision"), get("book_revision"))
        paused_value = get("paused")
        try:
            page_chars = int(get("page_chars"))
        except ValueError:
            page_chars = 0
        try:
            valid = (page_chars in KINDLE_PAGE_CHOICES and paused_value in ("0", "1") and
                     hmac.compare_digest(get("csrf"), self._kindle_form_mac(
                         cookie_value, name, target, expected, revision, page_chars,
                         paused_value == "1")))
        except (ValueError, IndexError):
            valid = False
        if not valid or safe_name(name) != name or not os.path.isfile(book_path(name)):
            return self._kindle_html("请求无效", "<p>翻页表单已失效，请重新打开书籍。</p>", 403)
        if paused_value == "1":
            return self._redirect(self._kindle_book_url(name, target, revision, page_chars, True))
        redirect = self._kindle_book_url(name, target, revision, page_chars)
        conflict_redirect = self._kindle_book_url(name, target, revision, page_chars, True)
        return self._api_progress({"name": name, "who": "user", "offset": target,
                                   "expected_revision": expected, "book_revision": revision},
                                  redirect, conflict_redirect)



def decorate_page(body):
    """Insert optional controls into the stable base page template."""
    assets = os.path.dirname(__file__)
    for marker, filename in (
        (b"/* KINDLE_EXTENSION_CSS */", "kindle.css"),
        (b"<!-- KINDLE_EXTENSION_PANEL -->", "kindle-panel.html"),
        (b"/* KINDLE_EXTENSION_INIT */", "kindle-init.js"),
        (b"/* KINDLE_EXTENSION_JS */", "kindle.js"),
    ):
        if body.count(marker) != 1:
            raise RuntimeError("Kindle extension requires matching reader.html marker")
        with open(os.path.join(assets, filename), "rb") as stream:
            body = body.replace(marker, stream.read(), 1)
    return body
