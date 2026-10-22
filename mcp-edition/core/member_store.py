"""Compatible identity registry. One file, one commit for member + credential.

Legacy string entries stay byte-for-byte equal as values. $members is deliberately
outside the identity ID alphabet. No secrets are returned by profiles().
"""
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
from datetime import datetime, timezone

META = "$members"
LOCK = threading.RLock()


class StoreError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pairs(pairs):
    result = {}
    for k, v in pairs:
        if k in result:
            raise StoreError("身份文件含重复字段")
        result[k] = v
    return result


def validate(doc):
    if not isinstance(doc, dict):
        raise StoreError("身份文件结构异常")
    keys = set()
    for ident, token in doc.items():
        if ident == META:
            continue
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", ident) or not isinstance(token, str) or not token:
            raise StoreError("身份或凭据格式异常")
        if token in keys:
            raise StoreError("凭据绑定不唯一")
        keys.add(token)
    meta = doc.get(META, {"version": 1, "members": {}})
    if not isinstance(meta, dict) or meta.get("version") != 1 or not isinstance(meta.get("members"), dict):
        raise StoreError("成员元数据版本或结构异常")
    cids = set()
    for ident, m in meta["members"].items():
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", ident) or not isinstance(m, dict):
            raise StoreError("成员格式异常")
        if not isinstance(m.get("nickname"), str) or not 1 <= len(m["nickname"]) <= 64 or m.get("kind") not in ("human", "agent", "legacy"):
            raise StoreError("成员属性异常")
        c = m.get("credential")
        if not isinstance(c, dict) or c.get("channel") not in ("legacy", "web", "mcp", "mcp-url") or not isinstance(c.get("id"), str) or not c["id"]:
            raise StoreError("成员凭据属性异常")
        if c["id"] in cids:
            raise StoreError("凭据标识重复")
        cids.add(c["id"])
        if (c.get("revoked_at") is None) != (ident in doc):
            raise StoreError("成员与凭据状态不一致")
        if ident == "user" and (ident not in doc or c["channel"] != "legacy"):
            raise StoreError("user 保留身份状态异常")
    return doc


def read(path):
    try:
        with open(path, "rb") as f:
            raw = f.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise StoreError("身份文件超过大小限制")
        return validate(json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_pairs))
    except (OSError, UnicodeError, ValueError) as exc:
        raise StoreError("身份文件不可用，请核对可信备份；未修改数据") from exc


def profiles(doc):
    members = doc.get(META, {}).get("members", {})
    out = {}
    for ident in sorted(set(doc) - {META} | set(members)):
        m = members.get(ident) or {"nickname": ident, "kind": "legacy", "created_at": None,
            "credential": {"id": "legacy-" + ident, "channel": "legacy", "revoked_at": None}}
        c = m["credential"]
        out[ident] = {"id": ident, "nickname": m["nickname"], "kind": m["kind"],
            "created_at": m.get("created_at"), "credential": {"id": c["id"],
            "channel": c["channel"], "status": "active" if ident in doc else "revoked",
            "revoked_at": c.get("revoked_at")}}
    return out


def authenticate(doc, token, channel=None):
    if not isinstance(token, str) or not token:
        return ""
    for ident, key in doc.items():
        if isinstance(key, str) and hmac.compare_digest(token.encode(), key.encode()):
            c = doc.get(META, {}).get("members", {}).get(ident, {}).get("credential", {})
            allowed = c.get("channel", "legacy")
            if channel and allowed != "legacy" and allowed != channel:
                return ""
            return ident
    return ""


@contextlib.contextmanager
def locked(path):
    with LOCK:
        fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+b") as f:
            if os.name == "nt":
                import msvcrt
                f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _atomic(path, raw):
    fd, tmp = tempfile.mkstemp(prefix=".members-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw); f.flush(); os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)  # the sole commit point; no fallible work after it
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def transact(path, operation):
    """Full read/modify/backup/commit under process lock; exceptions never commit."""
    with locked(path):
        with open(path, "rb") as stream:
            original = stream.read(4 * 1024 * 1024 + 1)
        if len(original) > 4 * 1024 * 1024:
            raise StoreError("身份文件超过大小限制")
        try:
            doc = validate(json.loads(original.decode("utf-8-sig"), object_pairs_hook=_pairs))
        except (ValueError, UnicodeError) as exc:
            raise StoreError("身份文件损坏，未修改数据") from exc
        result = operation(doc)
        validate(doc)
        raw = (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if len(raw) > 4 * 1024 * 1024:
            raise StoreError("身份文件超过大小限制")
        backups = os.path.join(os.path.dirname(path), ".token-backups")
        os.makedirs(backups, mode=0o700, exist_ok=True)
        backup = os.path.join(backups, "identities-" + hashlib.sha256(original).hexdigest() + ".json")
        if not os.path.exists(backup):
            _atomic(backup, original)
        elif open(backup, "rb").read() != original:
            raise StoreError("身份备份校验失败")
        if open(path, "rb").read() != original:
            raise StoreError("身份文件被外部修改，请刷新后重试")
        _atomic(path, raw)
        return result


def adopt(doc):
    """Record legacy provenance without inventing type or creation date."""
    rows = profiles(doc)
    members = doc.setdefault(META, {"version": 1, "members": {}})["members"]
    for ident, row in rows.items():
        if ident not in members:
            c = dict(row["credential"]); c.pop("status")
            members[ident] = {"nickname": ident, "kind": "legacy", "created_at": None, "credential": c}
    return members


def create(path, nickname, kind, channel, ident=None):
    if not isinstance(nickname, str) or not 1 <= len(nickname.strip()) <= 64 or any(ord(c) < 32 for c in nickname):
        raise ValueError("昵称需为 1–64 字，不能含控制字符")
    if (kind, channel) not in (("human", "web"), ("agent", "mcp"), ("agent", "mcp-url"), ("legacy", "legacy")):
        raise ValueError("成员类型与接入方式不匹配")
    ident = ident or "member_" + secrets.token_hex(10)
    if ident == "user" or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", ident):
        raise ValueError("身份 ID 无效或保留")
    def change(doc):
        members = adopt(doc)
        if ident in doc or ident in members:
            raise FileExistsError("身份 ID 已存在或已保留，不能重新分配")
        token = secrets.token_urlsafe(32)
        stamp = now()
        members[ident] = {"nickname": nickname.strip(), "kind": kind, "created_at": stamp,
            "credential": {"id": "cred_" + secrets.token_hex(12), "channel": channel,
                           "created_at": stamp, "revoked_at": None}}
        doc[ident] = token
        return {"member": profiles(doc)[ident], "token": token}
    return transact(path, change)


def revoke(path, ident, credential_id=None):
    if ident == "user":
        raise FileExistsError("user 是保留的系统身份，不能撤销")
    def change(doc):
        members = adopt(doc)
        if ident not in members:
            raise KeyError("没有这个成员")
        c = members[ident]["credential"]
        if credential_id and c["id"] != credential_id:
            raise FileExistsError("凭据已变化，请刷新列表")
        if c.get("revoked_at") is None:
            c["revoked_at"] = now()
            del doc[ident]
        return profiles(doc)[ident]
    return transact(path, change)
