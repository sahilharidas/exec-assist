"""Durable state.

The vault stores no per-task timestamps, so an item's age has to be tracked
here: every scan upserts what it sees and the first sighting is remembered. Ages
for tasks that predate the tooling are backfilled from the nightly vault
archives (see bin/ea-backfill.py), and those carry `floor=True` to record that
the real first sighting is *at least* that old rather than exactly that old.
"""
import json
import os
import tempfile
from datetime import date, datetime, timedelta

from . import config, tasks as tasklib, vault


def _path(name):
    config.ensure_dirs()
    return config.STATE_DIR / f"{name}.json"


def load(name, default=None):
    p = _path(name)
    if not p.is_file():
        return {} if default is None else default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {} if default is None else default


def save(name, obj):
    p = _path(name)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p


def tracking_since():
    """When completion tracking began on this host.

    Needed so reports can distinguish "you closed nothing" from "nothing was
    being watched yet". Without it, the first weekly review reads as a verdict on
    the reader's week when it is really an artefact of install day.
    """
    meta = load("meta", {})
    if "tracking_since" not in meta:
        meta["tracking_since"] = vault.now().isoformat()
        save("meta", meta)
    try:
        return datetime.fromisoformat(meta["tracking_since"])
    except ValueError:
        return vault.now()


def _count(copies):
    return {
        "open": sum(1 for t in copies if t.open),
        "done": sum(1 for t in copies if t.done),
        "cancelled": sum(1 for t in copies if t.cancelled),
    }


class TaskStore:
    NAME = "tasks"

    def __init__(self):
        data = load(self.NAME, {"version": 1, "tasks": {}})
        self.version = data.get("version", 1)
        self.tasks = data.get("tasks", {})

    def commit(self):
        save(self.NAME, {"version": self.version, "tasks": self.tasks})

    def propagate(self, scanned):
        """Close every copy of an item once any one copy is newly closed.

        The same item often sits in several notes (TODO.md, a daily, an activity
        log), and dedupe() keeps it open while any copy is open -- right for a
        recurring item, but it meant a tick in Obsidian only counted if every
        copy was found and ticked by hand. complete() already closes all copies;
        this does the same for a tick made in the vault, the first time a scan
        sees it: a key whose count of done (or cancelled) copies went up.

        Only keys with a count from an earlier scan qualify, so the first scan
        after install ticks nothing, and neither do copies that were already
        mixed before it. Returns {key: counts} for sync() to record.
        """
        groups = {}
        for t in scanned:
            if not t.migrated:
                groups.setdefault(t.key, []).append(t)
        counts = {}
        for key, copies in groups.items():
            c = _count(copies)
            prev = self.tasks.get(key, {}).get("copies")
            if prev and c["open"]:
                mark = ("x" if c["done"] > prev.get("done", 0)
                        else "-" if c["cancelled"] > prev.get("cancelled", 0)
                        else None)
                if mark:
                    for t in copies:
                        if t.open and tasklib.toggle(t.file, t, mark):
                            t.mark = mark
                    c = _count(copies)
            counts[key] = c
        return counts

    def sync(self, task_list=None, when=None, record_first_seen=True):
        """Upsert what the vault currently shows; return the live records.

        A scan of the live vault (no `task_list`) also closes the remaining
        copies of anything newly ticked; see propagate().
        """
        when = when or vault.now()
        stamp = when.isoformat()
        scanned = task_list if task_list is not None else tasklib.index()
        counts = self.propagate(scanned) if task_list is None else {}
        seen = tasklib.dedupe(scanned)
        live = []
        for t in seen:
            rec = self.tasks.setdefault(t.key, {})
            if t.key in counts:
                rec["copies"] = counts[t.key]
            # Read the previous state before overwriting it: the open -> done
            # transition is the only thing that proves a completion happened
            # while we were watching, and it is what done_at is allowed to mean.
            prev_state = rec.get("state")
            if "first_seen" not in rec and record_first_seen:
                rec["first_seen"] = stamp
                rec["floor"] = False
            rec.update({
                "text": t.text,
                "where": t.where,
                "domain": t.domain,
                "section": t.section,
                "waiting": t.waiting,
                "frog": t.frog,
                "due": t.due.isoformat() if t.due else None,
                "last_seen": stamp,
                "state": "done" if t.done else ("cancelled" if t.cancelled else "open"),
            })
            if t.done:
                if prev_state is None:
                    # Never seen before and already ticked: it was finished
                    # before tracking began. Stamping it now would invent a
                    # completion time and produce false rhythm patterns -- the
                    # first run of this tooling otherwise "completed" 246 tasks
                    # at once, all on a Monday night.
                    rec["pre_existing_done"] = True
                    rec.pop("done_at", None)
                elif prev_state == "open" and not rec.get("done_at"):
                    rec["done_at"] = stamp
                    rec.pop("pre_existing_done", None)
            if t.open and rec.get("done_at"):
                # Re-opened: drop the completion stamp so it can nag again, but
                # keep first_seen so its true age survives.
                rec.pop("done_at", None)
            rec.setdefault("nag_count", 0)
            rec.setdefault("nagged_at", None)
            live.append((t, rec))
        return live

    def age_days(self, rec, when=None):
        fs = rec.get("first_seen")
        if not fs:
            return 0
        try:
            seen = datetime.fromisoformat(fs)
        except ValueError:
            return 0
        return max(0, ((when or vault.now()) - seen).days)

    def note_nag(self, key, when=None):
        rec = self.tasks.setdefault(key, {})
        rec["nagged_at"] = (when or vault.now()).isoformat()
        rec["nag_count"] = rec.get("nag_count", 0) + 1

    def nagged_within(self, key, hours):
        rec = self.tasks.get(key, {})
        at = rec.get("nagged_at")
        if not at:
            return False
        try:
            return (vault.now() - datetime.fromisoformat(at)) < timedelta(hours=hours)
        except ValueError:
            return False

    def completed_between(self, start, end):
        """Records completed in [start, end) -- drives the weekly review."""
        out = []
        for key, rec in self.tasks.items():
            at = rec.get("done_at")
            if not at:
                continue
            try:
                when = datetime.fromisoformat(at)
            except ValueError:
                continue
            if start <= when < end:
                out.append((key, rec))
        return sorted(out, key=lambda kv: kv[1].get("done_at", ""))

    def seed(self, key, text, when, floor=True, extra=None):
        """Record a first sighting from history (backfill), never overwriting one."""
        rec = self.tasks.setdefault(key, {})
        existing = rec.get("first_seen")
        if existing:
            try:
                if datetime.fromisoformat(existing) <= when:
                    return False
            except ValueError:
                pass
        rec["first_seen"] = when.isoformat()
        rec["floor"] = floor
        rec.setdefault("text", text)
        if extra:
            rec.update(extra)
        return True
