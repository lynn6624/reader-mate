"""Disposable browser check for shared highlight rendering and touch selection."""
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).parent
CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe"
BOOK = "第一章\n你好😀一起读书。\n第二段。"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


with tempfile.TemporaryDirectory() as directory:
    base = pathlib.Path(directory)
    shutil.copy(ROOT / "reader_server.py", base / "reader_server.py")
    shutil.copy(ROOT / "chapter_index.py", base / "chapter_index.py")
    shutil.copy(ROOT / "progress_catalog.py", base / "progress_catalog.py")
    shutil.copytree(ROOT / "web", base / "web")
    with_kindle = (ROOT / "extensions" / "kindle.py").exists()
    if with_kindle:
        shutil.copytree(ROOT / "extensions", base / "extensions")
    (base / "data").mkdir()
    (base / "书库").mkdir()
    (base / "data" / "identities.json").write_text(
        json.dumps({who: "test-" + who for who in ("user", "op001", "aru001", "op004")}))
    (base / "书库" / "test.txt").write_text(BOOK, encoding="utf-8")
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    env = dict(os.environ, READER_CLOUD="1", READER_PORT=str(port),
               READER_MCP_PUBLIC_HOST="reader.example.com")
    process = subprocess.Popen([sys.executable, str(base / "reader_server.py")],
                               env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def request(path, who="user", data=None):
        headers = {"X-Who-Token": "test-" + who}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            url + path, headers=headers,
            data=json.dumps(data, ensure_ascii=False).encode() if data is not None else None)
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.load(response)

    def add(who, start, end):
        text = BOOK.encode("utf-16-le")[start * 2:end * 2].decode("utf-16-le")
        return request("/api/notes/highlight", who, {
            "name": "test.txt", "start": start, "end": end, "text": text})["id"]

    try:
        for _ in range(100):
            try:
                request("/api/books")
                break
            except Exception:
                time.sleep(.1)
        else:
            raise AssertionError("HTTP server did not start")
        h002 = add("user", 4, 6)
        h004 = add("op004", 4, 6)
        add("op001", 5, 9)
        add("op004", 9, 11)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=CHROME, headless=True)
            try:
                context = browser.new_context(viewport={"width": 390, "height": 844},
                                              is_mobile=True, has_touch=True)
                page = context.new_page()
                page.goto(url + "/?who_token=test-user&who=op004", wait_until="domcontentloaded")
                page.wait_for_function("document.querySelector('#reader mark.multi') !== null")
                assert page.evaluate("state.who") == 'user'
                result = page.evaluate("""() => {
                    const marks = [...document.querySelectorAll('#reader mark.shared-hl')];
                    return {
                      mode: document.querySelector('#hlVisibility').value,
                      pink: marks.some(m => m.textContent.includes('你') && m.style.getPropertyValue('--hl-color') === '#f6d8e4'),
                      purple: marks.some(m => m.textContent.includes('😀') && m.style.getPropertyValue('--hl-color') === '#e5dcf5'),
                      cyan: marks.some(m => m.textContent.includes('读') && m.style.getPropertyValue('--hl-color') === '#d4f0ee'),
                      noNested: marks.every(m => !m.querySelector('mark')),
                      overlap: marks.some(m => m.classList.contains('multi') && m.dataset.hlIds.split(' ').length === 3)
                    };
                }""")
                assert result == {"mode": "all", "pink": True, "purple": True,
                                  "cyan": True, "noNested": True, "overlap": True}, result
                assert page.evaluate("""async () => {
                    const before = document.querySelector('#reader mark.multi');
                    await loadHighlights('test.txt');
                    return before === document.querySelector('#reader mark.multi');
                }"""), "Unchanged co-reading poll rewrote the reader DOM"
                page.locator("#reader mark.multi").filter(has_text="好").first.tap()
                assert page.locator("#hlChoices button").count() == 3
                page.locator("#btnCloseHlPanel").tap()
                page.locator("#hlVisibility").select_option("mine")
                assert page.locator("#reader mark.shared-hl").count() >= 1
                assert page.locator("#reader mark.shared-hl").evaluate_all(
                    "(marks) => marks.every(m => m.style.getPropertyValue('--hl-color') === '#f6d8e4')")
                page.locator("#hlVisibility").select_option("all")
                page.locator("#btnFontInc").tap()
                page.reload()
                page.wait_for_function("document.querySelector('#reader mark.multi') !== null")
                assert page.locator("#reader mark.multi").count() >= 1
                # Select one character inside an existing mark on a touch-enabled page.
                selected = page.evaluate("""() => {
                    const m = [...document.querySelectorAll('#reader mark.shared-hl')]
                      .find(x => x.textContent.includes('读'));
                    const node = m.firstChild, at = node.textContent.indexOf('读');
                    const range = document.createRange();
                    range.setStart(node, at); range.setEnd(node, at + 1);
                    const selection = window.getSelection();
                    selection.removeAllRanges(); selection.addRange(range);
                    onSelectionChange();
                    return {start: hlSel?.info.start, end: hlSel?.info.end,
                            menu: document.querySelector('#hlMenu').classList.contains('on')};
                }""")
                assert selected == {"start": 10, "end": 11, "menu": True}, selected
                page.locator("#hlMenu [data-act=line]").tap()
                page.wait_for_function("""() => document.querySelectorAll('#reader mark.multi')
                    .length > 0 && [...document.querySelectorAll('#reader mark.multi')]
                    .some(m => m.textContent.includes('读') && m.dataset.hlIds.split(' ').length === 2)""")
                assert len(request("/api/notes?name=test.txt")["highlights"]) == 5
                page.wait_for_timeout(1500)  # let an earlier normal scroll save settle
                before = request("/api/reading-progress?name=test.txt")
                page.evaluate("jumpTo(state.book.text.indexOf('第二段'), '第二段')")
                assert page.locator("#btnKeepPosition").is_visible()
                page.wait_for_timeout(1500)
                after = request("/api/reading-progress?name=test.txt")
                assert after['progress'] == before['progress'], (before, after)
                page.locator("#btnKeepPosition").tap()
                page.wait_for_function("""(rev) => state.book.progress.revision > rev
                    && state.book.progress.offset === state.segs[state.currentSeg].start""",
                    arg=before['revision'])
                saved = request("/api/reading-progress?name=test.txt")['progress']
                assert saved['offset'] == page.evaluate("state.segs[state.currentSeg].start")
                page.locator("#btnLib").tap()
                page.locator("#libTabs [data-tab=overview]").tap()
                page.wait_for_function("document.querySelectorAll('#overviewList .progressItem').length === 4")
                labels = page.locator("#overviewList .progressHead span:nth-child(2)").all_text_contents()
                assert any("op001（001）" in label for label in labels), labels
                assert any(label == "aru001" for label in labels), labels
                assert not any(label == "001" for label in labels), labels
                if with_kindle:
                    assert page.locator("#kindlePairing").is_visible()
                    page.locator("#kindleCreate").tap()
                    page.wait_for_function("document.querySelector('#kindlePairUrl').textContent.includes('/k/')")
                    assert page.locator("#kindlePairUrl").get_attribute("href").startswith(
                        "https://reader.example.com/k/")
                else:
                    assert page.locator("#kindlePairing").count() == 0
                page.locator("#libTabs [data-tab=lib]").tap()
                assert page.locator(".shelf-group").count() == 6
                assert page.locator('.shelf-group[data-category=nonfiction] summary b').inner_text() == '随笔与纪实'
                assert page.locator('.shelf-group[data-category=humanities] summary b').inner_text() == '人文社科'
                assert page.locator(".shelf-group[open]").count() == 0
                pending = page.locator('.shelf-group[data-category=uncategorized]')
                literature = page.locator('.shelf-group[data-category=literature]')
                pending.locator("summary").tap()
                literature.locator("summary").tap()
                assert page.locator(".shelf-group[open]").count() == 2
                assert literature.locator(".hint").inner_text() == "暂无书籍"
                page.reload()
                page.wait_for_function("document.querySelectorAll('.shelf-group').length === 6")
                page.locator("#btnLib").tap()
                assert page.locator(".shelf-group[open]").count() == 2
                page.locator("#libQuery").fill("test")
                assert page.locator(".shelf-group:not([hidden])").count() == 1
                assert pending.locator(".book").is_visible()
                page.locator("#libQuery").fill("")
                assert page.locator(".shelf-group[open]").count() == 2
                page.locator("#fileInput").set_input_files([
                    {"name":"ui-novel.txt", "mimeType":"text/plain", "buffer":"小说正文。".encode()},
                    {"name":"ui-tools.txt", "mimeType":"text/plain", "buffer":"工具正文。".encode()}])
                assert page.locator("#uploadDialog").is_visible()
                assert page.locator("#uploadSubmit").is_disabled()
                assert not (base / "书库/ui-novel.txt").exists()
                page.locator("#uploadCategory").select_option("literature")
                page.locator('#uploadRows select').nth(1).select_option("humanities")
                artifact = os.environ.get('READER_TEST_SCREENSHOTS')
                if artifact:
                    pathlib.Path(artifact).mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(pathlib.Path(artifact) / 'upload-categories-mobile.png'))
                page.locator("#uploadSubmit").tap()
                page.wait_for_function("document.querySelector('#uploadDialog').hidden && document.querySelectorAll('#libList .book').length === 3")
                page.locator("#libQuery").fill("ui-tools")
                assert page.locator('.shelf-group[data-category=humanities] .book').is_visible()
                page.locator('.shelf-group[data-category=humanities] .book-category select').select_option("web-fiction")
                page.wait_for_function("document.querySelector('.shelf-group[data-category=web-fiction] .book') !== null")
                catalog = {b['name']: b for b in request('/api/books')['books']}
                assert catalog['ui-tools.txt']['category'] == 'web-fiction'
                assert catalog['ui-novel.txt']['category'] == 'literature'
                assert request('/api/reading-progress?name=test.txt')['progress'] == saved
                assert len(request('/api/notes?name=test.txt')['highlights']) == 5
                page.locator("#libQuery").fill("")
                artifact = os.environ.get('READER_TEST_SCREENSHOTS')
                if artifact:
                    pathlib.Path(artifact).mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(pathlib.Path(artifact) / 'bookshelf-mobile.png'))
                context.close()
                desktop = browser.new_context(viewport={"width": 1280, "height": 820})
                desktop_page = desktop.new_page()
                desktop_page.goto(url + "/?who_token=test-op004", wait_until="domcontentloaded")
                desktop_page.wait_for_function("document.querySelector('#reader mark.multi') !== null")
                assert not desktop_page.locator("#kindlePairing").is_visible()
                assert desktop_page.evaluate("""() => [...document.querySelectorAll('#reader mark.multi')]
                    .some(m => m.textContent.includes('你') && m.style.getPropertyValue('--hl-color') === '#d4f0ee')""")
                desktop_page.locator("#reader mark.multi").filter(has_text="你").first.click()
                assert desktop_page.locator("#hlChoices button").count() == 2
                desktop_page.locator("#hlChoices button").filter(has_text="002").first.click()
                desktop_page.wait_for_function("document.querySelector('#hlPanelTitle').textContent.startsWith('002')")
                desktop_page.locator("#hlInput").fill("004在002的划线下留言")
                desktop_page.locator("#hlSend").click()
                desktop_page.wait_for_function("""() => document.querySelector('#hlPanelThread')
                    ?.innerText.includes('004在002的划线下留言')""")
                annotations = request("/api/notes/annotations?name=test.txt&highlight_id=" + h002,
                                      "op004")["annotations"]
                assert len(annotations) == 1 and annotations[0]["author_id"] == "op004"
                desktop_page.locator('#btnCloseHlPanel').click()
                desktop_page.locator('#btnLib').click()
                assert not desktop_page.locator('#importTrigger').is_visible()
                assert desktop_page.locator('#libList .book-category').count() == 0
                assert desktop_page.locator('.shelf-group').count() == 6
                desktop_page.locator('#libQuery').fill('ui-novel')
                assert desktop_page.locator('.shelf-group[data-category=literature] .book').is_visible()
                desktop.close()
            finally:
                browser.close()
        print("PASS: mobile/desktop co-reading; collapsed groups, remembered state, cross-group search, required batch/per-file categories, owner-only controls")
    finally:
        process.terminate()
        process.wait(timeout=10)
