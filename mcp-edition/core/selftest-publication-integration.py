"""Publication HTTP integration: two disposable readers, synthetic data only."""
import concurrent.futures
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parent
OWNER, AGENT = "integration-owner", "integration-agent"
BOOK, OTHER, INSERT = "integration-a.txt", "integration-b.txt", "integration-insert.txt"
TEXT = "第一章 开始\n广告水印。\n前段甲😀乙。\n第二章 正文\n目标批注锚点。\n后段进度锚点。\n结束。\n"
OTHER_TEXT = "另一书第一章\n另一书固定划线。\n另一书新划线。\n另一书阅读位置。\n"


def units(text):
    return len(text.encode("utf-16-le")) // 2


def offset(text, quote):
    return units(text[:text.index(quote)])


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode()


def assert_preserved(previous, current):
    """Ordinary notes saves may add compatibility keys, but may not lose data."""
    if isinstance(previous, dict):
        assert isinstance(current, dict)
        for key, value in previous.items():
            assert key in current, key
            assert_preserved(value, current[key])
    elif isinstance(previous, list):
        assert isinstance(current, list) and len(previous) == len(current)
        for old, new in zip(previous, current):
            assert_preserved(old, new)
    else:
        assert previous == current, (previous, current)


def wait_until(predicate, description, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.025)
    raise AssertionError("Timed out: " + description)


def child(core, port, control, instrument):
    """Only this test wrapper supplies fault/synchronization hooks."""
    sys.path.insert(0, str(core))
    os.environ.update(READER_CLOUD="1", READER_HOST="127.0.0.1", READER_PORT=str(port), READER_TOKEN="")
    import reader_server as reader
    from publication_store import Store
    def hook(label, index):
        armed = control / "armed.json"
        if not instrument or not armed.exists():
            return
        settings = json.loads(armed.read_text())
        if settings["label"] != label:
            return
        armed.unlink()
        (control / "entered").write_text(label)
        if settings["action"] == "fail":
            raise OSError("synthetic pre-COMMIT failure")
        wait_until(lambda: (control / "release").exists(), "release publication hold", 40)
    # Preserve reader.publication_store() and its registry/factory behavior;
    # inject only the transaction event hook at the existing constructor seam.
    reader.PublicationStore = lambda directory: Store(directory, hook=hook)
    original_post, original_json = reader.Handler.do_POST, reader.Handler._json
    def post(handler):
        tag = handler.headers.get("X-Test-Gate", "")
        if tag in ("other-progress", "other-highlight", "slow-member"):
            (control / (tag + ".started")).write_text("received")
        return original_post(handler)
    def reply(handler, value, *args, **kwargs):
        if handler.headers.get("X-Test-Drop-Reply") == "1" and value.get("ok") is True:
            handler.close_connection = True
            return None
        return original_json(handler, value, *args, **kwargs)
    reader.Handler.do_POST, reader.Handler._json = post, reply
    reader.main()


def run():
    artifacts = Path(os.environ.get("PUBLICATION_INTEGRATION_ARTIFACT_DIR", ROOT.parent / "checks" / "integration")).resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="publication-http-") as folder:
        core = Path(folder) / "core"
        core.mkdir()
        for path in ROOT.glob("*.py"):
            if not path.name.startswith("selftest"):
                shutil.copy(path, core / path.name)
        shutil.copytree(ROOT / "web", core / "web", ignore=shutil.ignore_patterns("*.pyc", "__pycache__"))
        shutil.copytree(ROOT / "tools", core / "tools", ignore=shutil.ignore_patterns("*.pyc", "__pycache__"))
        (core / "书库").mkdir()
        (core / "data").mkdir()
        control = Path(folder) / "control"
        control.mkdir()
        raw_book = TEXT.replace("\n", "\r\n").encode()
        for name, raw in ((BOOK, raw_book), (OTHER, OTHER_TEXT.encode()), (INSERT, TEXT.encode()),
                          ("MCP工具标准调用参考_只读.txt", b"Synthetic deployment-owned guide")):
            (core / "书库" / name).write_bytes(raw)
        identities = core / "data/identities.json"
        identities.write_bytes(encode({"user": OWNER, "agent": AGENT}))
        original_identity = identities.read_bytes()
        original_revision = "sha256:" + hashlib.sha256(raw_book).hexdigest()
        old_note = dict(id="history-a", start=offset(TEXT, "目标批注锚点。"),
            end=offset(TEXT, "目标批注锚点。") + units("目标批注锚点。"), text="目标批注锚点。",
            owner="revoked-member", author_id="revoked-member", created="2020-01-01", extra={"preserve": [1, 2]},
            thread=[dict(annotation_id="historical-comment", author_id="retired-agent", who="ai", name="retired-agent",
                         text="历史评论保留", extra={"origin": "legacy"})])
        (core / "data/notes.json").write_bytes(encode({"version": 2, "updated": "initial", "extension": {"keep": True},
            "books": {BOOK: {"highlights": [old_note], "book_extra": [7]}, OTHER: {"highlights": [], "other_extra": 8}}}))
        old_progress = dict(offset=offset(TEXT, "后段进度锚点。"), anchor="后段进度锚点。 结束。 ",
            book_revision=original_revision, revision=0, updated="2020-01-01T00:00:00",
            tts_offset=offset(TEXT, "目标批注锚点。"), custom={"keep": [4]})
        (core / "data/progress.json").write_bytes(encode({"version": 2, "extension": {"keep": [9]},
            "books": {BOOK: {"user": old_progress}, OTHER: {"user": {"offset": 0, "revision": 0, "custom": {"other": True}}}}}))
        hashes = {str(path.relative_to(core)): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in core.rglob("*") if path.is_file() and path.suffix in (".py", ".js", ".html")}
        (artifacts / "source-sha256.json").write_text(json.dumps(hashes, indent=2), encoding="utf-8")
        ports = []
        for _ in range(2):
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                ports.append(listener.getsockname()[1])
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def request(route, data=None, *, server=0, token=OWNER, status=200, headers=None):
            fields = {"Content-Type": "application/json"}
            if token:
                fields["X-Who-Token"] = token
            fields.update(headers or {})
            req = urllib.request.Request("http://127.0.0.1:%d%s" % (ports[server], route),
                data=None if data is None else encode(data), headers=fields)
            try:
                with opener.open(req, timeout=50) as response:
                    actual, value = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                actual, value = error.code, json.load(error)
            if status is not None:
                assert actual == status, (route, actual, value)
            return actual, value
        def query(name, **extra):
            return "?" + urllib.parse.urlencode(dict(name=name, **extra))
        def book(name=BOOK, server=0):
            return request("/api/book" + query(name), server=server)[1]
        def notes(name=BOOK, **extra):
            return request("/api/notes" + query(name, **extra))[1]
        def canonical():
            return {str(path.relative_to(core)): path.read_bytes() for root in (core / "书库", core / "data")
                    for path in root.rglob("*") if path.is_file() and "publications" not in path.parts
                    and path.suffix in (".json", ".txt", ".md")}
        def raw_store(name):
            return json.loads((core / "data" / name).read_text(encoding="utf-8-sig"))
        def start_draft(name=BOOK, quote="广告水印。", replacement=""):
            current = book(name)
            draft = request("/api/draft/start", dict(name=name, book_revision=current["book_revision"]))[1]
            request("/api/draft/edit", dict(name=name, base_revision=current["book_revision"], expected_revision=draft["draft_revision"],
                request_id="draft_integration_" + name.replace(".", "_"), start=offset(current["text"], quote),
                end=offset(current["text"], quote) + units(quote), quote=quote, replacement=replacement))
        def arm(action):
            for filename in ("entered", "release", "other-progress.started", "other-highlight.started"):
                (control / filename).unlink(missing_ok=True)
            (control / "armed.json").write_text(json.dumps({"label": "before_commit", "action": action}))
        handles, processes = [], []
        try:
            for number, port in enumerate(ports):
                log = (artifacts / ("server-%d.log" % number)).open("w", encoding="utf-8")
                handles.append(log)
                processes.append(subprocess.Popen([sys.executable, "-u", "-B", __file__, "--server-child", str(core), str(port), str(control), str(number == 0)], stdout=log, stderr=log))
            for server in range(2):
                def ready():
                    try:
                        return request("/api/ping", server=server)[1]["ok"]
                    except OSError:
                        return False
                wait_until(ready, "reader HTTP server %d" % server)
            # Warm only normal derived library metadata before preservation comparisons.
            request("/api/books")
            other_book = book(OTHER)
            # Obtain a genuine queue/acceptance receipt through the existing API;
            # its digests are produced by revision_proposals, not copied here.
            proposed = request("/api/proposals/edit", dict(name=BOOK, book_revision=original_revision,
                operation="delete", quote="广告水印。", reason="synthetic publication fixture"), token=AGENT)[1]["proposal"]
            proposal_id = proposed["id"]
            reviewed = request("/api/proposals/preview", dict(name=BOOK, id=proposal_id))[1]
            request("/api/proposals/review", dict(name=BOOK, id=proposal_id, action="accept", expected_revision=reviewed["expected_revision"]))
            draft_path = core / "data" / "drafts" / (hashlib.sha256(BOOK.encode()).hexdigest() + ".json")
            valid_draft = draft_path.read_bytes()
            broken = json.loads(valid_draft)
            broken["proposal_receipts"][proposal_id]["decision_digest"] = "0" * 64
            draft_path.write_bytes(encode(broken))
            broken_baseline = canonical()
            request("/api/publication/preview", {"name": BOOK}, status=503)
            request("/api/publication/commit", dict(name=BOOK, preview_digest="not-used", request_id="corrupt_receipt_0001"), status=503)
            assert canonical() == broken_baseline, "Corrupt receipt changed an original file"
            draft_path.write_bytes(valid_draft)
            print("PASS HTTP: corrupt genuine approval receipt blocks preview/commit with 503 and retains all original bytes", flush=True)
            baseline = canonical()
            routes = ("preview", "commit", "rollback-preview", "rollback", "cleanup-preview", "cleanup")
            for token, denied in (("", 401), (AGENT, 403)):
                for route in routes:
                    request("/api/publication/" + route, {"name": BOOK, "who": "user"}, token=token, status=denied)
                request("/api/publication/history" + query(BOOK), token=token, status=denied)
            for route in routes:
                request("/api/publication/" + route, {"name": "MCP工具标准调用参考_只读.txt"}, status=400)
            request("/api/publication/history" + query("MCP工具标准调用参考_只读.txt"), status=400)
            # The member has authenticated and entered do_POST, but has not sent a
            # complete body. Publication must not wait for its ten-second deadline.
            slow = socket.create_connection(("127.0.0.1", ports[1]), timeout=5)
            try:
                body = encode(dict(name=BOOK, id="history-a", who="ai", text="incomplete synthetic member body"))
                header = ("POST /api/notes/comment HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Who-Token: %s\r\nX-Test-Gate: slow-member\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % (AGENT, len(body))).encode()
                slow.sendall(header + body[:len(body)//2])
                wait_until(lambda: (control / "slow-member.started").exists(), "authenticated partial POST reached handler")
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    pending = pool.submit(request, "/api/publication/preview", {"name": BOOK})
                    try:
                        assert pending.result(timeout=4)[1]["can_commit"]
                    finally:
                        slow.close()
                print("PASS HTTP: an authenticated half-sent member POST does not hold the publication barrier", flush=True)
            finally:
                slow.close()
            preview = request("/api/publication/preview", {"name": BOOK})[1]
            repeated = request("/api/publication/preview", {"name": BOOK}, server=1)[1]
            assert preview["can_commit"] and preview["preview_digest"] == repeated["preview_digest"], (preview, repeated)
            assert canonical() == baseline
            print("PASS HTTP: all publication endpoints deny anonymous/agent/guide writes; previews preserve canonical bytes", flush=True)

            # A failed preparation has no commit. The exact same confirmation may retry across a second.
            confirmation = dict(name=BOOK, preview_digest=preview["preview_digest"], request_id="integration_publish_0001")
            arm("fail")
            request("/api/publication/commit", confirmation, status=500)
            assert canonical() == baseline
            time.sleep(1.1)  # Deliberately expose wall-clock payload instability on retry.
            before_cache = notes()["revision"]
            arm("hold")
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                published = pool.submit(request, "/api/publication/commit", confirmation)
                wait_until(lambda: (control / "entered").exists(), "pre-COMMIT hold")
                other_progress = dict(name=OTHER, offset=offset(OTHER_TEXT, "另一书阅读位置。"), book_revision=other_book["book_revision"], expected_revision=0)
                other_highlight = dict(name=OTHER, start=offset(OTHER_TEXT, "另一书新划线。"),
                    end=offset(OTHER_TEXT, "另一书新划线。") + units("另一书新划线。"), text="另一书新划线。")
                progress_future = pool.submit(request, "/api/progress", other_progress, server=1, headers={"X-Test-Gate": "other-progress"})
                highlight_future = pool.submit(request, "/api/notes/highlight", other_highlight, server=1, headers={"X-Test-Gate": "other-highlight"})
                try:
                    wait_until(lambda: (control / "other-progress.started").exists() and (control / "other-highlight.started").exists(), "other-book requests reached second process")
                    assert not progress_future.done() and not highlight_future.done(), "Writer crossed publication barrier"
                finally:
                    (control / "release").write_text("continue")
                result = published.result(timeout=50)[1]
                progress_future.result(timeout=50)
                highlight_future.result(timeout=50)
            current = book()
            assert "广告水印。" not in current["text"] and current["publication_generation"] == 1
            assert request("/api/publication/commit", confirmation, server=1)[1]["already_applied"] is True
            accepted = request("/api/proposals" + query(BOOK, status="accepted"))[1]["proposals"]
            assert any(row["id"] == proposal_id and row["status"] == "accepted" for row in accepted)
            other_notes = copy.deepcopy(raw_store("notes.json")["books"][OTHER])
            other_saved_progress = copy.deepcopy(raw_store("progress.json")["books"][OTHER])
            assert other_notes["highlights"][0]["text"] == "另一书新划线。" and other_notes["other_extra"] == 8
            assert other_saved_progress["user"]["custom"] == {"other": True}
            assert raw_store("notes.json")["extension"] == {"keep": True}
            assert raw_store("progress.json")["extension"] == {"keep": [9]}
            fresh_notes = notes(since=before_cache)
            assert not fresh_notes.get("not_modified") and fresh_notes["book_revision"] == current["book_revision"]
            assert notes(since=fresh_notes["revision"])["not_modified"] is True
            print("PASS HTTP: pre-COMMIT same-ID retry; two-process other-book notes/progress wait and survive publication; notes cache invalidates", flush=True)

            protected = canonical()
            first = "第一章"
            highlight = dict(name=BOOK, start=0, end=units(first), text=first)
            request("/api/notes/highlight", highlight, server=1, status=428)
            request("/api/notes/highlight", dict(highlight, book_revision=original_revision), server=1, status=409)
            previous = current["by_who"]["user"]
            request("/api/progress", dict(name=BOOK, offset=0, book_revision=original_revision, expected_revision=previous["revision"]), server=1, status=409)
            request("/api/progress", dict(name=BOOK, offset=0, expected_revision=previous["revision"]), server=1, status=428)
            assert canonical() == protected
            new_quote = "后段进度锚点。"
            new_offset = offset(current["text"], new_quote)
            new_id = request("/api/notes/highlight", dict(name=BOOK, start=new_offset, end=new_offset + units(new_quote), text=new_quote,
                book_revision=current["book_revision"]), server=1)[1]["id"]
            request("/api/notes/comment", dict(name=BOOK, id=new_id, who="ai", text="发布后新评论"), server=1, token=AGENT)
            request("/api/progress", dict(name=BOOK, offset=new_offset, book_revision=current["book_revision"], expected_revision=previous["revision"]), server=1)
            # The ordinary notes RMW above can add author_status/highlight_id to
            # other legacy rows. Verify all prior data, then bind the strict
            # before/after comparison to the actual rollback operation.
            other_before_rollback = copy.deepcopy(raw_store("notes.json")["books"][OTHER])
            assert_preserved(other_notes, other_before_rollback)
            history = request("/api/publication/history" + query(BOOK))[1]["items"]
            publication_id = next(row["publication_id"] for row in history if row["can_rollback"])
            rollback = request("/api/publication/rollback-preview", dict(name=BOOK, publication_id=publication_id))[1]
            assert rollback["can_commit"]
            cache_before_rollback = notes()["revision"]
            payload = dict(name=BOOK, publication_id=publication_id, preview_digest=rollback["preview_digest"], request_id="integration_rollback_0001")
            request("/api/publication/rollback", payload, server=1)
            assert request("/api/publication/rollback", payload)[1]["already_applied"] is True
            restored = book()
            assert restored["book_revision"] == original_revision and restored["publication_generation"] == 2
            assert restored["by_who"]["user"]["offset"] == offset(TEXT, new_quote)
            stored_notes = raw_store("notes.json")
            original = next(row for row in stored_notes["books"][BOOK]["highlights"] if row["id"] == "history-a")
            added = next(row for row in stored_notes["books"][BOOK]["highlights"] if row["id"] == new_id)
            assert original["author_id"] == "revoked-member" and original["extra"] == {"preserve": [1, 2]}
            assert original["thread"][0]["annotation_id"] == "historical-comment" and original["thread"][0]["extra"] == {"origin": "legacy"}
            assert added["start"] == offset(TEXT, new_quote) and added["thread"][0]["author_id"] == "agent"
            assert stored_notes["books"][OTHER] == other_before_rollback
            assert raw_store("progress.json")["books"][OTHER] == other_saved_progress
            assert not notes(since=cache_before_rollback).get("not_modified")
            assert identities.read_bytes() == original_identity
            rollback_protected = canonical()
            request("/api/notes/highlight", highlight, server=1, status=428)
            request("/api/notes/highlight", dict(highlight, book_revision=current["book_revision"]), server=1, status=409)
            request("/api/progress", dict(name=BOOK, offset=0, book_revision=current["book_revision"],
                expected_revision=restored["by_who"]["user"]["revision"]), server=1, status=409)
            assert canonical() == rollback_protected
            print("PASS HTTP: published stale/missing version denial; rollback preserves new comments/progress, foreign book and historical metadata", flush=True)

            # Exercise the rollback EX barrier itself with real writes on the
            # second reader; the earlier rollback used a strict unchanged-B gate.
            second_preview = request("/api/publication/preview", {"name": BOOK})[1]
            assert second_preview["can_commit"]
            request("/api/publication/commit", dict(name=BOOK, preview_digest=second_preview["preview_digest"], request_id="integration_publish_0002"))
            second_id = next(row["publication_id"] for row in request("/api/publication/history" + query(BOOK))[1]["items"] if row["can_rollback"])
            second_rollback = request("/api/publication/rollback-preview", dict(name=BOOK, publication_id=second_id))[1]
            assert second_rollback["can_commit"]
            rollback_payload = dict(name=BOOK, publication_id=second_id, preview_digest=second_rollback["preview_digest"], request_id="integration_rollback_0002")
            full = canonical()
            limited = request("/api/publication/rollback", rollback_payload, status=409)[1]
            assert "three histories" in limited["error"] and canonical() == full
            obsolete = next(row["publication_id"] for row in request("/api/publication/history" + query(BOOK))[1]["items"]
                            if row["action"] == "rollback" and row["can_cleanup"])
            capacity_plan = request("/api/publication/cleanup-preview", dict(name=BOOK, publication_ids=[obsolete]))[1]
            request("/api/publication/cleanup", dict(name=BOOK, publication_ids=[obsolete], confirm_digest=capacity_plan["digest"]))
            before_other_notes = copy.deepcopy(raw_store("notes.json")["books"][OTHER])
            before_other_progress = copy.deepcopy(raw_store("progress.json")["books"][OTHER])
            other_id = before_other_notes["highlights"][0]["id"]
            arm("hold")
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                rolling = pool.submit(request, "/api/publication/rollback", rollback_payload)
                try:
                    wait_until(lambda: (control / "entered").exists(), "rollback pre-COMMIT hold")
                except AssertionError:
                    if rolling.done():
                        rolling.result()
                    raise
                concurrent_progress = pool.submit(request, "/api/progress", dict(name=OTHER, offset=0,
                    book_revision=other_book["book_revision"], expected_revision=before_other_progress["user"]["revision"]),
                    server=1, headers={"X-Test-Gate": "other-progress"})
                concurrent_comment = pool.submit(request, "/api/notes/comment", dict(name=OTHER, id=other_id, who="ai", text="回滚期间另一书新评论"),
                    server=1, token=AGENT, headers={"X-Test-Gate": "other-highlight"})
                try:
                    wait_until(lambda: (control / "other-progress.started").exists() and (control / "other-highlight.started").exists(), "other-book writes reached rollback barrier")
                    assert not concurrent_progress.done() and not concurrent_comment.done()
                finally:
                    (control / "release").write_text("continue")
                rolling.result(timeout=50)
                progress_ack = concurrent_progress.result(timeout=50)[1]
                comment_ack = concurrent_comment.result(timeout=50)[1]["annotation"]
            expected_other = copy.deepcopy(before_other_notes)
            expected_other["highlights"][0]["thread"].append(comment_ack)
            assert raw_store("notes.json")["books"][OTHER] == expected_other
            saved_other = raw_store("progress.json")["books"][OTHER]["user"]
            assert saved_other["revision"] == progress_ack["revision"] == before_other_progress["user"]["revision"] + 1
            assert saved_other["offset"] == 0 and saved_other["custom"] == before_other_progress["user"]["custom"]
            assert book()["book_revision"] == original_revision
            print("PASS HTTP: another book's comment and progress block across rollback, then both acknowledged writes survive", flush=True)

            selected = [publication_id]
            before_cleanup = canonical()
            plan = request("/api/publication/cleanup-preview", dict(name=BOOK, publication_ids=selected))[1]
            assert canonical() == before_cleanup
            clean = dict(name=BOOK, publication_ids=selected, confirm_digest=plan["digest"])
            try:
                request("/api/publication/cleanup", clean, headers={"X-Test-Drop-Reply": "1"})
            except (http.client.RemoteDisconnected, urllib.error.URLError, ConnectionResetError):
                pass
            else:
                raise AssertionError("Expected intentionally dropped cleanup response")
            assert request("/api/publication/cleanup", clean, server=1)[1]["already_cleaned"] is True
            assert canonical() == before_cleanup
            print("PASS HTTP: cleanup preview is read-only; lost cleanup response retries idempotently across processes", flush=True)

            archive = Path(folder) / "legacy-export.zip"
            before_backup = canonical()
            backup = [sys.executable, "-B", str(core / "tools/backup.py")]
            for label, arguments, expected_ok in (
                    ("export", ["export", "-o", str(archive)], True),
                    ("info", ["info", str(archive)], True),
                    ("import-refused", ["import", str(archive), "--overwrite"], False)):
                result = subprocess.run(backup + arguments, capture_output=True, text=True, encoding="utf-8", timeout=30)
                (artifacts / ("backup-" + label + ".log")).write_text(result.stdout + result.stderr, encoding="utf-8")
                assert (result.returncode == 0) is expected_ok, (label, result.stdout, result.stderr)
                if not expected_ok:
                    assert "发布记录" in result.stderr
                assert canonical() == before_backup, label
            print("PASS maintenance CLI: backup export/info work; legacy overwrite-import refuses a published library and preserves data", flush=True)

            start_draft(INSERT, quote="目标批注锚点。", replacement="新增文字目标批注锚点。")
            insertion = request("/api/publication/preview", {"name": INSERT})[1]
            assert insertion["can_commit"]
            request("/api/publication/commit", dict(name=INSERT, preview_digest=insertion["preview_digest"], request_id="integration_insert_0001"))
            inserted = book(INSERT)
            quote = "增文"
            new_start = offset(inserted["text"], quote)
            request("/api/notes/highlight", dict(name=INSERT, start=new_start, end=new_start + units(quote), text=quote, book_revision=inserted["book_revision"]))
            insert_id = request("/api/publication/history" + query(INSERT))[1]["items"][0]["publication_id"]
            preserved = canonical()
            blocked = request("/api/publication/rollback-preview", dict(name=INSERT, publication_id=insert_id))[1]
            assert not blocked["can_commit"] and blocked["issues_total"] > 0
            request("/api/publication/rollback", dict(name=INSERT, publication_id=insert_id, preview_digest=blocked["preview_digest"], request_id="integration_insert_rollback_0001"), status=409)
            assert canonical() == preserved
            print("PASS HTTP: reverse-ambiguous newly inserted highlight blocks rollback without touching any canonical data", flush=True)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
            for process in processes:
                process.wait(timeout=15)
            for handle in handles:
                handle.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--server-child":
        child(Path(sys.argv[2]), int(sys.argv[3]), Path(sys.argv[4]), sys.argv[5] == "True")
    else:
        run()
