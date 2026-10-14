#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小书桌身份令牌管理 —— 极简后端

用途：在任何设备上打开一个网页，就能**就地生成一个身份令牌并复制**，
不必再问 001／op001「URL 是什么、key 是什么」然后手动抄到另一台设备。

设计约束（与 002 商定，2026-10-14）：
  - 列表可见（只有身份名，没有令牌值）
  - **创建**要管理员口令
  - **删除**要管理员口令
  - 令牌**只在创建响应里出现一次**，之后永不回显
  - 口令**只存 salt + sha256**，不存明文
  - 口令**不经过任何人**（首次由 002 自己在页面上设置）

安全边界：
  - 只绑 127.0.0.1（外层由 Cloudflare 隧道/Caddy 暴露）
  - 直接读写 reader-mate 的 data/identities.json —— 该文件本来就是 reader 用户所有
  - 每次写入前先备份、原子替换
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------- 路径与常量 ----------

DATA_DIR = os.environ.get("READER_DATA_DIR", "/opt/reader-mate/core/data")
IDENTITIES = os.path.join(DATA_DIR, "identities.json")
ADMIN_FILE = os.path.join(DATA_DIR, ".token-admin.json")
BOOTSTRAP_FILE = os.path.join(DATA_DIR, ".token-bootstrap")
BACKUP_DIR = os.path.join(DATA_DIR, ".token-backups")
MCP_KEY_FILE = os.environ.get("READER_MCP_PATH_KEY_FILE",
                           "/opt/reader-mate/state/mcp-path-key.txt")
MCP_PUBLIC_BASE = os.environ.get("READER_MCP_PUBLIC_BASE", "https://reader.example.com/mcp/")
WRITE_LOCK = threading.RLock()

HOST = os.environ.get("TOKEN_HOST", "127.0.0.1")
PORT = int(os.environ.get("TOKEN_PORT", "8860"))

INDEX_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")

# 身份名允许的字符：字母、数字、下划线、连字符；1..32 位
NAME_MAX = 32
TOKEN_BYTES = 32  # 与现有令牌一致：43 字符的 Base64URL


def log(msg):
    sys.stderr.write("[token-mcp] %s\n" % msg)
    sys.stderr.flush()


# ---------- 口令 ----------


def _read_admin():
    try:
        with open(ADMIN_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("salt"), str) and isinstance(d.get("hash"), str):
            return d
    except FileNotFoundError:
        return None
    except Exception as e:
        log("读 admin 文件失败：%s" % e)
        return None
    return None


def _admin_exists():
    # A malformed/unreadable file must never reopen the public setup endpoint.
    return os.path.lexists(ADMIN_FILE)


def check_bootstrap(code):
    if not isinstance(code, str) or not code:
        return False
    try:
        with open(BOOTSTRAP_FILE, "r", encoding="ascii") as f:
            expected = f.read().strip()
    except OSError:
        return False
    return bool(expected) and hmac.compare_digest(code, expected)


def _hash_password(password, salt_hex):
    salt = bytes.fromhex(salt_hex)
    return hashlib.sha256(salt + password.encode("utf-8")).hexdigest()


def set_password(password):
    salt_hex = secrets.token_hex(16)
    d = {"salt": salt_hex, "hash": _hash_password(password, salt_hex), "created": int(time.time())}
    tmp = ADMIN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, ADMIN_FILE)
    log("已设置管理员口令")
    return True


def check_password(password):
    d = _read_admin()
    if not d:
        return False
    try:
        expect = d["hash"]
        got = _hash_password(password, d["salt"])
        return hmac.compare_digest(expect, got)
    except (KeyError, TypeError, ValueError):
        return False


# ---------- 身份表 ----------


def load_identities():
    """与 reader_server.load_identities 同语义：不存在或损坏 → 空 dict。"""
    try:
        with open(IDENTITIES, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_identities(d, why):
    """先备份、再原子替换。"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if os.path.exists(IDENTITIES):
        shutil.copy2(IDENTITIES, os.path.join(BACKUP_DIR, "identities.json.bak-%s-%s" % (stamp, why)))
    tmp = IDENTITIES + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
        f.write("\n")
    # 与既有文件同样的严格权限
    os.chmod(tmp, 0o600)
    os.replace(tmp, IDENTITIES)
    log("identities.json 已更新（%s）" % why)


def new_token():
    """与现有令牌同格式：Base64URL(32 随机字节) 去掉 = 填充 → 43 字符。"""
    return base64.urlsafe_b64encode(secrets.token_bytes(TOKEN_BYTES)).decode("ascii").rstrip("=")


def valid_name(name):
    return isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name) is not None


def common_mcp_url():
    """Only return the shared MCP entrance after successful admin authentication."""
    mode = os.stat(MCP_KEY_FILE).st_mode
    if os.name != "nt" and stat.S_IMODE(mode) & 0o077:
        raise ValueError("MCP path key permissions are too broad")
    with open(MCP_KEY_FILE, "r", encoding="ascii") as stream:
        key = stream.read().strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{24}", key):
        raise ValueError("Invalid MCP path key")
    return MCP_PUBLIC_BASE + key


# ---------- HTTP ----------


class Handler(BaseHTTPRequestHandler):
    server_version = "token-mcp/0.1"

    def log_message(self, fmt, *args):
        log("%s - %s" % (self.address_string(), fmt % args))

    # --- 工具 ---

    def _send(self, code, payload, ctype="application/json; charset=utf-8"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 64 * 1024:
                return None
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return None

    def _password_ok(self, body):
        if not isinstance(body, dict):
            return False
        pw = body.get("password")
        return isinstance(pw, str) and pw != "" and check_password(pw)

    # --- 路由 ---

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            try:
                with open(INDEX_HTML, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(500, {"ok": False, "error": "index.html 缺失"})
            return
        if path == "/api/status":
            configured = _admin_exists()
            admin_ok = _read_admin() is not None
            names = sorted(load_identities().keys())
            self._send(200, {
                "ok": True,
                "configured": configured,
                "admin_error": configured and not admin_ok,
                "identities": names,
            })
            return
        self._send(404, {"ok": False, "error": "没有这个端点"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        body = self._json_body()

        if path == "/api/setup":
            if not isinstance(body, dict) or not isinstance(body.get("password"), str) or len(body["password"]) < 8:
                self._send(400, {"ok": False, "error": "口令至少 8 位"})
                return
            with WRITE_LOCK:
                if _admin_exists():
                    self._send(409, {"ok": False, "error": "口令已设置过，不能重复初始化"})
                    return
                if not check_bootstrap(body.get("bootstrap")):
                    self._send(403, {"ok": False, "error": "初始化码不正确"})
                    return
                set_password(body["password"])
                try:
                    os.unlink(BOOTSTRAP_FILE)
                except OSError:
                    pass
            self._send(200, {"ok": True})
            return

        if path == "/api/login":
            self._send(200, {"ok": self._password_ok(body)})
            return

        if path == "/api/identities":
            if not _admin_exists():
                self._send(409, {"ok": False, "error": "还没设置管理员口令"})
                return
            if not self._password_ok(body):
                self._send(403, {"ok": False, "error": "口令不对"})
                return
            name = (body or {}).get("name")
            if not valid_name(name):
                self._send(400, {"ok": False, "error": "身份名只能是 1–32 位的字母/数字/_/-"})
                return
            try:
                mcp_url = common_mcp_url()
            except (OSError, ValueError) as exc:
                log("通用 MCP URL 不可用：%s" % type(exc).__name__)
                self._send(503, {"ok": False, "error": "通用 MCP 地址暂不可用，身份未创建"})
                return
            with WRITE_LOCK:
                ids = load_identities()
                if name in ids:
                    self._send(409, {"ok": False, "error": "这个身份已存在；要换令牌请先删除它"})
                    return
                token = new_token()
                ids[name] = token
                save_identities(ids, "add-" + name)
            # ★ 令牌只在这一条响应里出现
            self._send(200, {"ok": True, "name": name, "token": token,
                             "mcp_url": mcp_url,
                             "note": "令牌只显示这一次，请立刻复制到本机客户端的配置里"})
            return

        self._send(404, {"ok": False, "error": "没有这个端点"})

    def do_DELETE(self):
        path = self.path.split("?", 1)[0]
        body = self._json_body()
        prefix = "/api/identities/"
        if not path.startswith(prefix):
            self._send(404, {"ok": False, "error": "没有这个端点"})
            return
        if not _admin_exists():
            self._send(409, {"ok": False, "error": "还没设置管理员口令"})
            return
        if not self._password_ok(body):
            self._send(403, {"ok": False, "error": "口令不对"})
            return
        name = path[len(prefix):]
        with WRITE_LOCK:
            ids = load_identities()
            if name not in ids:
                self._send(404, {"ok": False, "error": "没有这个身份"})
                return
            del ids[name]
            save_identities(ids, "del-" + name)
        self._send(200, {"ok": True, "name": name})
        return


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    configured = _admin_exists()
    log("启动：%s:%d  数据目录=%s  口令已设置=%s" % (HOST, PORT, DATA_DIR, configured))
    if not configured:
        log("⚠️ 尚未设置管理员口令 —— 打开页面后请先设置")
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
