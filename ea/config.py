"""Paths, timezone and secret resolution.

Secrets are read line-by-line out of env files rather than sourced, so a file
with shell syntax in it cannot execute anything. Nothing here ever logs a secret
value.
"""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = Path(os.environ.get("EA_STATE_DIR", ROOT / "state"))
LOG_DIR = Path(os.environ.get("EA_LOG_DIR", ROOT / "logs"))

VAULT_ROOT = Path(os.environ.get("EA_VAULT_ROOT", ROOT / "vault"))
# The Obsidian vault is one folder inside the synced directory.
VAULT = VAULT_ROOT / os.environ.get("EA_VAULT_NAME", "Notes")

# Folders inside the vault that the tooling writes to or reads from.
DAILY_DIR = VAULT / "00 📅 Daily"
INBOX_NOTE = VAULT / "00 📅 Daily" / "_Inbox.md"
TODO_NOTE = VAULT / "TODO.md"
ATTACHMENTS = VAULT / "06 🖼️ Attachments"
REVIEW_DIR = VAULT / "00 📅 Daily" / "Reviews"

# The host clock is UTC and the user is not, so every user-facing date/time goes
# through this.
TZ = ZoneInfo(os.environ.get("EA_TZ", "Africa/Johannesburg"))

# Searched in order; first file containing the key wins.
# EA_ENV_FILES is a colon-separated list; the default is .env beside the code.
SECRET_FILES = [
    Path(p) for p in os.environ.get("EA_ENV_FILES", str(ROOT / ".env")).split(":") if p
]

# Some env files carry placeholders ("user_provided") where a key should be, not
# real credentials. Treat them as absent so callers fall back cleanly.
_PLACEHOLDERS = {"user_provided", "", "changeme", "sk-placeholder"}


def secret(*names, required=False):
    """Return the first resolved value among `names`, checking env then files."""
    for name in names:
        val = os.environ.get(name)
        if _usable(val):
            return val.strip()
    for path in SECRET_FILES:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#"):
                continue
            line = line.removeprefix("export ").strip()
            for name in names:
                if line.startswith(name + "="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if _usable(val):
                        return val
    if required:
        raise SystemExit(f"required secret not resolved: {' / '.join(names)}")
    return None


def _usable(val):
    return val is not None and val.strip() and val.strip() not in _PLACEHOLDERS


def ensure_dirs():
    for d in (STATE_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
