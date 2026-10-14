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
    with tempfile.TemporaryDirectory() as temp:
        base = pathlib.Path(temp)
        shutil.copy(ROOT / "reader_server.py", base)
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

        def upload(name, raw, token="test-owner"):
            return call("/api/import?name=" + urllib.parse.quote(name), token, raw)

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
            print("PASS: TXT encoding, EPUB conversion, duplicate protection, invalid content and owner-only access")
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
