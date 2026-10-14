"""Isolated HTTP regression checks for token setup and identity writes."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def main():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        (data_dir / ".token-bootstrap").write_text("bootstrap-for-test\n", encoding="ascii")
        key_file = data_dir / "mcp-path-key.txt"
        key_file.write_text("T" * 24, encoding="ascii")
        key_file.chmod(0o600)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = dict(os.environ, READER_DATA_DIR=tmp, TOKEN_PORT=str(port),
                   READER_MCP_PATH_KEY_FILE=str(key_file))
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("server.py"))],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        base = f"http://127.0.0.1:{port}"

        def request(method, path, body=None):
            data = None if body is None else json.dumps(body).encode("utf-8")
            req = urllib.request.Request(
                base + path, data=data, method=method,
                headers={"Content-Type": "application/json"} if data else {},
            )
            try:
                with urllib.request.urlopen(req, timeout=2) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as exc:
                return exc.code, json.load(exc)

        try:
            for _ in range(50):
                try:
                    status, result = request("GET", "/api/status")
                    break
                except (urllib.error.URLError, TimeoutError):
                    time.sleep(0.05)
            else:
                raise AssertionError("token server did not start")
            assert status == 200 and result["configured"] is False
            assert "mcp_url" not in result
            assert request("POST", "/api/setup", {
                "bootstrap": "wrong", "password": "correct horse battery",
            })[0] == 403
            assert request("POST", "/api/setup", {
                "bootstrap": "bootstrap-for-test", "password": "correct horse battery",
            })[0] == 200
            assert not (data_dir / ".token-bootstrap").exists()
            assert request("POST", "/api/setup", {
                "bootstrap": "bootstrap-for-test", "password": "another password",
            })[0] == 409
            assert request("POST", "/api/identities", {
                "name": "test004", "password": "wrong password",
            })[0] == 403
            status, result = request("POST", "/api/identities", {
                "name": "test004", "password": "correct horse battery",
            })
            assert status == 200 and len(result["token"]) == 43
            assert result["mcp_url"] == "https://reader.example.com/mcp/" + "T" * 24
            assert "mcp_url" not in request("GET", "/api/status")[1]
            assert request("POST", "/api/identities", {
                "name": "test004", "password": "correct horse battery",
            })[0] == 409
            assert request("POST", "/api/identities", {
                "name": "中文", "password": "correct horse battery",
            })[0] == 400
            key_file.unlink()
            assert request("POST", "/api/identities", {
                "name": "test005", "password": "correct horse battery",
            })[0] == 503
            assert "test005" not in request("GET", "/api/status")[1]["identities"]
            admin = data_dir / ".token-admin.json"
            admin.write_text("{broken", encoding="utf-8")
            status, result = request("GET", "/api/status")
            assert status == 200 and result["configured"] and result["admin_error"]
            assert request("POST", "/api/setup", {
                "bootstrap": "bootstrap-for-test", "password": "another password",
            })[0] == 409
            print("PASS: one-time setup, fail-closed admin, identity auth and validation")
        finally:
            proc.terminate()
            proc.wait(timeout=5)


if __name__ == "__main__":
    main()
