"""Review keyboard isolation and narrow-screen controls; loopback synthetic data only."""
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
import urllib.request

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
TEXT = '第一章\n' + ''.join(f'第{i:03d}段：合成阅读正文，保持阅读位置。\n' for i in range(200))


def run():
    with tempfile.TemporaryDirectory() as directory:
        core = Path(directory) / 'core'
        core.mkdir()
        for name in ('reader_server.py', 'member_store.py', 'book_drafts.py', 'revision_proposals.py',
                     'navigation.py', 'chapter_index.py', 'progress_catalog.py'):
            shutil.copy(ROOT / name, core / name)
        shutil.copytree(ROOT / 'web', core / 'web')
        (core / 'data').mkdir(); (core / '书库').mkdir()
        book = core / '书库/test.txt'; book.write_bytes(TEXT.encode())
        revision = 'sha256:' + hashlib.sha256(book.read_bytes()).hexdigest()
        identities = core / 'data/identities.json'
        identities.write_text('{"user":"owner-test","agent":"agent-test"}')
        notes = core / 'data/notes.json'; notes.write_text('{"version":2,"books":{}}')
        progress = core / 'data/progress.json'
        progress.write_text(json.dumps({'books': {'test.txt': {'user': {
            'offset': 0, 'percent': 0, 'chars': len(TEXT), 'book_revision': revision,
            'revision': 0, 'anchor': TEXT[:40]}}}}))
        protected = {p: p.read_bytes() for p in (book, identities, notes)}
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        origin = f'http://127.0.0.1:{port}'
        env = dict(os.environ, READER_CLOUD='1', READER_HOST='127.0.0.1', READER_PORT=str(port))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def request(path, data=None):
            raw = None if data is None else json.dumps(data).encode()
            headers = {'X-Who-Token': 'owner-test', 'Content-Type': 'application/json'}
            with opener.open(urllib.request.Request(origin + path, headers=headers, data=raw), timeout=10) as response:
                return json.load(response)

        with (core / 'test.log').open('w+', encoding='utf-8') as log:
            worker = subprocess.Popen([sys.executable, str(core / 'reader_server.py')], env=env, stdout=log, stderr=log)
            try:
                for _ in range(150):
                    try:
                        request('/api/ping'); break
                    except OSError:
                        if worker.poll() is not None:
                            log.seek(0); raise AssertionError(log.read())
                        time.sleep(.05)
                else:
                    raise AssertionError('test server did not start')
                for i in range(20):
                    request('/api/proposals/edit', dict(name='test.txt', book_revision=revision,
                        operation='replace', quote=f'第{i:03d}段', replacement=f'修改{i:03d}段'))
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(executable_path=os.environ.get('CHROME_TEST', '/usr/bin/chromium'), headless=True)
                    try:
                        for width in (320, 390, 480, 1280):
                            context = browser.new_context(viewport={'width': width, 'height': 844},
                                is_mobile=width < 600, has_touch=width < 600)
                            page = context.new_page(); errors = []
                            page.on('pageerror', lambda e: errors.append(str(e)))
                            page.goto(origin + '/?who_token=owner-test')
                            page.wait_for_function('typeof state!=="undefined" && state.book && state.segs.length')
                            for identifier in ('btnLib', 'btnToc', 'hlVisibility', 'btnTts', 'btnSearch', 'btnFontDec', 'btnFontInc', 'btnTheme'):
                                rect = page.locator('#' + identifier).bounding_box()
                                assert rect and rect['x'] >= 0 and rect['x'] + rect['width'] <= width, (width, identifier, rect)
                            page.locator('#btnFontInc').click(); page.locator('#btnFontDec').click()
                            page.locator('#btnTheme').click(); page.locator('#btnTheme').click()
                            page.wait_for_timeout(1600)
                            stable = progress.read_bytes(); position = page.evaluate('view.scrollTop')
                            page.locator('#btnLib').click(); page.locator('#btnReview').click()
                            page.locator('.rm-review-card').first.wait_for()
                            assert not page.locator('#libPanel').evaluate('(el)=>el.classList.contains("on")')
                            # Reproduce the original PageDown on the initially focused Close button.
                            page.keyboard.press('PageDown'); page.wait_for_timeout(400)
                            assert page.locator('.rm-review-body').evaluate('(el)=>el.scrollTop') > 0, 'modal cannot scroll'
                            page.locator('.rm-review-body').evaluate('(el)=>{el.tabIndex=0;el.focus()}')
                            for key in ('PageDown', 'PageUp', 'Space', 'ArrowDown', 'ArrowUp', 'Home', 'j', 'k', '/'):
                                page.keyboard.press(key)
                            page.locator('.rm-review-body').evaluate('(el)=>el.removeAttribute("tabindex")')
                            assert not page.locator('#searchPanel').evaluate('(el)=>el.classList.contains("on")')
                            # Tab and Shift+Tab must stay in the modal.
                            page.locator('#reviewClose').focus(); page.keyboard.press('Shift+Tab')
                            assert page.evaluate('reviewDialog.contains(document.activeElement) && document.activeElement.id!=="reviewClose"')
                            page.keyboard.press('Tab')
                            assert page.evaluate('document.activeElement.id==="reviewClose"')
                            # Text editing and native button activation still work.
                            page.locator('#reviewRefresh').focus(); page.keyboard.press('Space')
                            page.wait_for_function('document.querySelectorAll(".rm-review-card").length===20 && document.getElementById("reviewMessage").textContent.includes("20")')
                            page.locator('.rm-review-card').first.get_by_role('button', name='预览并审批').click()
                            field = page.get_by_label('接受内容（可手动调整）').first
                            field.fill('手动 / j k'); field.press('End'); field.press('Space'); field.press('PageDown')
                            assert field.input_value() == '手动 / j k '
                            page.wait_for_timeout(1600)
                            assert page.evaluate('view.scrollTop') == position, 'review scrolled formal text'
                            assert progress.read_bytes() == stable, 'review changed formal progress'
                            page.keyboard.press('Escape')
                            assert page.locator('#reviewDialog').is_hidden()
                            assert page.evaluate('document.activeElement.id==="btnLib"')
                            assert progress.read_bytes() == stable
                            # Reader shortcuts resume normally after closing review.
                            page.keyboard.press('PageDown')
                            page.wait_for_function('(before)=>view.scrollTop>before', arg=position)
                            for _ in range(50):
                                if progress.read_bytes() != stable: break
                                page.wait_for_timeout(100)
                            assert progress.read_bytes() != stable, 'reader progress did not resume'
                            assert not errors, errors
                            context.close()
                            print(f'PASS review UI {width}px: controls, drawer entry, native modal scroll/edit, focus, progress isolation and restored reader keys', flush=True)
                        context = browser.new_context(); page = context.new_page()
                        page.goto(origin + '/?who_token=agent-test')
                        page.wait_for_function('typeof state!=="undefined" && state.book')
                        page.locator('#btnLib').click()
                        assert page.locator('#btnReview').is_hidden()
                        context.close()
                    finally:
                        browser.close()
                for path, original in protected.items():
                    assert path.read_bytes() == original, path.name
                print('PASS review UI: ordinary identity has no review entry; original book, notes and identities preserved', flush=True)
            finally:
                worker.terminate(); worker.wait(timeout=10)


if __name__ == '__main__':
    run()
