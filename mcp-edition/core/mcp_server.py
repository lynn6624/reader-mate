"""Remote co-reading MCP. Credentials live in data/identities.json; never accept who from tools."""
import asyncio
import json
import os
import re
import stat
import urllib.parse
import urllib.request
from mcp.server.fastmcp import FastMCP, Context
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
import uvicorn
import reader_server as reader

KEY_FILE = os.environ.get(
    "READER_MCP_PATH_KEY_FILE",
    os.path.join(os.path.dirname(reader.BASE), "state", "mcp-path-key.txt"))
CHATGPT_KEY_FILE = os.environ.get(
    "READER_MCP_004_KEY_FILE",
    os.path.join(os.path.dirname(reader.BASE), "state", "mcp-004-url-key.txt"))


def load_path_key(filename):
    """Read a server-owned path key; fail closed if absent or too broadly readable."""
    mode = os.stat(filename).st_mode
    if os.name != "nt" and stat.S_IMODE(mode) & 0o077:
        raise RuntimeError("MCP path key file must be owner-only (chmod 600)")
    with open(filename, encoding="ascii") as stream:
        key = stream.read().strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{24}", key):
        raise RuntimeError("MCP path key must contain 24 URL-safe characters")
    return key


MCP_PATH = "/mcp/" + load_path_key(KEY_FILE)
CHATGPT_PATH = "/mcp/" + load_path_key(CHATGPT_KEY_FILE)
if CHATGPT_PATH == MCP_PATH:
    raise RuntimeError("004 URL key must differ from the regular MCP path key")

public_host = os.environ.get("READER_MCP_PUBLIC_HOST", "").strip()
# The existing orange-cloud host remains valid for dsh001/op001. The grey-cloud
# host is used by 004/ChatGPT and must pass FastMCP's Host/Origin checks too.
extra_host = os.environ.get("READER_MCP_EXTRA_HOST", "mcp.example.com").strip()
public_hosts = list(dict.fromkeys(h for h in (public_host, extra_host) if h))
security = TransportSecuritySettings(
    allowed_hosts=["127.0.0.1:*", "localhost:*"] +
                  [entry for host in public_hosts for entry in (host, host + ":443")],
    allowed_origins=["http://127.0.0.1:*", "http://localhost:*"] +
                    ["https://" + host for host in public_hosts])
mcp = FastMCP("ReaderMate", stateless_http=True, json_response=True,
              streamable_http_path=MCP_PATH, transport_security=security)
API = os.environ.get("READER_API_URL", "http://127.0.0.1:8831").rstrip("/")

def bearer(headers):
    value = headers.get("authorization", "")
    return value[7:].strip() if value.startswith("Bearer ") else ""

def credentials(ctx):
    req = ctx.request_context.request
    token = bearer(req.headers) if req else ""
    who = reader.resolve_who(None, token) if token else ""
    if not who:
        raise ValueError("Invalid identity credential")
    return who, token

async def api(ctx, path, payload=None, method=None):
    who, token = credentials(ctx)
    def request():
        headers = {"X-Who-Token": token, "Content-Type": "application/json"}
        if reader.TOKEN:
            headers["X-Token"] = reader.TOKEN
        data = None
        if payload is not None:
            payload["who"] = who
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(API + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    return await asyncio.to_thread(request)

@mcp.tool()
async def list_books(ctx: Context) -> dict:
    """List the shared TXT bookshelf."""
    return await api(ctx, "/api/books")

@mcp.tool()
async def read_book(name: str, ctx: Context, offset: int = 0, length: int = 3000) -> dict:
    """Read a bounded range. Offsets are UTF-16 units, matching the browser. Use next_offset to continue."""
    if offset < 0 or not 1 <= length <= 12000:
        raise ValueError("offset >= 0; length 1..12000")
    book = await api(ctx, "/api/book?" + urllib.parse.urlencode({"name": name}))
    raw = book["text"].encode("utf-16-le")
    if offset * 2 > len(raw):
        raise ValueError("Offset past end of book")
    end = min(len(raw), (offset + length) * 2)
    # Don't split a surrogate pair at the trailing edge; reject invalid input starts.
    try:
        text = raw[offset * 2:end].decode("utf-16-le")
    except UnicodeDecodeError:
        end -= 2
        text = raw[offset * 2:end].decode("utf-16-le")
    return {"name": book["name"], "offset": offset, "next_offset": end // 2,
            "total_units": len(raw) // 2, "text": text, "by_who": book["by_who"]}

@mcp.tool()
async def list_comments(name: str, ctx: Context, chapter_id: str = "",
                        offset: int = 0, limit: int = 200) -> dict:
    """Legacy shared notes reader; paginated. Prefer get_highlights for chapter reads."""
    if offset < 0 or not 1 <= limit <= 200:
        raise ValueError("offset >= 0; limit 1..200")
    return await api(ctx, "/api/notes?" + urllib.parse.urlencode({
        "name": name, "chapter_id": chapter_id, "offset": offset, "limit": limit}))

@mcp.tool()
async def get_highlights(name: str, chapter_id: str, ctx: Context,
                         author_id: str = "", offset: int = 0, limit: int = 100) -> dict:
    """Read all permitted co-readers' highlights in one chapter; optionally filter author. Paginated."""
    if not chapter_id or offset < 0 or not 1 <= limit <= 200:
        raise ValueError("chapter_id required; offset >= 0; limit 1..200")
    return await api(ctx, "/api/notes?" + urllib.parse.urlencode({
        "name": name, "chapter_id": chapter_id, "author_id": author_id,
        "offset": offset, "limit": limit}))

@mcp.tool()
async def get_annotations(name: str, highlight_id: str, ctx: Context) -> dict:
    """Read every co-reader's annotation on a shared highlight, with stable author IDs."""
    return await api(ctx, "/api/notes/annotations?" + urllib.parse.urlencode({
        "name": name, "highlight_id": highlight_id}))

@mcp.tool()
async def add_highlight(name: str, start: int, end: int, text: str, ctx: Context) -> dict:
    """Create a shared highlight using UTF-16 offsets and the exact quoted text."""
    book = await api(ctx, "/api/book?" + urllib.parse.urlencode({"name": name}))
    raw = book["text"].encode("utf-16-le")
    if not 0 <= start < end <= len(raw) // 2 or raw[start*2:end*2].decode("utf-16-le") != text:
        raise ValueError("Quote and range must match the book")
    return await api(ctx, "/api/notes/highlight", {"name": name, "start": start, "end": end, "text": text})

@mcp.tool()
async def add_comment(name: str, highlight_id: str, text: str, ctx: Context) -> dict:
    """Reply under a highlight (existing limit: about 150 Chinese characters, 12 replies)."""
    who, _ = credentials(ctx)
    # Backend comment schema uses user/ai for type; verified identity is supplied by the header.
    def request():
        _, token = credentials(ctx)
        data = {"name": name, "id": highlight_id, "text": text,
                "who": "user" if who == "user" else "ai"}
        headers = {"X-Who-Token": token, "X-Token": reader.TOKEN, "Content-Type": "application/json"}
        req = urllib.request.Request(API + "/api/notes/comment", data=json.dumps(data).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    return await asyncio.to_thread(request)

@mcp.tool()
async def delete_highlight(name: str, highlight_id: str, ctx: Context) -> dict:
    """Delete only your own highlight; refuse if another co-reader has annotated it."""
    return await api(ctx, "/api/notes/highlight?" + urllib.parse.urlencode({
        "name": name, "id": highlight_id}), method="DELETE")

@mcp.tool()
async def update_progress(name: str, offset: int, ctx: Context) -> dict:
    """Update only the authenticated reader's progress; keep their listening position."""
    book = await api(ctx, "/api/book?" + urllib.parse.urlencode({"name": name}))
    who, _ = credentials(ctx)
    raw = book["text"].encode("utf-16-le")
    units = len(raw) // 2
    if not 0 <= offset <= units:
        raise ValueError("Offset outside book")
    raw[:offset*2].decode("utf-16-le")  # reject mid-surrogate offsets
    prior = book["by_who"].get(who, {})
    anchor = raw[offset*2:].decode("utf-16-le")[:40]
    return await api(ctx, "/api/progress", {"name": name, "offset": offset,
        "chars": units, "percent": offset / units if units else 0,
        "anchor": anchor, "tts_offset": prior.get("tts_offset", 0)})

class IdentityGate:
    def __init__(self, app):
        self.app = app
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            # Reject the old bare endpoint before revealing whether a bearer is valid.
            if scope.get("path") != MCP_PATH:
                return await JSONResponse({"error": "Not found"}, status_code=404)(scope, receive, send)
            headers = {k.decode(): v.decode() for k, v in scope["headers"]}
            token = bearer(headers)
            if not token or not reader.resolve_who(None, token):
                response = JSONResponse({"error": "Unauthorized"}, status_code=401)
                return await response(scope, receive, send)
        await self.app(scope, receive, send)

class ChatGPT004Gateway:
    """Exchange the grey-host URL credential for 004's existing identity token.

    The core gate and tools still authenticate by Bearer token. This narrow
    adapter is only for ChatGPT's custom connector, which supplies a URL but
    cannot supply our fixed Bearer token.
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == CHATGPT_PATH:
            hosts = [v.decode("ascii", "ignore").lower() for k, v in scope["headers"]
                     if k.lower() == b"host"]
            if len(hosts) != 1 or hosts[0] not in (extra_host, extra_host + ":443"):
                return await JSONResponse({"error": "Not found"}, status_code=404)(scope, receive, send)
            token = reader.load_identities().get("004", "")
            if not token or reader.resolve_who(None, token) != "004":
                return await JSONResponse({"error": "Unauthorized"}, status_code=401)(scope, receive, send)
            scope = dict(scope)
            scope["path"] = MCP_PATH
            scope["raw_path"] = MCP_PATH.encode("ascii")
            scope["headers"] = [(k, v) for k, v in scope["headers"] if k.lower() != b"authorization"]
            scope["headers"].append((b"authorization", b"Bearer " + token.encode("utf-8")))
        await self.app(scope, receive, send)

app = ChatGPT004Gateway(IdentityGate(mcp.streamable_http_app()))
if __name__ == "__main__":
    if not reader.CLOUD or "user" not in reader.load_identities():
        raise SystemExit("Set READER_CLOUD=1 and configure data/identities.json first")
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("READER_MCP_PORT", "8832")),
                access_log=False)
