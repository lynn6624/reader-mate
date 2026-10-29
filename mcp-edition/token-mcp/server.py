#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Member management using the existing administrator password and identity store.
New member metadata and credentials commit together; revocation keeps history.
Legacy endpoints remain compatible. Never log or list credential values.
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
from pathlib import Path
from urllib.parse import urlsplit

# Source checkout, published package, and existing /opt layout.
_module = Path(__file__).resolve().parent.parent / "core"
if not _module.is_dir():
    _module = Path(__file__).resolve().parent.parent / "reader-mate" / "core"
sys.path.insert(0, os.environ.get("READER_CORE_DIR", str(_module)))
import member_store
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
WEB_PUBLIC_BASE = os.environ.get("READER_WEB_PUBLIC_BASE", "https://reader.example.com/")
MCP_URL_PUBLIC_BASE = os.environ.get("READER_MCP_URL_PUBLIC_BASE", "")
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
    return member_store.read(IDENTITIES)


def connection_base(channel):
    if channel in ("mcp", "legacy"):
        return common_mcp_url()
    value = WEB_PUBLIC_BASE if channel == "web" else MCP_URL_PUBLIC_BASE
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise ValueError("接入域名未配置或格式无效")
    return value.rstrip("/") + ("/" if channel == "web" else "/mcp/member/")


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
        log("request completed")  # Never log URLs, request bodies, or credentials.

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
            try:
                names = sorted(k for k, v in load_identities().items() if isinstance(v, str))
                store_error = False
            except member_store.StoreError:
                names = []
                store_error = True
            self._send(200, {
                "ok": True,
                "configured": configured,
                "admin_error": configured and not admin_ok,
                "store_error": store_error,
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

        if path in ("/api/identities", "/api/members", "/api/members/list", "/api/members/rename", "/api/credentials/revoke"):
            if not self._password_ok(body):
                return self._send(403, {"ok": False, "error": "管理员口令不正确"})
            try:
                if path == "/api/members/list":
                    return self._send(200, {"ok": True, "members": list(member_store.profiles(load_identities()).values())})
                if path == "/api/members/rename":
                    row = member_store.rename(IDENTITIES, body.get("member_id"), body.get("nickname"),
                                              body.get("expected_nickname"))
                    return self._send(200, {"ok": True, "member": row})
                if path == "/api/credentials/revoke":
                    if not valid_name(body.get("member_id")) or not isinstance(body.get("credential_id"), str) or not body["credential_id"]:
                        raise ValueError("需要成员 ID 和凭据 ID")
                    row = member_store.revoke(IDENTITIES, body["member_id"], body["credential_id"])
                    return self._send(200, {"ok": True, "member": row})
                legacy = path == "/api/identities"
                if legacy and not valid_name(body.get("name")):
                    raise ValueError("身份名只能是 1–32 位的字母/数字/_/-")
                kind = "legacy" if legacy else body.get("kind")
                channel = "legacy" if legacy else body.get("channel")
                if channel not in ("legacy", "web", "mcp", "mcp-url"):
                    raise ValueError("接入方式无效")
                # Validate complete connection instructions BEFORE the sole storage commit.
                try:
                    base = connection_base(channel)
                except (OSError, ValueError):
                    return self._send(503, {"ok": False, "error": "接入地址暂不可用，成员未创建"})
                result = member_store.create(IDENTITIES, body.get("name") if legacy else body.get("nickname"),
                    kind, channel, body.get("name") if legacy else None)
                url = base + result["token"] if channel == "mcp-url" else base
                instructions = ({"web": "打开网页，在门禁处输入此令牌。登录后使用此成员的独立进度。",
                    "mcp": "分别填写 MCP URL 和 Bearer 令牌；先调用 get_calling_guide。",
                    "legacy": "分别填写 MCP URL 和 Bearer 令牌；旧接入方式继续有效。",
                    "mcp-url": "仅填写完整 MCP URL，不另填令牌；链接本身就是凭据，请私下保存。"})[channel]
                result.update(ok=True, name=result["member"]["id"], url=url,
                              mcp_url=url if channel != "web" else None, instructions=instructions)
                return self._send(200, result)
            except FileExistsError as exc:
                return self._send(409, {"ok": False, "error": str(exc)})
            except KeyError:
                return self._send(404, {"ok": False, "error": "成员不存在"})
            except (member_store.StoreError, OSError):
                return self._send(503, {"ok": False, "error": "身份存储不可用，未提交更改，请检查备份"})
            except (ValueError, TypeError):
                error = ("需要有效成员 ID、原显示名和新的 1–64 字显示名，不能含控制字符"
                         if path == "/api/members/rename" else "昵称、成员类型或接入方式无效")
                return self._send(400, {"ok": False, "error": error})

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
        if name == "user":
            self._send(409, {"ok": False, "error": "user 是保留的系统身份，不能删除"})
            return
        try:
            row = member_store.revoke(IDENTITIES, name)
            self._send(200, {"ok": True, "name": name, "member": row})
        except KeyError:
            self._send(404, {"ok": False, "error": "没有这个身份"})
        except FileExistsError as exc:
            self._send(409, {"ok": False, "error": str(exc)})
        except (member_store.StoreError, OSError):
            self._send(503, {"ok": False, "error": "身份存储不可用，未提交更改"})



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
