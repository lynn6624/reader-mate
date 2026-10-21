#!/usr/bin/env python3
"""Exercise book uploads through a disposable cloud-mode HTTP server."""
import io
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from reader_server import assign_book_category


ROOT = pathlib.Path(__file__).parent


def make_epub():
    container = ('<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                 '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
    opf = ('<package xmlns="http://www.idpf.org/2007/opf">'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           '<dc:title>测试电子书</dc:title></metadata>'
           '<manifest><item id="c1" href="ch1.xhtml"/></manifest>'
           '<spine><itemref idref="c1"/></spine></package>')
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/ch1.xhtml", '<html><body><p>电子书正文。</p></body></html>')
    return output.getvalue()


def main():
    metadata = {}
    assert assign_book_category(metadata, "literature", source="auto")
    assert assign_book_category(metadata, "nonfiction", author="user")
    manual = dict(metadata)
    assert not assign_book_category(metadata, "web-fiction", source="auto")
    assert metadata == manual
    with tempfile.TemporaryDirectory() as temp:
        base = pathlib.Path(temp)
        shutil.copy(ROOT / "reader_server.py", base)
        shutil.copy(ROOT / "chapter_index.py", base)
        shutil.copy(ROOT / "navigation.py", base)
        shutil.copy(ROOT / "progress_catalog.py", base)
        shutil.copytree(ROOT / "web", base / "web")
        shutil.copytree(ROOT / "tools", base / "tools")
        (base / "data").mkdir()
        (base / "书库").mkdir()
        (base / "data" / "identities.json").write_text(
            json.dumps({"user": "test-owner", "004": "test-reader"}), encoding="utf-8")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = dict(os.environ, READER_CLOUD="1", READER_HOST="127.0.0.1",
                   READER_PORT=str(port))
        log = open(base / "server.log", "w+", encoding="utf-8")
        proc = subprocess.Popen([sys.executable, str(base / "reader_server.py")],
                                env=env, stdout=log, stderr=log)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        url = "http://127.0.0.1:%d" % port

        def call(path, token="test-owner", body=None):
            headers = {"X-Who-Token": token} if token else {}
            req = urllib.request.Request(url + path, data=body, headers=headers)
            try:
                with opener.open(req, timeout=15) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as error:
                return error.code, json.load(error)

        def upload(name, raw, token="test-owner", category=None):
            suffix = "" if category is None else "&category=" + urllib.parse.quote(category)
            return call("/api/import?name=" + urllib.parse.quote(name) + suffix, token, raw)

        def categorize(name, category, token="test-owner"):
            return call("/api/books/category", token, json.dumps({"name":name, "category":category}).encode())

        try:
            for _ in range(100):
                try:
                    if call("/api/ping", token=None)[0] == 200:
                        break
                except (OSError, urllib.error.URLError):
                    pass
                time.sleep(.1)
            else:
                raise AssertionError("test server did not start")

            assert upload("a.txt", b"hello")[0] == 200
            assert (base / "书库" / "a.txt").read_text(encoding="utf-8") == "hello"
            assert upload("a.txt", b"replace")[0] == 409
            assert (base / "书库" / "a.txt").read_text(encoding="utf-8") == "hello"
            assert upload("reader.txt", b"forbidden", "test-reader")[0] == 403
            assert upload("anon.txt", b"forbidden", "")[0] == 401
            assert call("/api/books")[1]["books"][0]["category"] == "uncategorized"
            assert upload("bad-category.txt", b"hello", category="invented")[0] == 400
            assert upload("blank-category.txt", b"hello", category="")[0] == 400
            assert upload("novel.txt", b"hello", category="literature")[0] == 200
            assert upload("manual.txt", b"hello", category="web-fiction")[0] == 200
            assert categorize("a.txt", "nonfiction", "test-reader")[0] == 403
            assert categorize("a.txt", "nonfiction", "")[0] == 401
            assert categorize("a.txt", "bad")[0] == 400
            assert categorize("a.txt", {"bad":True})[0] == 400
            assert categorize("missing.txt", "nonfiction")[0] == 404
            assert categorize("a.txt", "nonfiction")[0] == 200
            listing = call("/api/books")[1]
            assert listing["can_manage"] and len(listing["categories"]) == 6
            labels = {c['id']: c['name'] for c in listing['categories']}
            assert labels['nonfiction'] == '随笔与纪实' and labels['humanities'] == '人文社科'
            assert next(b for b in listing['books'] if b['name'] == 'a.txt')['category'] == 'nonfiction'
            prior_category = {key:value for key,value in json.loads((base / 'data/library.json').read_text(encoding='utf-8'))['books']['a.txt'].items() if key.startswith('category')}
            assert upload("research.txt", b"research", category="humanities")[0] == 200
            assert [b['name'] for b in call('/api/books?category=humanities')[1]['books']] == ['research.txt']
            after_category = {key:value for key,value in json.loads((base / 'data/library.json').read_text(encoding='utf-8'))['books']['a.txt'].items() if key.startswith('category')}
            assert after_category == prior_category
            assert not call("/api/books", "test-reader")[1]["can_manage"]
            by_name = {book["name"]:book for book in listing["books"]}
            assert by_name["a.txt"]["category_source"] == "manual"
            assert sum(c["count"] for c in listing["categories"]) == len(listing["books"])
            filtered = call("/api/books?category=literature")[1]
            assert [book["name"] for book in filtered["books"]] == ["novel.txt"]
            assert call("/api/books?category=unknown")[0] == 400
            # Category edits must preserve the physical file, highlights, and progress.
            book = call("/api/book?name=a.txt")[1]
            assert call("/api/progress", body=json.dumps({"name":"a.txt", "offset":2,
                "expected_revision":0, "book_revision":book["book_revision"]}).encode())[0] == 200
            assert call("/api/notes/highlight", body=json.dumps({"name":"a.txt", "start":0,
                "end":2, "text":"he"}).encode())[0] == 200
            before = {file:(base / file).read_bytes() for file in
                      ("书库/a.txt", "data/progress.json", "data/notes.json")}
            assert categorize("a.txt", "science-tools")[0] == 200
            for file, raw in before.items():
                assert (base / file).read_bytes() == raw
            # A direct filesystem import defaults to pending; re-scan retains metadata.
            external = base / "书库/external.txt"
            external.write_text("external", encoding="utf-8")
            assert next(b for b in call("/api/books")[1]["books"] if b["name"] == external.name)["category"] == "uncategorized"
            assert categorize(external.name, "nonfiction")[0] == 200
            external.write_text("external changed", encoding="utf-8")
            scanned = next(b for b in call("/api/books")[1]["books"] if b["name"] == external.name)
            assert scanned["category"] == "nonfiction" and scanned["category_source"] == "manual"
            # Force metadata write failure in the disposable directory: no new file/record survives.
            (base / "data/library.json.tmp").mkdir()
            assert upload("write-fails.txt", b"hello", category="literature")[0] == 500
            (base / "data/library.json.tmp").rmdir()
            assert not (base / "书库/write-fails.txt").exists()
            metadata = json.loads((base / "data/library.json").read_text(encoding="utf-8"))
            assert "write-fails.txt" not in metadata["books"]
            assert "bad-category.txt" not in metadata["books"]

            gbk = "第一章\n旧书正文。".encode("gb18030")
            status, result = upload("老书.txt", gbk)
            assert status == 200 and result["encoding"] == "gb18030", result
            assert (base / "书库" / "老书.txt").read_text(encoding="utf-8") == "第一章\n旧书正文。"
            status, result = upload("Unicode.txt", "繁體正文。".encode("utf-16"))
            assert status == 200 and result["encoding"] == "utf-16", result
            assert (base / "书库" / "Unicode.txt").read_text(encoding="utf-8") == "繁體正文。"

            status, result = upload("故事.epub", make_epub())
            assert status == 200 and result["name"] == "故事.txt", result
            assert "电子书正文" in (base / "书库" / "故事.txt").read_text(encoding="utf-8")
            assert upload("故事.epub", make_epub())[0] == 409
            assert upload("假书.epub", b"not a zip")[0] == 400
            assert upload("压缩包.txt", make_epub())[0] == 400
            assert upload("未知.pdf", b"hello")[0] == 400
            assert upload("二进制.txt", b"a\x00b")[0] == 400
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w") as zf:
                for number in range(4097):
                    zf.writestr("part-%04d" % number, "")
            assert upload("太多文件.epub", archive.getvalue())[0] == 400
            print("PASS: TXT/EPUB uploads, required/legacy categories, filters, owner-only edits, metadata failure rollback, progress/notes preservation")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            log.close()


if __name__ == "__main__":
    main()
