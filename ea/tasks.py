"""Parse the vault's checkbox tasks into structured records.

The vault's own conventions drive this parser rather than the other way round:
'- '/'* ' bullets, tab nesting, '### ... DOMAIN: X' headings, '**Bold**'
sub-sections and '[WAIT]' prefix tags all already exist in TODO.md.
"""
import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import config, vault

CHECKBOX = re.compile(r"^(?P<ws>[\t ]*)(?P<bullet>[-*+])\s+\[(?P<mark>.)\]\s?(?P<text>.*)$")
DOMAIN = re.compile(r"^#{1,6}\s*.*?DOMAIN:\s*(.+?)\s*$", re.I)
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
BOLD_SECTION = re.compile(r"^\s*\*\*(.+?)\*\*:?\s*$")
TAG = re.compile(r"\[([A-Z][A-Z0-9 _/-]{1,20})\]")
DUE = re.compile(r"(?:📅\s*|\bdue:\s*|(?<![\w@])@)(\d{4}-\d{2}-\d{2})")

# Checkbox marks. Obsidian's Tasks plugin uses more than two states and the
# difference matters here: a cancelled item must not be nagged about, but it is
# also not an accomplishment for the weekly review.
OPEN_MARKS = {" ", "", "/", "?"}
DONE_MARKS = {"x", "X"}
CANCELLED_MARKS = {"-", "~"}
# '[>]' is the bullet-journal "migrated" mark: the morning carry-over moved the
# item into a later note, so the line left behind is a pointer, not a live copy.
# Counting it as open is what kept carried items alive after they were ticked in
# today's note -- each had a fresh open copy in every daily it passed through.
MIGRATED_MARKS = {">"}


@dataclass
class Task:
    file: Path
    lineno: int
    indent: int
    bullet: str
    mark: str
    text: str
    raw: str
    domain: str = None
    section: str = None
    tags: list = field(default_factory=list)
    due: date = None

    @property
    def done(self):
        return self.mark in DONE_MARKS

    @property
    def cancelled(self):
        return self.mark in CANCELLED_MARKS

    @property
    def migrated(self):
        return self.mark in MIGRATED_MARKS

    @property
    def open(self):
        return not self.done and not self.cancelled and not self.migrated

    @property
    def waiting(self):
        return any(t.startswith("WAIT") for t in self.tags)

    @property
    def frog(self):
        return any(t == "FROG" for t in self.tags)

    @property
    def key(self):
        """Stable identity for a task across edits, ticks and file moves.

        Keyed on normalised text only -- not the file -- so that rehoming a note
        (or the autofile job moving a loose daily) does not reset the item's age.
        """
        return hashlib.sha1(normalise(self.text).encode("utf-8")).hexdigest()[:16]

    @property
    def where(self):
        return vault.rel(self.file)

    def __str__(self):
        return self.text


def normalise(text):
    """Lowercased, punctuation-light form used for identity and de-duplication."""
    t = text.lower()
    t = TAG.sub(" ", t)
    t = DUE.sub(" ", t)
    t = re.sub(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", r"\1", t)   # wiki links
    t = re.sub(r"[*_`~]", "", t)                                # emphasis
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def clean(text):
    """Display form: emphasis, tags and due tokens stripped."""
    t = TAG.sub("", text)
    t = DUE.sub("", t)
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip(" -–—:")


def parse_note(path):
    """Parse one markdown note into Tasks, carrying heading context down."""
    out = []
    domain = None
    section = None
    for i, raw in enumerate(vault.read(path).splitlines(), start=1):
        dm = DOMAIN.match(raw)
        if dm:
            domain = dm.group(1).strip()
            section = None
            continue
        hm = HEADING.match(raw)
        if hm and not CHECKBOX.match(raw):
            # A plain heading resets the sub-section but keeps the domain, which
            # is set by the more specific DOMAIN pattern above.
            section = hm.group(2).strip() or None
            continue
        bm = BOLD_SECTION.match(raw)
        if bm:
            section = bm.group(1).strip()
            continue
        cm = CHECKBOX.match(raw)
        if not cm:
            continue
        text = cm.group("text").strip()
        if not text:
            continue
        due = None
        dmatch = DUE.search(text)
        if dmatch:
            try:
                due = date.fromisoformat(dmatch.group(1))
            except ValueError:
                due = None
        out.append(Task(
            file=Path(path),
            lineno=i,
            indent=len(cm.group("ws").replace("\t", "    ")) // 4,
            bullet=cm.group("bullet"),
            mark=cm.group("mark"),
            text=clean(text),
            raw=raw,
            domain=domain,
            section=section,
            tags=[t.strip() for t in TAG.findall(text)],
            due=due,
        ))
    return out


def index(paths=None):
    """Parse every note (or the given ones) into a flat task list."""
    out = []
    for p in (paths if paths is not None else vault.all_notes()):
        try:
            out.extend(parse_note(p))
        except OSError:
            continue
    return out


def dedupe(tasks):
    """Collapse tasks that share an identity key, preferring the open copy.

    The same item often lives in both TODO.md and a daily note; nagging about it
    twice is exactly the noise that makes people mute a bot. Migrated copies are
    skipped: the live copy is wherever the item was carried to.
    """
    best = {}
    for t in tasks:
        if t.migrated:
            continue
        cur = best.get(t.key)
        if cur is None:
            best[t.key] = t
            continue
        # An open copy beats a done copy: if it is still open anywhere, it is
        # still real work. Among open copies, one on a to-do list beats one in a
        # project plan, so the item is still nagged about.
        if t.open and (not cur.open or (vault.is_todo_list(t.file)
                                        and not vault.is_todo_list(cur.file))):
            best[t.key] = t
    return list(best.values())


def toggle(path, task, mark="x"):
    """Tick (or re-open) a task in place, matching on text rather than line number.

    Only rewrites a line that is in the copy's own state: matching on text alone
    picked the first hit, so a note with a done copy above an open one "ticked"
    the done line and left the open one open.
    """
    lines = vault.read(path).splitlines()
    target = normalise(task.text)
    for i, raw in enumerate(lines):
        cm = CHECKBOX.match(raw)
        if not cm or normalise(clean(cm.group("text"))) != target:
            continue
        if cm.group("mark") != task.mark:
            continue
        lines[i] = f"{cm.group('ws')}{cm.group('bullet')} [{mark}] {cm.group('text')}"
        vault.write(path, "\n".join(lines) + "\n")
        return True
    return False
