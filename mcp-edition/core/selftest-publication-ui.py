"""Real publication UI acceptance using disposable books and loopback HTTP only."""
import ast
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parent
NAME = "publication-ui.txt"
TEXT = "第一章 合成测试\n前段广告。\n甲😀乙。\n" + "".join(
    "第%03d段：合成阅读正文，保留批注和进度。\n" % index for index in range(350))


def units(value):
    return len(value.encode("utf-16-le")) // 2


def offset(text, quote):
    return units(text[:text.index(quote)])


def mcp_contracts():
    """Exercise the actual tool bodies without importing credentials or doing IO."""
    tree = ast.parse((ROOT / "mcp_server.py").read_text(encoding="utf-8"))
    tools = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
             and any(isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                     and decorator.func.attr == "tool" for decorator in node.decorator_list)]
    assert len(tools) == 15
    functions = [node for node in tools if node.name in ("read_book", "add_highlight", "update_progress")]
    for node in functions:
        node.decorator_list = []
    context = {"Context": object, "urllib": urllib}
    exec(compile(ast.Module(body=functions, type_ignores=[]), "<isolated MCP bodies>", "exec"), context)
    arguments = {node.name: [argument.arg for argument in node.args.args] for node in functions}
    assert arguments["add_highlight"] == ["name", "start", "end", "text", "ctx", "book_revision"]
    assert arguments["update_progress"] == ["name", "offset", "ctx", "expected_revision", "book_revision"]
    for generation in (0, 1):
        for revision in ("", "explicit-read-version"):
            for tool in ("add_highlight", "update_progress"):
                calls = []
                book = dict(text="甲乙丙丁", book_revision="current-version", publication_generation=generation,
                            by_who={"agent": {"revision": 4, "tts_offset": 0}})
                async def api(ctx, path, payload=None):
                    calls.append((path, copy.deepcopy(payload)))
                    return copy.deepcopy(book) if payload is None else {"ok": True}
                context["api"] = api
                context["credentials"] = lambda ctx: ("agent", "synthetic")
                arguments = (NAME, 2, None) if tool == "update_progress" else (NAME, 0, 2, "甲乙", None)
                coroutine = context[tool](*arguments, **({"book_revision": revision} if revision else {}))
                try:
                    coroutine.send(None)  # All awaited APIs complete synchronously; no event-loop sockets.
                except StopIteration:
                    expected = revision or ("current-version" if tool == "update_progress" else "")
                    assert calls[-1][1]["book_revision"] == expected
                except ValueError as error:
                    assert tool == "update_progress" and generation and not revision
                    assert all(payload is None for _, payload in calls)
                    assert "read_book" in str(error) and "book_revision" in str(error)
                else:
                    coroutine.close()
                    raise AssertionError("Unexpected pending tool IO")
    for generation in (0, 2):
        book = dict(name=NAME, text="甲😀乙", book_revision="read-response-version", by_who={})
        if generation:
            book["publication_generation"] = generation
        async def read_api(ctx, path):
            return copy.deepcopy(book)
        context["api"] = read_api
        coroutine = context["read_book"](NAME, None, length=3)
        try:
            coroutine.send(None)
        except StopIteration as finished:
            result = finished.value
        else:
            coroutine.close()
            raise AssertionError("Unexpected pending read IO")
        assert result["text"] == "甲😀" and result["next_offset"] == 3
        assert result["publication_generation"] == generation
        assert "book_revision=" in result["reminder"] and book["book_revision"] in result["reminder"]
    print("PASS MCP: 15 tools, appended optional arguments, legacy fallback, published refusal, exact revision forwarding and read generation/reminder", flush=True)


def run():
    from playwright.sync_api import sync_playwright
    artifacts = Path(os.environ["PUBLICATION_UI_ARTIFACT_DIR"]).resolve() if os.environ.get("PUBLICATION_UI_ARTIFACT_DIR") else None
    if artifacts:
        artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="publication-ui-") as directory:
        core = Path(directory) / "core"
        core.mkdir()
        for path in ROOT.glob("*.py"):
            if not path.name.startswith("selftest"):
                shutil.copy(path, core / path.name)
        for name in ("web", "tools"):
            shutil.copytree(ROOT / name, core / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        if artifacts:
            hashes = {str(path.relative_to(core)): hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in core.rglob("*") if path.is_file() and path.suffix in (".py", ".js", ".html")}
            (artifacts / "source-sha256.json").write_text(json.dumps(hashes, indent=2), encoding="utf-8")
        (core / "书库").mkdir()
        (core / "data").mkdir()
        book_path = core / "书库" / NAME
        book_path.write_bytes(TEXT.replace("\n", "\r\n").encode())
        identities = core / "data/identities.json"
        identities.write_text('{"user":"synthetic-owner","agent":"synthetic-agent"}')
        for filename in ("notes.json", "progress.json"):
            (core / "data" / filename).write_text('{"version":2,"books":{}}')
        original_identity = identities.read_bytes()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        origin = "http://127.0.0.1:%d" % port
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def request(route, data=None, token="synthetic-owner", expected=200):
            raw = None if data is None else json.dumps(data, ensure_ascii=False).encode()
            req = urllib.request.Request(origin + route, data=raw,
                headers={"X-Who-Token": token, "Content-Type": "application/json"})
            try:
                with opener.open(req, timeout=30) as response:
                    status, value = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                status, value = error.code, json.load(error)
            if expected is not None:
                assert status == expected, (route, status, value)
            return status, value
        query = "?" + urllib.parse.urlencode({"name": NAME})
        def protected():
            result = {"book": book_path.read_bytes(), "identity": identities.read_bytes()}
            for path in (core / "data").rglob("*.json"):
                if "publications" not in path.relative_to(core / "data").parts:
                    result[str(path.relative_to(core))] = path.read_bytes()
            return result
        env = dict(os.environ, READER_CLOUD="1", READER_HOST="127.0.0.1", READER_PORT=str(port), READER_TOKEN="")
        log_path = artifacts / "server.log" if artifacts else core / "test.log"
        with log_path.open("w+", encoding="utf-8") as log:
            process = subprocess.Popen([sys.executable, "-B", str(core / "reader_server.py")], env=env, stdout=log, stderr=log)
            try:
                for _ in range(150):
                    try:
                        request("/api/ping")
                        break
                    except OSError:
                        if process.poll() is not None:
                            log.seek(0)
                            raise AssertionError(log.read())
                        time.sleep(.05)
                else:
                    raise AssertionError("Synthetic server failed to start")
                book = request("/api/book" + query)[1]
                revision = book["book_revision"]
                draft = request("/api/draft/start", dict(name=NAME, book_revision=revision))[1]
                quote = "前段广告。"
                request("/api/draft/edit", dict(name=NAME, base_revision=revision,
                    expected_revision=draft["draft_revision"], request_id="publication_ui_edit_001",
                    start=offset(TEXT, quote), end=offset(TEXT, quote) + units(quote), quote=quote, replacement=""))
                with sync_playwright() as playwright:
                    executable = os.environ.get("CHROME_TEST") or ("C:/Program Files/Google/Chrome/Application/chrome.exe" if os.name == "nt" else "/usr/bin/chromium")
                    browser = playwright.chromium.launch(executable_path=executable, headless=True)
                    try:
                        context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
                        page = context.new_page()
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(origin + "/?who_token=synthetic-owner")
                        page.wait_for_function("typeof state!=='undefined' && state.book && canManageLibrary")
                        page.wait_for_timeout(1700)
                        def open_publication():
                            page.locator("#btnLib").click()
                            page.locator("#btnPublication").click()
                            page.locator("#publicationDialog:not([hidden])").wait_for()
                            page.wait_for_function("!publicationState.busy")
                        open_publication()
                        before = protected()
                        scroll = page.evaluate("view.scrollTop")
                        page.locator("#publicationPreview").click()
                        page.wait_for_function("publicationState.preview?.valid===true && !publicationState.busy")
                        assert page.locator("#publicationConfirm").is_disabled()
                        assert page.locator("#publicationAcknowledge").is_enabled()
                        # Space on a focused Close button correctly activates it.
                        # Exercise scrolling/reader shortcuts from the modal body instead.
                        page.locator("#publicationDialog .rm-review-body").evaluate("element=>{element.tabIndex=0;element.focus()}")
                        for key in ("PageDown", "Space", "j", "/", "Home"):
                            page.keyboard.press(key)
                        page.wait_for_timeout(1500)
                        assert protected() == before, "Read-only preview changed canonical records"
                        assert page.evaluate("view.scrollTop") == scroll
                        assert not page.locator("#searchPanel").evaluate("element=>element.classList.contains('on')")
                        summary = page.locator("#publicationSummary").inner_text()
                        if artifacts:
                            page.screenshot(path=str(artifacts / "preview-390.png"), full_page=True)
                        def stale(route):
                            route.fulfill(status=409, content_type="application/json", body=json.dumps({"ok": False, "error": "合成版本冲突"}))
                        page.route("**/api/publication/commit", stale)
                        page.locator("#publicationAcknowledge").check()
                        page.locator("#publicationConfirm").click()
                        page.wait_for_function("!publicationState.busy && !publicationState.preview.valid")
                        assert page.locator("#publicationConfirm").is_disabled()
                        assert page.locator("#publicationSummary").inner_text() == summary
                        assert page.locator("#publicationRetry").is_hidden()
                        page.unroute("**/api/publication/commit", stale)
                        page.locator("#publicationPreview").click()
                        page.wait_for_function("publicationState.preview?.valid===true && !publicationState.busy")
                        identifiers = []
                        def lost_response(route):
                            identifiers.append(route.request.post_data_json["request_id"])
                            if len(identifiers) == 1:
                                response = route.fetch()
                                assert response.status == 200, response.text()
                                route.abort("failed")
                            else:
                                route.continue_()
                        page.route("**/api/publication/commit", lost_response)
                        page.locator("#publicationAcknowledge").check()
                        page.locator("#publicationConfirm").click()
                        page.wait_for_function("publicationState.retryable && !publicationState.busy")
                        assert page.locator("#publicationConfirm").is_disabled()
                        page.locator("#publicationRetry").click()
                        page.wait_for_function("document.getElementById('publicationDialog').hidden && !publicationState.busy")
                        assert len(identifiers) == 2 and identifiers[0] == identifiers[1]
                        page.unroute("**/api/publication/commit", lost_response)
                        assert page.evaluate("state.temporaryView && !state.book.isDraft")
                        if artifacts:
                            page.screenshot(path=str(artifacts / "published-390.png"), full_page=True)
                        current = request("/api/book" + query)[1]
                        assert current["publication_generation"] > 0 and quote not in current["text"]
                        mark = "第250段"
                        start = offset(current["text"], mark)
                        payload = dict(name=NAME, start=start, end=start + units(mark), text=mark)
                        notes_before = (core / "data/notes.json").read_bytes()
                        assert request("/api/notes/highlight", payload, expected=None)[0] != 200
                        assert (core / "data/notes.json").read_bytes() == notes_before
                        highlight = request("/api/notes/highlight", dict(payload, book_revision=current["book_revision"]))[1]
                        highlights = request("/api/notes" + query)[1]["highlights"]
                        identifier = next(row["id"] for row in highlights if row["text"] == mark)
                        request("/api/notes/comment", dict(name=NAME, id=identifier, text="发布后的新评论", who="user"))
                        previous = current.get("by_who", {}).get("user", {})
                        request("/api/progress", dict(name=NAME, offset=start, book_revision=current["book_revision"], expected_revision=previous.get("revision", 0)))
                        open_publication()
                        history = request("/api/publication/history" + query)[1]["items"]
                        publication_id = next(row["publication_id"] for row in history if row["can_rollback"])
                        card = page.locator("#publicationHistory [data-publication-id]").filter(has=page.get_by_text(publication_id, exact=False)).first
                        card.get_by_role("button", name="校验回滚影响").click()
                        page.wait_for_function("publicationState.preview?.action==='rollback' && publicationState.preview.valid && !publicationState.busy")
                        assert page.locator("#publicationAcknowledge").is_enabled()
                        if artifacts:
                            page.screenshot(path=str(artifacts / "rollback-preview-390.png"), full_page=True)
                        page.locator("#publicationAcknowledge").check()
                        page.locator("#publicationConfirm").click()
                        page.wait_for_function("document.getElementById('publicationDialog').hidden && !publicationState.busy")
                        restored = request("/api/book" + query)[1]
                        assert restored["book_revision"] == revision
                        assert restored["by_who"]["user"]["offset"] == offset(TEXT, mark)
                        row = next(row for row in request("/api/notes" + query)[1]["highlights"] if row["id"] == identifier)
                        assert row["start"] == offset(TEXT, mark) and any(comment["text"] == "发布后的新评论" for comment in row["thread"])
                        open_publication()
                        before_cleanup = protected()
                        choices = page.locator("#publicationHistory input[data-publication-id]")
                        assert choices.count() > 0, "No old history material available for cleanup acceptance"
                        choices.first.check()
                        page.locator("#publicationCleanupPreview").click()
                        page.wait_for_function("publicationState.preview?.action==='cleanup' && publicationState.preview.valid && !publicationState.busy")
                        assert page.locator("#publicationConfirm").is_disabled()
                        assert "无法" in page.locator("#publicationAcknowledgeText").inner_text()
                        page.locator("#publicationAcknowledge").check()
                        page.locator("#publicationConfirm").click()
                        page.wait_for_function("!publicationState.busy && publicationState.preview===null")
                        assert protected() == before_cleanup
                        if artifacts:
                            page.screenshot(path=str(artifacts / "cleanup-history-390.png"), full_page=True)
                        page.locator("#publicationClose").click()
                        context.close()
                        for width in (320, 1280):
                            context = browser.new_context(viewport={"width": width, "height": 844})
                            page = context.new_page()
                            page.goto(origin + "/?who_token=synthetic-owner")
                            page.wait_for_function("typeof state!=='undefined' && state.book && canManageLibrary")
                            open_publication()
                            box = page.locator("#publicationDialog .rm-review-body").bounding_box()
                            assert box and box["x"] >= 0 and box["x"] + box["width"] <= width
                            if artifacts:
                                page.screenshot(path=str(artifacts / ("dialog-%d.png" % width)), full_page=True)
                            page.keyboard.press("Escape")
                            assert page.locator("#publicationDialog").is_hidden()
                            context.close()

                        # A reader already open in another window must discover an
                        # external HTTP publication through the real five-second
                        # notes polling timer, without saving a replacement position.
                        stale_name = "publication-stale-window.txt"
                        stale_path = core / "书库" / stale_name
                        stale_path.write_bytes(TEXT.replace("\n", "\r\n").encode())
                        stale_query = "?" + urllib.parse.urlencode({"name": stale_name})
                        stale_book = request("/api/book" + stale_query)[1]
                        stale_revision = stale_book["book_revision"]
                        quote = "第250段"
                        old_start = offset(TEXT, quote)
                        stale_id = request("/api/notes/highlight", dict(name=stale_name, start=old_start,
                            end=old_start + units(quote), text=quote, book_revision=stale_revision))[1]["id"]
                        request("/api/progress", dict(name=stale_name, offset=old_start,
                            book_revision=stale_revision, expected_revision=0))
                        stale_draft = request("/api/draft/start", dict(name=stale_name, book_revision=stale_revision))[1]
                        removed = "前段广告。"
                        request("/api/draft/edit", dict(name=stale_name, base_revision=stale_revision,
                            expected_revision=stale_draft["draft_revision"], request_id="external_publish_edit_001",
                            start=offset(TEXT, removed), end=offset(TEXT, removed) + units(removed), quote=removed, replacement=""))
                        context = browser.new_context(viewport={"width": 390, "height": 844})
                        page = context.new_page()
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(origin + "/?who_token=synthetic-owner")
                        page.wait_for_function("typeof state!=='undefined' && state.book")
                        page.evaluate("name=>openBook(name)", stale_name)
                        page.wait_for_function("data=>state.book?.name===data.name && notesCache.name===data.name && notesCache.list.some(row=>row.id===data.id)", arg={"name": stale_name, "id": stale_id})
                        page.wait_for_timeout(1700)
                        assert page.evaluate("state.book.book_revision") == stale_revision
                        old_cache = page.evaluate("notesCache.revision")
                        progress_posts = []
                        page.on("request", lambda req: progress_posts.append(req.url)
                                if req.method == "POST" and urllib.parse.urlparse(req.url).path == "/api/progress" else None)
                        external = request("/api/publication/preview", {"name": stale_name})[1]
                        assert external["can_commit"]
                        request("/api/publication/commit", dict(name=stale_name, preview_digest=external["preview_digest"], request_id="external_publish_confirm_001"))
                        after_publication_progress = (core / "data/progress.json").read_bytes()
                        updated_book = request("/api/book" + stale_query)[1]
                        expected_start = offset(updated_book["text"], quote)
                        page.wait_for_function("data=>state.book?.name===data.name && state.book.book_revision===data.revision && state.temporaryView && notesCache.name===data.name && notesCache.bookRevision===data.revision && notesCache.revision!==data.old_cache && notesCache.list.some(row=>row.id===data.id && row.start===data.start)",
                            arg={"name": stale_name, "revision": updated_book["book_revision"], "old_cache": old_cache, "id": stale_id, "start": expected_start}, timeout=15000)
                        page.wait_for_timeout(1600)
                        assert removed not in page.evaluate("state.book.text")
                        assert (core / "data/progress.json").read_bytes() == after_publication_progress
                        assert not progress_posts, progress_posts
                        if artifacts:
                            page.screenshot(path=str(artifacts / "external-publish-refresh-390.png"), full_page=True)
                            (artifacts / "external-publish-refresh.json").write_text(json.dumps(dict(
                                old_revision=stale_revision, new_revision=updated_book["book_revision"],
                                old_cache=old_cache, new_cache=page.evaluate("notesCache.revision"),
                                old_offset=old_start, new_offset=expected_start, extra_progress_posts=len(progress_posts)),
                                ensure_ascii=False, indent=2), encoding="utf-8")
                        context.close()
                        print("PASS old reader window: real notes polling detects external HTTP publication, reloads body/cache/positions and does not save progress", flush=True)
                        context = browser.new_context()
                        page = context.new_page()
                        page.goto(origin + "/?who_token=synthetic-agent")
                        page.wait_for_function("typeof state!=='undefined' && state.book")
                        assert page.locator("#btnPublication").is_hidden()
                        assert request("/api/publication/preview", {"name": NAME}, token="synthetic-agent", expected=None)[0] == 403
                        context.close()
                        assert not errors, errors
                    except BaseException:
                        if artifacts:
                            try:
                                page.screenshot(path=str(artifacts / "failure.png"), full_page=True)
                                (artifacts / "browser-errors.json").write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8")
                            except Exception:
                                pass
                        raise
                    finally:
                        browser.close()
                assert identities.read_bytes() == original_identity
                print("PASS publication browser: preview preservation, keyboard isolation, stale refusal, lost-response same-ID retry, publish/rollback with new records, explicit cleanup, 320/390/1280px and ordinary-identity denial", flush=True)
            finally:
                process.terminate()
                process.wait(timeout=15)


if __name__ == "__main__":
    mcp_contracts()
    if "--mcp-only" not in sys.argv:
        run()
