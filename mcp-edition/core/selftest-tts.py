"""Disposable mobile-browser check for selection TTS continuation and following."""
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
SENTENCES = [f"第{i}句文字要连续朗读，直到下一句开始才会结束。" for i in range(1, 45)]
BOOK = "第一章\n" + "".join(SENTENCES) + "\n末尾还有下一段内容。"
MOCK_SPEECH = """() => {
  const utterances = [];
  const synth = {
    speaking: false, pending: false, paused: false, acceptNext: false,
    getVoices: () => [],
    speak(u) {
      if (utterances.length && !this.acceptNext) { u.onerror?.({error: 'not-allowed'}); return; }
      utterances.push(u); this.speaking = true; u.onstart?.();
    },
    pause() { this.paused = true; },
    resume() { this.paused = false; },
    cancel() { this.speaking = false; this.paused = false; },
  };
  window.__spoken = utterances;
  window.__finish = () => {
    const u = utterances.at(-1);
    synth.speaking = false; synth.acceptNext = true;
    u.onend?.();
    synth.acceptNext = false;
  };
  window.__synth = synth;
  window.SpeechSynthesisUtterance = class { constructor(text) { this.text = text; } };
  Object.defineProperty(window, 'speechSynthesis', {configurable: true, value: synth});
}"""


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


with tempfile.TemporaryDirectory() as directory:
    base = pathlib.Path(directory)
    shutil.copy(ROOT / "reader_server.py", base / "reader_server.py")
    shutil.copytree(ROOT / "web", base / "web")
    (base / "data").mkdir()
    (base / "书库").mkdir()
    (base / "data" / "identities.json").write_text(json.dumps({"user": "test-user"}))
    (base / "书库" / "test.txt").write_text(BOOK, encoding="utf-8")
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    env = dict(os.environ, READER_CLOUD="1", READER_PORT=str(port))
    process = subprocess.Popen([sys.executable, str(base / "reader_server.py")],
                               env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(urllib.request.Request(
                    url + "/api/books", headers={"X-Who-Token": "test-user"}), timeout=1)
                break
            except Exception:
                time.sleep(.1)
        else:
            raise AssertionError("HTTP server did not start")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=CHROME, headless=True)
            try:
                context = browser.new_context(viewport={"width": 390, "height": 844},
                                              is_mobile=True, has_touch=True)
                context.add_init_script("(" + MOCK_SPEECH + ")()")
                page = context.new_page()
                page.goto(url + "/?who_token=test-user", wait_until="domcontentloaded")
                page.wait_for_function("state.book && state.book.name === 'test.txt'")
                picked = page.evaluate("""() => {
                    const el = state.els[1];
                    const node = el.firstChild;
                    const at = node.textContent.indexOf('第2句');
                    const range = document.createRange();
                    range.setStart(node, at + 1); range.setEnd(node, at + 2);
                    const sel = window.getSelection();
                    sel.removeAllRanges(); sel.addRange(range);
                    onSelectionChange();
                    return {menu: document.querySelector('#hlMenu').classList.contains('on'), at};
                }""")
                assert picked["menu"] and picked["at"] > 0, picked
                page.locator("#hlMenu [data-act=read]").tap()
                page.wait_for_function("window.__spoken.length === 1")
                first = page.evaluate("""() => ({
                    text: __spoken[0].text, offset: state.ttsOffset,
                    start: state.segs[1].start
                })""")
                assert first["text"] == SENTENCES[1], first
                assert first["offset"] == first["start"] + len(SENTENCES[0]), first
                # A mobile browser can report speaking=false while still speaking.
                # This must never cut the sentence short or advance without end.
                page.evaluate("__synth.speaking = false")
                page.wait_for_timeout(1300)
                assert page.evaluate("window.__spoken.length") == 1
                page.evaluate("__finish()")
                page.wait_for_function("window.__spoken.length === 2", timeout=3000)
                assert page.evaluate("__spoken[1].text") == SENTENCES[2]
                max_scroll = 0
                for count in range(3, 25):
                    page.evaluate("__finish()")
                    page.wait_for_function("n => window.__spoken.length === n", arg=count)
                    max_scroll = max(max_scroll, page.evaluate("document.querySelector('#view').scrollTop"))
                assert max_scroll > 0, "mobile viewport did not follow the spoken sentence"
                assert page.evaluate("state.ttsOffset") > first["offset"]
                page.locator("#btnTtsStop").tap()
                page.wait_for_function("tts.state === 'idle'")
                assert page.evaluate("window.__spoken.length") == 24
                context.close()
            finally:
                browser.close()
        print("PASS: mobile selection starts at sentence, waits for end, queues next sentence synchronously, follows within paragraph")
    finally:
        process.terminate()
        process.wait(timeout=10)
