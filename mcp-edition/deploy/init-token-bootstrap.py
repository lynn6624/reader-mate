"""Create one-time token-admin setup code; never print its value."""
import os
from pathlib import Path
import secrets

data = Path(os.environ.get("READER_DATA_DIR", "/opt/reader-mate/core/data"))
data.mkdir(parents=True, exist_ok=True)
path = data / ".token-bootstrap"
try:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
except FileExistsError:
    raise SystemExit("Bootstrap file already exists; refusing to replace it")
with os.fdopen(fd, "w", encoding="ascii") as stream:
    stream.write(secrets.token_urlsafe(32) + "\n")
print("Created one-time bootstrap file (0600); value omitted")
