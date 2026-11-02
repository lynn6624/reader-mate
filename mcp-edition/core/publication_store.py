"""Bounded, recoverable publication of the existing flat ReaderMate files.

The durable COMMIT marker is the decision point. Before it, recovery discards
preparation; after it, recovery only rolls forward. Every cooperating data
request must use barrier(); recover() must precede initialization/listen.
Human rollback is a NEW commit assembled from CURRENT data by the caller.
This module never restores an old global JSON file as a rollback operation.

POSIX requests share flock SH; commits use EX. Windows uses exclusive byte
locks for both modes. Reentry is thread-local, including across Store instances;
a held shared barrier can NEVER be upgraded to exclusive.
"""

import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import threading
from datetime import datetime, timezone


MAX_HISTORY_BYTES = 256 * 1024 * 1024
MAX_HISTORY_PER_BOOK = 3
MAX_REPORT_BYTES = 256 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_SCAN_FILES = 20000
_ID = re.compile(r"[A-Za-z0-9_-]{8,80}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_KEY = re.compile(r"[0-9a-f]{64}\Z")
_REGISTRY = {}
_REGISTRY_LOCK = threading.Lock()


class PublicationError(ValueError):
    status = 503


class ConflictError(PublicationError):
    status = 409


class ValidationError(PublicationError):
    status = 400


def _check(value, message, error=PublicationError):
    if not value:
        raise error(message)


def _encoded(value):
    try:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValidationError("Publication metadata is not finite UTF-8 JSON") from exc


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _check(key not in result, "Duplicate publication metadata key")
        result[key] = value
    return result


def _constant(_value):
    raise PublicationError("Nonfinite publication metadata")


def _safe(path, missing=True):
    """No symlinks/junctions, including ancestors; no special/hardlinked files."""
    path = Path(path)
    for item in [*reversed(path.parents), path]:
        try:
            info = item.lstat()
        except FileNotFoundError:
            _check(missing, "Required publication path is missing")
            continue
        _check(not stat.S_ISLNK(info.st_mode)
               and not getattr(item, "is_junction", lambda: False)(), "Linked publication path refused")
        _check(stat.S_ISDIR(info.st_mode) or (stat.S_ISREG(info.st_mode) and info.st_nlink == 1),
               "Special or hardlinked publication path refused")
    return path


def _digest(path):
    path = _safe(path)
    if not path.exists():
        return None
    _check(path.is_file(), "Publication target is not a file")
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _sync_dir(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _mkdir(path):
    path = _safe(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    _safe(path, False)


def _atomic(path, *, raw=None, source=None, expected=None):
    path = _safe(path)
    _mkdir(path.parent)
    temporary = path.parent / (".publication-tmp-" + secrets.token_hex(12))
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            if source is not None:
                with _safe(source, False).open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        output.write(chunk)
            else:
                view = memoryview(raw)
                for offset in range(0, len(view), 1024 * 1024):
                    output.write(view[offset:offset + 1024 * 1024])
            output.flush()
            os.fsync(output.fileno())
        if expected is not None:
            _check(_digest(temporary) == expected, "Publication candidate hash mismatch")
        _safe(path)
        os.replace(temporary, path)
        _sync_dir(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json(path, limit=MAX_MANIFEST_BYTES):
    with _safe(path, False).open("rb") as stream:
        raw = stream.read(limit + 1)
    _check(len(raw) <= limit, "Publication metadata exceeds its size bound")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, UnicodeError) as exc:
        raise PublicationError("Publication metadata is corrupt; retained for inspection") from exc


def _name(name):
    _check(isinstance(name, str) and 1 <= len(name) <= 240 and name.endswith(".txt")
           and name == name.strip() and name not in (".", "..")
           and not any(c in name for c in ("/", "\\", "\0", ":"))
           and not any(ord(c) < 32 for c in name)
           and name != "MCP工具标准调用参考_只读.txt", "Invalid or reserved publication book name", ValidationError)
    try:
        name.encode("utf-8")
    except UnicodeError as exc:
        raise ValidationError("Invalid book name Unicode") from exc
    return hashlib.sha256(name.encode("utf-8")).hexdigest()


def _request(request_id):
    _check(isinstance(request_id, str) and _ID.fullmatch(request_id), "Invalid publication request ID", ValidationError)
    return request_id


class Store:
    def __init__(self, core, hook=None):
        core = Path(core).absolute()
        if os.name == "nt" and not str(core).startswith("\\\\?\\"):
            core = Path("\\\\?\\UNC\\" + str(core)[2:] if str(core).startswith("\\\\")
                        else "\\\\?\\" + str(core))
        self.core = _safe(core, False)
        _check(self.core.is_dir(), "Publication core directory is missing")
        self.root = self.core / "data" / "publications"
        _mkdir(self.root)
        self.hook = hook
        key = os.path.normcase(str(self.root))
        with _REGISTRY_LOCK:
            self._local = _REGISTRY.setdefault(key, threading.local())

    def _event(self, label, index=-1):
        if self.hook is not None:
            self.hook(label, index)

    @contextlib.contextmanager
    def _locked(self, exclusive):
        state = getattr(self._local, "state", None)
        if state is not None:
            _check(not exclusive or state["exclusive"], "Shared publication barrier cannot upgrade to exclusive", ConflictError)
            state["depth"] += 1
            try:
                yield
            finally:
                state["depth"] -= 1
            return
        path = _safe(self.root / ".barrier.lock")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            self._local.state = {"exclusive": exclusive, "depth": 1}
            try:
                yield
            finally:
                self._local.state = None
                if os.name == "nt":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _pending(self):
        for filename in ("active.json", "cleanup-active.json"):
            path = _safe(self.root / filename)
            if path.exists():
                return True
        return False

    @contextlib.contextmanager
    def barrier(self, exclusive=False):
        if getattr(self._local, "state", None) is not None:
            with self._locked(exclusive):
                yield
            return
        if exclusive:
            with self._locked(True):
                self._recover_locked()
                yield
            return
        while True:
            with self._locked(False):
                if not self._pending():
                    yield
                    return
            # Release SH before recovery; an ordinary request never upgrades SH.
            with self._locked(True):
                self._recover_locked()

    def _target(self, name, relative, allow_current=True):
        key = _name(name)
        _check(isinstance(relative, str) and str(PurePosixPath(relative)) == relative
               and not PurePosixPath(relative).is_absolute() and ".." not in PurePosixPath(relative).parts,
               "Noncanonical publication target", ValidationError)
        allowed = {"书库/" + name, "data/notes.json", "data/progress.json", "data/library.json",
                   "data/navigation/" + key + ".json", "data/drafts/" + key + ".json"}
        if allow_current:
            allowed.add("data/publications/" + key + "/current.json")
        _check(relative in allowed, "Publication target is outside the fixed whitelist", ValidationError)
        return _safe(self.core / relative)

    def _bucket(self, name):
        return _safe(self.root / _name(name))

    def _history(self, name, request_id):
        return _safe(self._bucket(name) / _request(request_id))

    def _head(self, name):
        path = self._bucket(name) / "current.json"
        if not _safe(path).exists():
            bucket = self._bucket(name)
            _check(not (bucket / "requests.json").exists() and not (
                bucket.exists() and any(p.is_dir() and _safe(p / "APPLIED").exists() for p in bucket.iterdir())),
                "Publication head is missing for retained publication history")
            return None
        value = _read_json(path)
        _check(isinstance(value, dict) and value.get("schema_version") == 1 and value.get("name") == name
               and isinstance(value.get("publication_id"), str) and _ID.fullmatch(value["publication_id"])
               and type(value.get("generation")) is int and value["generation"] >= 1,
               "Publication head is corrupt")
        _check(value.get("action") in ("publish", "rollback") and isinstance(value.get("created_at"), str)
               and isinstance(value.get("book_revision"), str) and value["book_revision"].startswith("sha256:")
               and _SHA.fullmatch(value["book_revision"][7:])
               and (value.get("previous_publication_id") is None or
                    (isinstance(value.get("previous_publication_id"), str) and _ID.fullmatch(value["previous_publication_id"]))),
               "Publication head metadata is corrupt")
        return value

    def current(self, name):
        with self.barrier():
            return self._head(name)

    def _ledger(self, name):
        path = self._bucket(name) / "requests.json"
        if not _safe(path).exists():
            return {}
        value = _read_json(path, 4 * 1024 * 1024)
        _check(isinstance(value, dict), "Publication request ledger is corrupt")
        for ident, row in value.items():
            _check(_ID.fullmatch(ident) and isinstance(row, dict)
                   and set(row) in ({"fingerprint", "manifest_sha", "generation"},
                                    {"fingerprint", "manifest_sha", "generation", "request"})
                   and isinstance(row.get("fingerprint"), str) and _SHA.fullmatch(row["fingerprint"])
                   and isinstance(row.get("manifest_sha"), str) and _SHA.fullmatch(row["manifest_sha"])
                   and type(row.get("generation")) is int and row["generation"] >= 1,
                   "Publication request ledger is corrupt")
            if "request" in row:
                _check(isinstance(row["request"], dict)
                       and self._request_info(row["request"]) == row["request"],
                       "Publication receipt request metadata is corrupt")
        return value

    @staticmethod
    def _request_info(report):
        info = {"action": report.get("action", "publish"),
                "preview_digest": report.get("preview_digest"),
                "rollback_of": report.get("rollback_of")}
        _check(info["action"] in ("publish", "rollback")
               and (info["preview_digest"] is None or (isinstance(info["preview_digest"], str)
                    and _SHA.fullmatch(info["preview_digest"])))
               and (info["rollback_of"] is None or (isinstance(info["rollback_of"], str)
                    and _ID.fullmatch(info["rollback_of"]))), "Invalid publication request receipt metadata", ValidationError)
        return info

    def request_receipt(self, name, request_id):
        """Durable request lookup, including after exact-file history cleanup.

        A pruned receipt retains action/preview_digest/rollback_of, sufficient
        for the HTTP coordinator to validate a response-lost retry BEFORE it
        rebuilds candidates from a newer draft or newer global reading data.
        """
        _name(name)
        ident = _request(request_id)
        with self.barrier():
            known = self._ledger(name).get(ident)
            if known is None:
                return None
            history = self._history(name, ident)
            if history.exists():
                manifest, manifest_sha = self._manifest(name, ident, known["manifest_sha"])
                _check(self._marker(history, "COMMIT", manifest_sha)
                       and self._marker(history, "APPLIED", manifest_sha), "Request receipt lacks completed history")
                return self._receipt(manifest, manifest_sha, True)
            return {"name": name, "publication_id": ident, "manifest_sha": known["manifest_sha"],
                    "generation": known["generation"], "already_applied": True,
                    "history_pruned": True, "report": dict(known.get("request", {}))}

    def _bindings(self, name):
        path = self._bucket(name) / "bindings.json"
        if not _safe(path).exists():
            return {}
        value = _read_json(path, 4 * 1024 * 1024)
        _check(isinstance(value, dict) and all(_ID.fullmatch(key) and isinstance(sha, str)
               and _SHA.fullmatch(sha) for key, sha in value.items()), "Publication request bindings are corrupt")
        return value

    def _manifest(self, name, request_id, expected=None):
        path = self._history(name, request_id) / "manifest.json"
        manifest_sha = _digest(path)
        _check(manifest_sha is not None and (expected is None or manifest_sha == expected), "Publication manifest hash mismatch")
        value = _read_json(path)
        _check(isinstance(value, dict) and value.get("schema_version") == 1
               and value.get("name") == name and value.get("request_id") == request_id
               and isinstance(value.get("fingerprint"), str) and _SHA.fullmatch(value["fingerprint"])
               and isinstance(value.get("head"), dict) and value["head"].get("name") == name
               and value["head"].get("publication_id") == request_id
               and type(value["head"].get("generation")) is int and value["head"]["generation"] >= 1
               and value["head"].get("action") in ("publish", "rollback")
               and isinstance(value.get("report"), dict) and isinstance(value.get("created_at"), str),
               "Invalid publication manifest")
        rows = value.get("targets")
        _check(isinstance(rows, list) and 2 <= len(rows) <= 7, "Invalid publication target count")
        seen = set()
        for row in rows:
            _check(isinstance(row, dict) and set(row) == {"target", "old_sha", "new_sha", "old_size", "new_size"},
                   "Invalid publication target description")
            self._target(name, row["target"])
            _check(row["target"] not in seen, "Duplicate publication target")
            seen.add(row["target"])
            for version in ("old", "new"):
                digest, size = row[version + "_sha"], row[version + "_size"]
                _check((digest is None or (isinstance(digest, str) and _SHA.fullmatch(digest)))
                       and type(size) is int and 0 <= size <= MAX_HISTORY_BYTES
                       and (digest is not None or size == 0), "Invalid publication payload description")
        _check("书库/" + name in seen and "data/publications/" + _name(name) + "/current.json" in seen,
               "Book/head missing from publication manifest")
        return value, manifest_sha

    def _marker(self, history, filename, manifest_sha):
        path = _safe(history / filename)
        if not path.exists():
            return False
        _check(_read_json(path) == {"manifest_sha": manifest_sha}, "Publication marker binding mismatch")
        return True

    def _payload(self, history, index, version, row):
        path = history / (version + "-" + str(index) + ".bin")
        if row[version + "_sha"] is None:
            _check(not _safe(path).exists(), "Unexpected absent-version payload")
            return None
        _check(_digest(path) == row[version + "_sha"] and path.stat().st_size == row[version + "_size"],
               "Publication payload is corrupt; retained for inspection")
        return path

    def _receipt(self, manifest, manifest_sha, already=False):
        return {"publication_id": manifest["request_id"], "name": manifest["name"],
                "manifest_sha": manifest_sha, "already_applied": already,
                "generation": manifest["head"]["generation"], "report": manifest["report"]}

    def _record_request(self, manifest, manifest_sha):
        ledger = self._ledger(manifest["name"])
        ident = manifest["request_id"]
        row = {"fingerprint": manifest["fingerprint"], "manifest_sha": manifest_sha,
               "generation": manifest["head"]["generation"], "request": self._request_info(manifest["report"])}
        if ident in ledger:
            _check(all(ledger[ident].get(key) == row[key] for key in ("fingerprint", "manifest_sha", "generation"))
                   and ("request" not in ledger[ident] or ledger[ident]["request"] == row["request"]),
                   "Publication request ledger conflict")
        ledger[ident] = row
        raw = _encoded(ledger)
        _check(len(raw) <= 4 * 1024 * 1024, "Publication request ledger capacity reached")
        _atomic(self._bucket(manifest["name"]) / "requests.json", raw=raw)

    def _clear_active(self):
        _safe(self.root / "active.json", False).unlink()
        _sync_dir(self.root)

    def _roll_forward(self, manifest, manifest_sha):
        name, ident = manifest["name"], manifest["request_id"]
        history = self._history(name, ident)
        rows = manifest["targets"]
        # Validate EVERY target and every candidate before the first replacement.
        for index, row in enumerate(rows):
            self._payload(history, index, "old", row)
            self._payload(history, index, "new", row)
            _check(_digest(self._target(name, row["target"])) in (row["old_sha"], row["new_sha"]),
                   "Publication target changed outside this transaction; refusing overwrite")
        if not self._marker(history, "APPLIED", manifest_sha):
            for index, row in enumerate(rows):
                target = self._target(name, row["target"])
                current = _digest(target)
                if current == row["new_sha"]:
                    continue
                _check(current == row["old_sha"], "Publication target changed during recovery")
                self._event("before_replace", index)
                if row["new_sha"] is None:
                    target.unlink()
                    _sync_dir(target.parent)
                else:
                    _atomic(target, source=self._payload(history, index, "new", row), expected=row["new_sha"])
                self._event("after_replace", index)
            _check(all(_digest(self._target(name, row["target"])) == row["new_sha"] for row in rows),
                   "Publication replacement verification failed")
            self._event("before_applied")
            _atomic(history / "APPLIED", raw=_encoded({"manifest_sha": manifest_sha}))
            self._event("after_applied")
        else:
            _check(all(_digest(self._target(name, row["target"])) == row["new_sha"] for row in rows),
                   "Applied transaction changed before its barrier was released")
        self._record_request(manifest, manifest_sha)
        self._event("after_receipt")
        self._clear_active()
        self._event("after_clear_active")
        return self._receipt(manifest, manifest_sha)

    def _discard(self, history, manifest=None):
        allowed = {"manifest.json"}
        if manifest is not None:
            for index, row in enumerate(manifest["targets"]):
                allowed.update(v + "-" + str(index) + ".bin" for v in ("old", "new") if row[v + "_sha"] is not None)
        else:
            allowed.update(v + "-" + str(index) + ".bin" for v in ("old", "new") for index in range(7))
        for path in list(_safe(history, False).iterdir()):
            _safe(path, False)
            _check(path.is_file() and (path.name in allowed or
                   re.fullmatch(r"\.publication-tmp-[0-9a-f]{24}", path.name)),
                   "Unknown preparation file; refusing cleanup")
        for path in list(history.iterdir()):
            path.unlink()
        history.rmdir()
        _sync_dir(history.parent)

    def _recover_locked(self):
        result = []
        if _safe(self.root / "cleanup-active.json").exists():
            self._finish_cleanup()
        active_path = _safe(self.root / "active.json")
        if not active_path.exists():
            self._discard_orphan_preparations()
            return result
        active = _read_json(active_path)
        _check(isinstance(active, dict) and set(active) == {"name", "request_id", "manifest_sha"}
               and isinstance(active["manifest_sha"], str) and _SHA.fullmatch(active["manifest_sha"]),
               "Invalid active publication pointer")
        name, ident = active["name"], active["request_id"]
        manifest, manifest_sha = self._manifest(name, ident, active["manifest_sha"])
        history = self._history(name, ident)
        if self._marker(history, "COMMIT", manifest_sha):
            result.append(self._roll_forward(manifest, manifest_sha))
        else:
            _check(not _safe(history / "APPLIED").exists(), "Applied publication lacks COMMIT")
            self._clear_active()
            self._discard(history, manifest)
            result.append({"name": name, "publication_id": ident, "discarded_preparation": True})
        return result

    def _discard_orphan_preparations(self):
        # A process can exit between writing candidates and fixing active.json.
        # Such a directory cannot contain a public commit and can be discarded.
        for bucket in self.root.iterdir():
            _safe(bucket, False)
            if not bucket.is_dir():
                continue
            _check(_KEY.fullmatch(bucket.name), "Unknown publication bucket")
            for history in list(bucket.iterdir()):
                _safe(history, False)
                if history.is_dir():
                    _request(history.name)
                    if not (history / "COMMIT").exists() and not (history / "APPLIED").exists():
                        self._discard(history)
                    else:
                        _check((history / "COMMIT").exists() and (history / "APPLIED").exists(),
                               "Unindexed unfinished publication requires inspection; serving is blocked")

    def recover(self):
        with self._locked(True):
            return self._recover_locked()

    def _usage(self):
        total, files = 0, 0
        for current, dirs, names in os.walk(self.root, followlinks=False):
            for name in dirs:
                _safe(Path(current) / name, False)
            for name in names:
                path = _safe(Path(current) / name, False)
                _check(path.is_file(), "Unknown publication storage entry")
                files += 1
                _check(files <= MAX_SCAN_FILES, "Publication history entry capacity reached")
                total += path.stat().st_size
        return total

    def commit(self, name, request_id, replacements, report):
        key, ident = _name(name), _request(request_id)
        _check(isinstance(replacements, dict) and 1 <= len(replacements) <= 6,
               "Expected 1..6 explicit publication replacements", ValidationError)
        for relative, raw in replacements.items():
            self._target(name, relative, allow_current=False)
            _check(raw is None or isinstance(raw, bytes), "Replacement payload must be bytes or None", ValidationError)
        _check(isinstance(replacements.get("书库/" + name), bytes), "The unchanged book name and new TXT bytes are required", ValidationError)
        _check(isinstance(report, dict) and report.get("action", "publish") in ("publish", "rollback"),
               "Invalid publication report/action", ValidationError)
        report_raw = _encoded(report)
        _check(len(report_raw) <= MAX_REPORT_BYTES, "Publication report is too large", ValidationError)
        report = json.loads(report_raw)
        fingerprint = _sha(_encoded({"name": name, "report": report,
                                    "replacements": {k: _sha(v) if v is not None else None for k, v in replacements.items()}}))
        with self.barrier(exclusive=True):
            bindings = self._bindings(name)
            _check(ident not in bindings or bindings[ident] == fingerprint,
                   "Request ID already bound to different publication content", ConflictError)
            ledger = self._ledger(name)
            if ident in ledger:
                known = ledger[ident]
                _check(known["fingerprint"] == fingerprint, "Request ID already used for different publication content", ConflictError)
                history = self._history(name, ident)
                if history.exists():
                    manifest, manifest_sha = self._manifest(name, ident, known["manifest_sha"])
                    return self._receipt(manifest, manifest_sha, True)
                return {"name": name, "publication_id": ident, "manifest_sha": known["manifest_sha"],
                        "generation": known["generation"], "already_applied": True, "history_pruned": True, "report": report}
            bucket = self._bucket(name)
            _mkdir(bucket)
            _check(_digest(self._target(name, "书库/" + name)) is not None,
                   "Cannot publish a missing original book", ValidationError)
            history = self._history(name, ident)
            if history.exists():
                _check(not (history / "COMMIT").exists() and not (history / "APPLIED").exists(),
                       "Unindexed committed history requires recovery")
                self._discard(history)
            head = self._head(name)
            if head is not None:
                _check(max((row["generation"] for row in ledger.values()), default=0) == head["generation"],
                       "Publication head and request ledger disagree")
                _check(head["book_revision"] == "sha256:" + _digest(self._target(name, "书库/" + name)),
                       "Book bytes changed outside its publication head", ConflictError)
            _check(sum(p.is_dir() for p in bucket.iterdir()) < MAX_HISTORY_PER_BOOK,
                   "This book retains three histories; preview and explicitly clean an older one first", ConflictError)
            created = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            new_head = {"schema_version": 1, "name": name, "publication_id": ident,
                        "generation": (head["generation"] if head else 0) + 1,
                        "book_revision": "sha256:" + _sha(replacements["书库/" + name]),
                        "previous_publication_id": head["publication_id"] if head else None,
                        "action": report.get("action", "publish"), "created_at": created}
            future_ledger = dict(ledger)
            future_ledger[ident] = {"fingerprint": fingerprint, "manifest_sha": "0" * 64,
                                    "generation": new_head["generation"], "request": self._request_info(report)}
            ledger_bytes = len(_encoded(future_ledger))
            _check(ledger_bytes <= 4 * 1024 * 1024, "Publication request ledger capacity reached", ConflictError)
            bindings[ident] = fingerprint
            binding_raw = _encoded(bindings)
            _check(len(binding_raw) <= 4 * 1024 * 1024, "Publication request binding capacity reached", ConflictError)
            payloads = dict(replacements)
            payloads["data/publications/" + key + "/current.json"] = _encoded(new_head)
            rows, estimate = [], 16384 + len(report_raw) + ledger_bytes + len(binding_raw)
            for relative, raw in sorted(payloads.items()):
                target = self._target(name, relative)
                old_sha = _digest(target)
                old_size = target.stat().st_size if old_sha is not None else 0
                new_size = len(raw) if raw is not None else 0
                rows.append({"target": relative, "old_sha": old_sha, "new_sha": _sha(raw) if raw is not None else None,
                             "old_size": old_size, "new_size": new_size})
                estimate += old_size + new_size
            _check(self._usage() + estimate <= MAX_HISTORY_BYTES, "Publication history exceeds 256 MiB; explicitly clean old histories first", ConflictError)
            # Keep the request's payload binding even if PREPARED is discarded.
            _atomic(bucket / "bindings.json", raw=binding_raw)
            _mkdir(history)
            for index, row in enumerate(rows):
                target = self._target(name, row["target"])
                if row["old_sha"] is not None:
                    _atomic(history / ("old-" + str(index) + ".bin"), source=target, expected=row["old_sha"])
                if row["new_sha"] is not None:
                    _atomic(history / ("new-" + str(index) + ".bin"), raw=payloads[row["target"]], expected=row["new_sha"])
            manifest = {"schema_version": 1, "name": name, "request_id": ident, "created_at": created,
                        "fingerprint": fingerprint, "head": new_head, "targets": rows, "report": report}
            manifest_raw = _encoded(manifest)
            _check(len(manifest_raw) <= MAX_MANIFEST_BYTES, "Publication manifest too large")
            manifest_sha = _sha(manifest_raw)
            _atomic(history / "manifest.json", raw=manifest_raw)
            self._event("prepared")
            active = {"name": name, "request_id": ident, "manifest_sha": manifest_sha}
            _atomic(self.root / "active.json", raw=_encoded(active))
            self._event("active")
            _check(all(_digest(self._target(name, row["target"])) == row["old_sha"] for row in rows),
                   "Publication inputs changed during preparation", ConflictError)
            self._event("before_commit")
            _atomic(history / "COMMIT", raw=_encoded({"manifest_sha": manifest_sha}))
            self._event("after_commit")
            return self._roll_forward(manifest, manifest_sha)

    def read_manifest(self, name, request_id):
        with self.barrier():
            return self._manifest(name, request_id)[0]

    def _read_payload(self, name, request_id, relative_target, version):
        with self.barrier():
            self._target(name, relative_target)
            manifest, _ = self._manifest(name, request_id)
            for index, row in enumerate(manifest["targets"]):
                if row["target"] == relative_target:
                    path = self._payload(self._history(name, request_id), index, version, row)
                    return path.read_bytes() if path is not None else None
            raise ValidationError("Target was not part of this publication")

    def old_payload(self, name, request_id, relative_target):
        return self._read_payload(name, request_id, relative_target, "old")

    def new_payload(self, name, request_id, relative_target):
        return self._read_payload(name, request_id, relative_target, "new")

    def _list_history(self, name=None):
        buckets = [self._bucket(name)] if name is not None else list(self.root.iterdir())
        rows = []
        for bucket in buckets:
            _safe(bucket)
            if not bucket.is_dir():
                continue
            _check(_KEY.fullmatch(bucket.name), "Unknown publication history directory")
            for history in bucket.iterdir():
                _safe(history, False)
                if not history.is_dir():
                    continue
                _request(history.name)
                if not (history / "manifest.json").exists():
                    continue
                value = _read_json(history / "manifest.json")
                manifest, manifest_sha = self._manifest(value.get("name"), history.name)
                _check(_name(manifest["name"]) == bucket.name, "History book bucket mismatch")
                status = "applied" if self._marker(history, "APPLIED", manifest_sha) else (
                    "committed" if self._marker(history, "COMMIT", manifest_sha) else "prepared")
                size = sum(p.stat().st_size for p in history.iterdir() if _safe(p, False).is_file())
                rows.append({"publication_id": history.name, "name": manifest["name"],
                             "action": manifest["head"]["action"], "created_at": manifest["created_at"],
                             "status": status, "bytes": size, "report": manifest["report"]})
        return sorted(rows, key=lambda row: (row["created_at"], row["publication_id"]), reverse=True)

    def list_history(self, name=None):
        with self.barrier():
            return self._list_history(name)

    def _cleanup_plan(self, name, request_ids):
        _check(isinstance(request_ids, list) and 1 <= len(request_ids) <= MAX_HISTORY_PER_BOOK
               and all(isinstance(ident, str) for ident in request_ids)
               and len(set(request_ids)) == len(request_ids), "Select distinct retained history IDs", ValidationError)
        head = self._head(name)
        rows, total = [], 0
        for ident in request_ids:
            _request(ident)
            _check(head is not None and ident != head["publication_id"], "Current publication cannot be cleaned", ConflictError)
            history = self._history(name, ident)
            _check(history.exists(), "Selected history no longer exists; refresh the history list", ConflictError)
            manifest, manifest_sha = self._manifest(name, ident)
            _check(self._marker(history, "COMMIT", manifest_sha) and self._marker(history, "APPLIED", manifest_sha),
                   "Only applied non-head history can be cleaned", ConflictError)
            names = {"manifest.json", "COMMIT", "APPLIED"}
            for index, row in enumerate(manifest["targets"]):
                for version in ("old", "new"):
                    path = self._payload(history, index, version, row)
                    if path is not None:
                        names.add(path.name)
            _check({p.name for p in history.iterdir()} == names, "Unknown history file blocks cleanup")
            files = []
            for filename in sorted(names):
                path = history / filename
                size = path.stat().st_size
                files.append({"filename": filename, "sha": _digest(path), "bytes": size})
                total += size
            rows.append({"publication_id": ident, "manifest_sha": manifest_sha, "files": files})
        plan = {"schema_version": 1, "name": name, "head": head["publication_id"],
                "head_generation": head["generation"], "histories": rows, "bytes": total}
        plan = dict(plan, digest=_sha(_encoded(plan)))
        future = dict(self._cleanup_receipts(name))
        future[plan["digest"]] = {"request_ids": list(request_ids), "bytes": total}
        _check(len(_encoded(future)) <= 4 * 1024 * 1024, "Cleanup receipt capacity reached", ConflictError)
        return plan

    def _cleanup_receipts(self, name):
        path = self._bucket(name) / "cleanup-receipts.json"
        if not _safe(path).exists():
            return {}
        value = _read_json(path, 4 * 1024 * 1024)
        _check(isinstance(value, dict), "Cleanup receipts are corrupt")
        for digest, row in value.items():
            _check(_SHA.fullmatch(digest) and isinstance(row, dict) and set(row) == {"request_ids", "bytes"}
                   and type(row.get("bytes")) is int and row["bytes"] >= 0
                   and isinstance(row.get("request_ids"), list)
                   and 1 <= len(row["request_ids"]) <= MAX_HISTORY_PER_BOOK
                   and all(isinstance(ident, str) and _ID.fullmatch(ident) for ident in row["request_ids"])
                   and len(set(row["request_ids"])) == len(row["request_ids"]), "Cleanup receipt is corrupt")
        return value

    def preview_cleanup(self, name, request_ids):
        with self.barrier():
            return self._cleanup_plan(name, request_ids)

    def _finish_cleanup(self):
        path = self.root / "cleanup-active.json"
        plan = _read_json(path)
        _check(isinstance(plan, dict) and plan.get("schema_version") == 1, "Invalid cleanup journal")
        name = plan.get("name")
        head = self._head(name)
        _check(head is not None and head["publication_id"] == plan.get("head")
               and head["generation"] == plan.get("head_generation"), "Cleanup head changed; cleanup blocked")
        expected_digest = plan.pop("digest", None)
        _check(expected_digest == _sha(_encoded(plan)), "Cleanup journal digest mismatch")
        histories = plan.get("histories")
        _check(isinstance(histories, list) and 1 <= len(histories) <= MAX_HISTORY_PER_BOOK, "Invalid cleanup histories")
        for row in histories:
            ident = _request(row.get("publication_id"))
            _check(ident != head["publication_id"], "Cleanup journal targets current publication")
            history = self._history(name, ident)
            files = row.get("files")
            _check(isinstance(files, list) and 3 <= len(files) <= 17, "Invalid cleanup file count")
            seen = set()
            for file in files:
                filename = file.get("filename")
                _check(isinstance(filename, str) and (filename in ("manifest.json", "COMMIT", "APPLIED")
                       or re.fullmatch(r"(?:old|new)-[0-6]\.bin", filename)) and filename not in seen
                       and isinstance(file.get("sha"), str) and _SHA.fullmatch(file["sha"]), "Unsafe cleanup file")
                seen.add(filename)
                target = _safe(history / filename)
                _check(not target.exists() or _digest(target) == file["sha"], "Cleanup payload changed")
            if history.exists():
                _check({p.name for p in history.iterdir()} <= seen, "Unknown file in interrupted cleanup")
        # All paths/hashes verified before deleting any exact known file.
        for row in histories:
            history = self._history(name, row["publication_id"])
            if history.exists():
                for file in row["files"]:
                    target = _safe(history / file["filename"])
                    if target.exists():
                        target.unlink()
                history.rmdir()  # Never recursive; unexpected files prevent removal.
                _sync_dir(history.parent)
        receipts = self._cleanup_receipts(name)
        receipt = {"request_ids": [row["publication_id"] for row in histories], "bytes": plan["bytes"]}
        _check(expected_digest not in receipts or receipts[expected_digest] == receipt, "Cleanup receipt conflict")
        receipts[expected_digest] = receipt
        _check(len(_encoded(receipts)) <= 4 * 1024 * 1024, "Cleanup receipt capacity reached")
        _atomic(self._bucket(name) / "cleanup-receipts.json", raw=_encoded(receipts))
        path.unlink()
        _sync_dir(self.root)

    def cleanup(self, name, request_ids, confirm_digest):
        with self.barrier(exclusive=True):
            _name(name)
            _check(isinstance(request_ids, list) and 1 <= len(request_ids) <= MAX_HISTORY_PER_BOOK
                   and all(isinstance(ident, str) and _ID.fullmatch(ident) for ident in request_ids)
                   and len(set(request_ids)) == len(request_ids), "Invalid cleanup history IDs", ValidationError)
            _check(isinstance(confirm_digest, str) and _SHA.fullmatch(confirm_digest),
                   "Invalid cleanup confirmation digest", ConflictError)
            known = self._cleanup_receipts(name).get(confirm_digest)
            if known is not None:
                _check(known["request_ids"] == request_ids, "Cleanup digest belongs to a different selection", ConflictError)
                return {"ok": True, "removed": list(request_ids), "bytes": known["bytes"], "already_cleaned": True}
            plan = self._cleanup_plan(name, request_ids)
            _check(isinstance(confirm_digest, str) and plan["digest"] == confirm_digest,
                   "Cleanup preview changed or was not confirmed", ConflictError)
            _atomic(self.root / "cleanup-active.json", raw=_encoded(plan))
            self._finish_cleanup()
            return {"ok": True, "removed": list(request_ids), "bytes": plan["bytes"], "already_cleaned": False}
