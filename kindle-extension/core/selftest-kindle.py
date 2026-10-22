"""Disposable HTTP checks for Kindle pairing; never touches the real bookshelf."""
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from html import unescape
from urllib.parse import urlparse

import httpx

ROOT = pathlib.Path(__file__).parent


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    with tempfile.TemporaryDirectory() as directory:
        base = pathlib.Path(directory)
        shutil.copy(ROOT / "reader_server.py", base / "reader_server.py")
        shutil.copy(ROOT / "chapter_index.py", base / "chapter_index.py")
        shutil.copy(ROOT / "navigation.py", base / "navigation.py")
        shutil.copy(ROOT / "progress_catalog.py", base / "progress_catalog.py")
        shutil.copytree(ROOT / "web", base / "web")
        shutil.copytree(ROOT / "extensions", base / "extensions")
        (base / "data").mkdir()
        (base / "书库").mkdir()
        (base / "data" / "identities.json").write_text(
            json.dumps({"user": "owner-secret", "004": "other-secret"}), encoding="utf-8")
        text = "开头😀\n" + "甲乙丙丁戊己庚辛壬癸。\n" * 300
        (base / "书库" / "book.txt").write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
        port = free_port()
        url = f"http://127.0.0.1:{port}"
        env = dict(os.environ, READER_CLOUD="1", READER_HOST="127.0.0.1",
                   READER_PORT=str(port), READER_MCP_PUBLIC_HOST="reader.example.com")
        process = subprocess.Popen([sys.executable, str(base / "reader_server.py")],
                                   env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=5) as client:
                for _ in range(100):
                    try:
                        if client.get(url + "/api/ping").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(.1)
                else:
                    raise AssertionError("server did not start: " + process.stderr.read().decode())
                assert client.post(url + "/api/kindle/pairings").status_code == 401
                assert client.post(url + "/api/kindle/pairings",
                                   headers={"X-Who-Token": "other-secret"}).status_code == 403
                owner = {"X-Who-Token": "owner-secret"}
                revision = client.get(url + "/api/book?name=book.txt", headers=owner).json()["book_revision"]
                saved_offset = len(text[:505].encode("utf-16-le")) // 2
                saved = client.post(url + "/api/progress", headers=owner, json={
                    "name": "book.txt", "offset": saved_offset, "expected_revision": 0,
                    "book_revision": revision})
                assert saved.status_code == 200, saved.text
                pairing = client.post(url + "/api/kindle/pairings", headers=owner)
                assert pairing.status_code == 201, pairing.text
                link = pairing.json()["url"]
                assert link.startswith("https://reader.example.com/k/")
                code_path = urlparse(link).path
                form = client.get(url + code_path)
                assert form.status_code == 200 and "owner-secret" not in form.text
                assert "no-referrer" == form.headers["Referrer-Policy"]
                bound = client.post(url + code_path)
                assert bound.status_code == 303 and "HttpOnly" in bound.headers["Set-Cookie"]
                assert "Secure" in bound.headers["Set-Cookie"]
                cookie = bound.headers["Set-Cookie"].split(";", 1)[0]
                assert client.post(url + code_path).status_code == 410
                device = {"Cookie": cookie}
                identity = client.get(url + "/api/auth-check", headers=device)
                assert identity.status_code == 200 and identity.json()["who"] == "user"
                assert client.get(url + "/api/auth-check", headers=dict(device, **{"X-Who-Token":"invalid"})).status_code == 401
                assert client.get(url + "/api/auth-check?who_token=invalid", headers=device).status_code == 401
                assert client.get(url + "/api/books", headers=device).status_code == 200
                assert client.get(url + "/", headers=device).headers["Location"] == "/kindle"
                full_page = client.get(url + "/?full=1&kindle=1", headers=device)
                assert full_page.status_code == 200 and 'href="/kindle"' in full_page.text
                assert 'href="/kindle"' not in client.get(url + "/?full=1", headers=owner).text
                shelf = client.get(url + "/kindle", headers=device)
                assert shelf.status_code == 200 and shelf.text.count('class=shelf-title') == 6
                assert '人文社科' in shelf.text and '随笔与纪实' in shelf.text
                assert 'class=book href=' not in shelf.text
                assert client.post(url + '/api/books/category', headers=owner,
                    json={'name':'book.txt', 'category':'literature'}).status_code == 200
                assert client.post(url + '/api/books/category', headers=device,
                    json={'name':'book.txt', 'category':'nonfiction'}).status_code == 403
                opened = client.get(url + '/kindle?open=literature&open=humanities', headers=device)
                assert 'class=book href=' in opened.text and '暂无书籍' in opened.text, (opened.status_code, opened.text[-3000:])
                found = client.get(url + '/kindle?q=book', headers=device)
                assert found.text.count('class=shelf-title') == 1 and 'class=book href=' in found.text
                reading = client.get(url + "/kindle?name=book.txt", headers=device)
                pct = 100 * saved_offset / (len(text.encode("utf-16-le")) // 2)
                assert reading.status_code == 200 and f"{pct:.1f}%" in reading.text
                assert text[505:525] in reading.text and "开头😀" not in reading.text
                assert "阅读字号：" in reading.text and "甲乙丙丁戊己庚辛壬癸" in reading.text
                assert "font:18px/1.4 serif" in reading.text
                assert len(unescape(re.search(r"<div class=text>(.*?)</div>", reading.text, re.S).group(1))) == 440
                large = client.get(url + "/kindle?name=book.txt&at=" + str(saved_offset) + "&n=340", headers=device)
                assert large.status_code == 200 and "font:20px/1.4 serif" in large.text
                assert len(unescape(re.search(r"<div class=text>(.*?)</div>", large.text, re.S).group(1))) == 340
                extra = client.get(url + "/kindle?name=book.txt&at=" + str(saved_offset) + "&n=280", headers=device)
                assert extra.status_code == 200 and "font:22px/1.4 serif" in extra.text
                assert len(unescape(re.search(r"<div class=text>(.*?)</div>", extra.text, re.S).group(1))) == 280
                first_page = client.get(url + "/kindle?name=book.txt&at=0&n=340", headers=device)
                first_text = unescape(re.search(r"<div class=text>(.*?)</div>", first_page.text, re.S).group(1))
                second_at = len(first_text.encode("utf-16-le")) // 2
                second_page = client.get(url + f"/kindle?name=book.txt&at={second_at}&n=340", headers=device)
                second_text = unescape(re.search(r"<div class=text>(.*?)</div>", second_page.text, re.S).group(1))
                assert first_text + second_text == text[:680]
                assert client.get(url + "/api/reading-progress?name=book.txt", headers=owner).json()["progress"]["offset"] == saved_offset
                forms = re.findall(r"<form method=post action='/kindle/page'>(.*?)</form>",
                                   reading.text, re.S)
                assert len(forms) == 2
                next_fields = {key: unescape(value) for key, value in
                               re.findall(r"name='([^']+)' value='([^']*)'", forms[1])}
                assert next_fields["target"] != str(saved_offset)
                assert next_fields["page_chars"] == "440"
                bad_fields = dict(next_fields, csrf="bad")
                assert client.post(url + "/kindle/page", headers=device, data=bad_fields).status_code == 403
                assert client.post(url + "/kindle/page", headers=device,
                                   data=dict(next_fields, page_chars="340")).status_code == 403
                assert client.post(url + "/kindle/page", headers=device,
                                   data=dict(next_fields, paused="1")).status_code == 403
                turned = client.post(url + "/kindle/page", headers=device, data=next_fields)
                assert turned.status_code == 303 and turned.headers["Location"].startswith("/kindle?")
                assert "n=440" in turned.headers["Location"]
                result = client.get(url + "/api/reading-progress?name=book.txt", headers=owner).json()
                assert result["progress"]["offset"] == int(next_fields["target"])
                assert result["progress"]["reader_id"] == "user"
                # The current page's form must not overwrite a newer phone save.
                phone = client.post(url + "/api/progress", headers=owner, json={
                    "name": "book.txt", "offset": saved_offset,
                    "expected_revision": result["revision"], "book_revision": revision})
                assert phone.status_code == 200
                conflict = client.post(url + "/kindle/page", headers=device, data=next_fields)
                assert conflict.status_code == 303 and "p=1" in conflict.headers["Location"]
                paused_page = client.get(url + conflict.headers["Location"], headers=device)
                assert paused_page.status_code == 200 and "暂未同步" in paused_page.text
                assert text[945:965] in paused_page.text
                assert client.get(url + "/api/reading-progress?name=book.txt",
                                  headers=owner).json()["progress"]["offset"] == saved_offset
                paused_forms = re.findall(r"<form method=post action='/kindle/page'>(.*?)</form>",
                                          paused_page.text, re.S)
                assert len(paused_forms) == 3
                paused_next = {key: unescape(value) for key, value in
                               re.findall(r"name='([^']+)' value='([^']*)'", paused_forms[2])}
                assert paused_next["paused"] == "1"
                local_turn = client.post(url + "/kindle/page", headers=device, data=paused_next)
                assert local_turn.status_code == 303 and "p=1" in local_turn.headers["Location"]
                assert client.get(url + "/api/reading-progress?name=book.txt",
                                  headers=owner).json()["progress"]["offset"] == saved_offset
                local_page = client.get(url + local_turn.headers["Location"], headers=device)
                resume_form = re.findall(r"<form method=post action='/kindle/page'>(.*?)</form>",
                                         local_page.text, re.S)[0]
                resume_fields = {key: unescape(value) for key, value in
                                 re.findall(r"name='([^']+)' value='([^']*)'", resume_form)}
                assert resume_fields["paused"] == "0"
                resumed = client.post(url + "/kindle/page", headers=device, data=resume_fields)
                assert resumed.status_code == 303 and "p=1" not in resumed.headers["Location"]
                assert client.get(url + "/api/reading-progress?name=book.txt",
                                  headers=owner).json()["progress"]["offset"] == int(resume_fields["target"])
                assert client.get(url + "/api/audit", headers=device).status_code == 403
                assert client.get(url + "/api/kindle/sessions", headers=device).status_code == 403
                assert client.post(url + "/api/import?name=no.txt", headers=device).status_code == 403
                assert client.post(url + "/api/progress", headers=device).status_code == 403
                same_origin = dict(device, Origin="https://reader.example.com")
                assert client.post(url + "/api/progress", headers=same_origin).status_code != 403
                sessions = client.get(url + "/api/kindle/sessions", headers=owner).json()["sessions"]
                assert len(sessions) == 1
                session_id = sessions[0]["session_id"]
                raw_store = (base / "data" / "kindle_auth.json").read_text(encoding="utf-8")
                assert cookie not in raw_store and code_path.rsplit("/", 1)[-1] not in raw_store
                process.terminate()
                process.wait(timeout=5)
                process = subprocess.Popen([sys.executable, str(base / "reader_server.py")],
                                           env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                for _ in range(100):
                    try:
                        if client.get(url + "/api/auth-check", headers=device).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(.1)
                else:
                    raise AssertionError("device session did not survive server restart")
                assert client.delete(url + "/api/kindle/sessions/" + session_id,
                                     headers=owner).status_code == 200
                assert client.get(url + "/api/auth-check", headers=device).status_code == 401
                print("Kindle pairing + touch pages: resume, turn/save, conflict, CSRF, restart, revoke OK")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    main()
