"""Create two independent MCP URL keys on the server; never print their values."""
import os
from pathlib import Path
import re
import secrets
import stat

state = Path(os.environ.get("READER_MCP_STATE_DIR", "/opt/reader-mate/state"))
state.mkdir(parents=True, exist_ok=True)
state.chmod(0o700)
for name in ("mcp-path-key.txt", "mcp-004-url-key.txt"):
    path = state / name
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise SystemExit(f"Existing key has unsafe permissions: {name}")
        if not re.fullmatch(r"[A-Za-z0-9_-]{24}", path.read_text(encoding="ascii").strip()):
            raise SystemExit(f"Existing key is invalid: {name}")
        print(f"Kept existing {name}")
        continue
    with os.fdopen(fd, "w", encoding="ascii") as stream:
        stream.write(secrets.token_urlsafe(18))
    print(f"Created {name} (0600); value omitted")
