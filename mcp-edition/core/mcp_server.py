"""Remote co-reading MCP. Credentials live in data/identities.json; never accept who from tools."""
import asyncio
import json
import os
import re
import stat
import urllib.error
import urllib.parse
import urllib.request
from typing import Literal
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
              streamable_http_path=MCP_PATH, transport_security=security,
              instructions=("先调用 get_calling_guide 查看标准调用顺序。read_book 只读取正文，"
                            "不会自动保存进度；实际读完后调用 update_progress 保存。"))
API = os.environ.get("READER_API_URL", "http://127.0.0.1:8831").rstrip("/")
GUIDE_FILE = os.path.join(os.path.dirname(__file__), reader.REFERENCE_BOOK_NAME)

def bearer(headers):
    value = headers.get("authorization", "")
    return value[7:].strip() if value.startswith("Bearer ") else ""

def credentials(ctx):
    req = ctx.request_context.request
    token = bearer(req.headers) if req else ""
    who = (reader.channel_identity(token, "mcp") or reader.channel_identity(token, "mcp-url")) if token else ""
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
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                detail = json.load(exc)
            except (ValueError, OSError):
                detail = {}
            raise ValueError("HTTP %s: %s%s" % (
                exc.code, detail.get("error", exc.reason),
                " (current_revision=%s)" % detail["current_revision"]
                if "current_revision" in detail else "")) from exc
    return await asyncio.to_thread(request)

@mcp.tool()
async def get_calling_guide(ctx: Context) -> dict:
    """Read the standard MCP calling guide before using bookshelf, notes, or progress tools. Read-only."""
    credentials(ctx)
    with open(GUIDE_FILE, encoding="utf-8") as stream:
        return {"name": reader.REFERENCE_BOOK_NAME, "text": stream.read()}

@mcp.tool()
async def list_books(ctx: Context, category: Literal["", "literature", "web-fiction", "nonfiction",
                                                    "science-tools", "humanities", "uncategorized"] = "") -> dict:
    """List shared books and six categories/counts. Omit category for all; pass a returned category ID to filter.

    Call get_calling_guide first if the workflow is unfamiliar. Category IDs never change book names/positions.
    Progress belongs to authenticated viewer_id (scope=self); unverified/missing saves use null, never another reader.
    """
    return await api(ctx, "/api/books?" + urllib.parse.urlencode({"category": category, "progress_scope":"self"}))

@mcp.tool()
async def read_book(name: str, ctx: Context, offset: int = 0, length: int = 3000) -> dict:
    """Read a bounded UTF-16 range. Use next_offset to continue; call update_progress to save, never automatic."""
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
            "total_units": len(raw) // 2, "text": text,
            "book_revision": book["book_revision"], "by_who": book["by_who"],
            "reminder": ("阅读进度不会随 read_book 自动保存。实际读完后请调用 "
                         "update_progress(name, offset, expected_revision)；"
                         "完整读完本次 text 才可用 next_offset，否则传实际停下的位置。"
                         "expected_revision 建议先从 get_reading_progress 取得。")}

@mcp.tool()
async def get_reading_progress(name: str, ctx: Context, reader_id: str = "") -> dict:
    """Get your saved position by default, or another member's position in this shared bookshelf."""
    return await api(ctx, "/api/reading-progress?" + urllib.parse.urlencode({
        "name": name, "reader_id": reader_id}))

@mcp.tool()
async def list_reading_progress(ctx: Context, name: str = "", reader_id: str = "") -> dict:
    """With name: existing per-book co-reader positions. Omit name: resume saved books.

    Cross-book defaults to your authenticated identity (latest 20, no pagination).
    Another reader returns only their latest valid save. Target user excludes <1%
    before choosing the latest. Legacy revision=0 may be valid; queries never save.
    Times include +08:00 (Asia/Shanghai). No limit/page/threshold bypass parameters.
    """
    if name:
        return await api(ctx, "/api/reading-progress?" + urllib.parse.urlencode({
            "name": name, "all": "1"}))
    return await api(ctx, "/api/reading-progress/resume?" + urllib.parse.urlencode({"reader_id":reader_id}))

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
async def update_progress(name: str, offset: int, ctx: Context,
                          expected_revision: int = -1) -> dict:
    """Save your UTF-16 position. Pass revision from get_reading_progress to reject stale writes.

    Legacy callers may omit expected_revision; then the tool reads the current
    revision immediately before writing. Explicit backward moves are allowed.
    """
    book = await api(ctx, "/api/book?" + urllib.parse.urlencode({"name": name}))
    who, _ = credentials(ctx)
    raw = book["text"].encode("utf-16-le")
    units = len(raw) // 2
    if not 0 <= offset <= units:
        raise ValueError("Offset outside book")
    raw[:offset*2].decode("utf-16-le")  # reject mid-surrogate offsets
    prior = book["by_who"].get(who, {})
    if expected_revision < 0:
        expected_revision = int(prior.get("revision") or 0)
    anchor = raw[offset*2:].decode("utf-16-le")[:40]
    return await api(ctx, "/api/progress", {"name": name, "offset": offset,
        "chars": units, "percent": offset / units if units else 0,
        "anchor": anchor, "tts_offset": prior.get("tts_offset", 0),
        "book_revision": book["book_revision"], "expected_revision": expected_revision})

@mcp.tool()
async def propose_edit(name: str, book_revision: str, operation: Literal['replace','delete','insert_before','insert_after'],
                       quote: str, ctx: Context, replacement: str = '', reason: str = '',
                       start: int = -1, context_before: str = '', context_after: str = '') -> dict:
    """Submit a pending revision to formal text read with read_book; only a human administrator can accept it.

    Exact quote is located by the server. Repeated quotes require adjacent context or an explicit UTF-16 start.
    Insertions use a nonempty quote as the before/after anchor. Identical submissions reuse the same proposal ID.
    Suggest clear errors/ads; flag uncertain literary wording in reason, never invent or rewrite plot.
    This does not change the shared administrator draft, formal TXT, annotations or reading progress.
    """
    return await api(ctx, '/api/proposals/edit', dict(name=name,book_revision=book_revision,operation=operation,
        quote=quote,replacement=replacement,reason=reason,start=start,context_before=context_before,context_after=context_after))


@mcp.tool()
async def propose_chapter(name: str, book_revision: str, operation: Literal['add','update','delete'],
                          ctx: Context, quote: str = '', title: str = '', chapter_id: str = '',
                          target_title: str = '', parent_title: str | None = None, reason: str = '',
                          start: int = -1, context_before: str = '', context_after: str = '') -> dict:
    """Propose a chapter change for administrator review, never change a directory directly.

    Add uses a selected quote; update/delete identify an existing formal chapter by ID or unique target_title.
    For update, omit quote to keep its position. Omit parent_title to retain the parent; empty means root.
    Positions are located from exact quote/context. Ambiguous text or chapter titles are rejected.
    """
    return await api(ctx, '/api/proposals/chapter', dict(name=name,book_revision=book_revision,operation=operation,
        quote=quote,title=title,chapter_id=chapter_id,target_title=target_title,parent_title=parent_title,
        reason=reason,start=start,context_before=context_before,context_after=context_after))


@mcp.tool()
async def list_proposals(name: str, ctx: Context, status: Literal['','pending','accepted','rejected','conflict'] = '',
                         offset: int = 0, limit: int = 20) -> dict:
    """List your own revision suggestions and decisions; administrators can see all for a book.

    Paginated, limit 1..50. Accepted means applied to the ONE shared administrator draft, not published TXT.
    This tool neither records reading/review progress nor exposes the administrator's draft text.
    """
    return await api(ctx, '/api/proposals?' + urllib.parse.urlencode(dict(name=name,status=status,offset=offset,limit=limit)))


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
            if not token or not (reader.channel_identity(token, "mcp") or reader.channel_identity(token, "mcp-url")):
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
        if scope["type"] == "http" and scope.get("path", "").startswith("/mcp/member/"):
            headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            hosts = [v.decode().lower() for k, v in scope["headers"] if k.lower() == b"host"]
            token = scope["path"][len("/mcp/member/"):]
            who = reader.channel_identity(token, "mcp-url")
            # Legacy tokens are never URL credentials; no implicit second channel.
            member = reader.member_profiles().get(who, {})
            if (len(hosts) != 1 or hosts[0] not in (extra_host, extra_host + ":443")
                    or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token)):
                return await JSONResponse({"error": "Not found"}, status_code=404)(scope, receive, send)
            if (not who or member.get("credential", {}).get("channel") != "mcp-url"
                    or ("authorization" in headers and bearer(headers) != token)):
                return await JSONResponse({"error": "Unauthorized"}, status_code=401)(scope, receive, send)
            scope = dict(scope)
            scope["path"] = MCP_PATH
            scope["raw_path"] = MCP_PATH.encode("ascii")
            scope["headers"] = [(k, v) for k, v in scope["headers"] if k.lower() != b"authorization"]
            scope["headers"].append((b"authorization", b"Bearer " + token.encode("ascii")))
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
