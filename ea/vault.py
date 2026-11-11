"""Vault note I/O.

Every write is atomic (temp file in the same directory + os.replace) because the
vault is live-synced over WebDAV and a half-written note is worse than no note.
"""
import os
import re
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from . import config

DAILY_TEMPLATE = """# {human}

## TODO
{carry}
## Captured

## Log
"""


def read(path):
    p = Path(path)
    if not p.is_file():
        return ""
    return p.read_text(encoding="utf-8", errors="replace")


def write(path, text):
    """Atomically replace a note's contents."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".ea-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, p)
    except BaseException:
        # Never leave a stray .ea-*.tmp behind for Obsidian to sync and display.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p


def append(path, text, header=None):
    """Append text, optionally under a markdown header, creating it if absent.

    Appending under a header keeps captures grouped instead of accreting at the
    bottom of the note in arrival order.
    """
    p = Path(path)
    body = read(p)
    block = text if text.endswith("\n") else text + "\n"

    if header:
        marker = header.strip()
        lines = body.splitlines()
        idx = next((i for i, ln in enumerate(lines) if ln.strip() == marker), None)
        if idx is None:
            sep = "" if not body or body.endswith("\n") else "\n"
            body = f"{body}{sep}\n{marker}\n{block}"
        else:
            # Insert at the end of this section: scan to the next header of the
            # same or shallower depth, then back up over trailing blank lines so
            # the new line sits directly under the existing content.
            depth = len(marker) - len(marker.lstrip("#"))
            end = len(lines)
            for i in range(idx + 1, len(lines)):
                s = lines[i].lstrip()
                if s.startswith("#"):
                    d = len(s) - len(s.lstrip("#"))
                    if 0 < d <= depth:
                        end = i
                        break
            while end > idx + 1 and not lines[end - 1].strip():
                end -= 1
            lines[end:end] = block.rstrip("\n").splitlines()
            body = "\n".join(lines) + "\n"
    else:
        sep = "" if not body or body.endswith("\n") else "\n"
        body = f"{body}{sep}{block}"

    return write(p, body)


def today(tz=None):
    return datetime.now(tz or config.TZ).date()


def now(tz=None):
    return datetime.now(tz or config.TZ)


def daily_path(d=None, create=False):
    """Resolve the daily note for `d`.

    The vault has dailies in two places: the intended '00 📅 Daily/' folder and
    loose at the vault root. Prefer
    whichever already exists so nothing gets fragmented further; only new notes
    go to the canonical folder.
    """
    d = d or today()
    name = f"{d.isoformat()}.md"
    canonical = config.DAILY_DIR / name
    loose = config.VAULT / name
    for candidate in (canonical, loose):
        if candidate.is_file():
            return candidate
    if create:
        return ensure_daily(d)
    return canonical


def ensure_daily(d=None):
    """Create the daily note from the template if it does not exist yet."""
    d = d or today()
    name = f"{d.isoformat()}.md"
    canonical = config.DAILY_DIR / name
    loose = config.VAULT / name
    for candidate in (canonical, loose):
        if candidate.is_file():
            return candidate
    carry = ""
    write(canonical, DAILY_TEMPLATE.format(human=d.strftime("%A %d %B %Y"), carry=carry))
    return canonical


def recent_dailies(days=14):
    """Daily notes from the last `days` days, oldest first, wherever they live."""
    out = []
    start = today() - timedelta(days=days)
    for i in range((today() - start).days + 1):
        d = start + timedelta(days=i)
        p = daily_path(d)
        if p.is_file():
            out.append((d, p))
    return out


DAILY_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")


def is_todo_list(path):
    """Is this one of the user's own to-do lists?

    Daily notes (in either location), anything named TODO, the activity logs,
    the inbox and the shopping list. Every other note with checkboxes is a
    project plan, spec, parts list or status summary: its items are still
    tracked, can be completed by name and still count when tagged or dated,
    but are never nagged about as stale or picked for the shortlist. One
    74-line renovation plan otherwise buries the real list.
    """
    name = Path(path).name
    return bool(DAILY_NAME.match(name)
                or "todo" in name.lower()
                or name.endswith("Activity Log.md")
                or name in (config.INBOX_NOTE.name, "Shopping List.md"))


def all_notes(root=None):
    """Yield every markdown note under the vault, skipping archives and junk."""
    root = Path(root or config.VAULT)
    skip_parts = {"99 🗄️ Archive", ".obsidian", ".trash", "06 🖼️ Attachments"}
    for p in sorted(root.rglob("*.md")):
        if any(part in skip_parts for part in p.parts):
            continue
        if p.name.startswith("."):
            continue
        yield p


def rel(path):
    """Vault-relative path, for display and for state keys."""
    try:
        return str(Path(path).relative_to(config.VAULT_ROOT))
    except ValueError:
        return str(path)
