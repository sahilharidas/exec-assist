#!/usr/bin/env python3
"""Backfill task first-sighting dates from the nightly vault archives.

Without this, every task looks brand new on the day the tooling is installed and
stale detection needs a month of warm-up before it can say anything useful. The
archives in ~/backups/vault give real history instead.

Tasks already present in the oldest archive get floor=True: their true age is
*at least* that old, and callers phrase them as "10+ days" rather than inventing
a precision that is not in the data.
"""
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ea import config, tasks as tasklib, vault
from ea.store import TaskStore

ARCHIVE_DIR = Path(os.environ.get("EA_ARCHIVE_DIR", config.ROOT / "backups" / "vault"))
STAMP = re.compile(r"vault-(\d{8})-(\d{6})\.tar\.gz$")


def archives():
    found = []
    for p in sorted(ARCHIVE_DIR.glob("vault-*.tar.gz")):
        m = STAMP.search(p.name)
        if not m:
            continue
        when = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        found.append((when.replace(tzinfo=config.TZ), p))
    return sorted(found)


def notes_from(archive, workdir):
    """Extract just the vault's markdown notes from one archive."""
    dest = Path(workdir)
    res = subprocess.run(
        ["tar", "-xzf", str(archive), "-C", str(dest),
         "--wildcards", "--no-anchored", f"{config.VAULT.name}/*.md"],
        capture_output=True, text=True, timeout=300,
    )
    if res.returncode != 0 and not any(dest.rglob("*.md")):
        print(f"  ! extract failed: {res.stderr.strip()[:200]}")
        return []
    return sorted(dest.rglob("*.md"))


def main():
    found = archives()
    if not found:
        sys.exit(f"no archives in {ARCHIVE_DIR}")
    oldest = found[0][0]
    print(f"{len(found)} archives, {oldest.date()} .. {found[-1][0].date()}")

    store = TaskStore()
    seeded = 0
    for when, archive in found:
        with tempfile.TemporaryDirectory(prefix="ea-backfill-") as tmp:
            paths = notes_from(archive, tmp)
            if not paths:
                print(f"  {when.date()}: no notes extracted, skipping")
                continue
            found_tasks = tasklib.dedupe(tasklib.index(paths))
            # Only the first archive a task appears in matters, and seed() keeps
            # the earliest sighting, so processing oldest-first settles each key
            # on its true floor.
            new = 0
            for t in found_tasks:
                if store.seed(t.key, t.text, when, floor=(when == oldest)):
                    new += 1
            seeded += new
            print(f"  {when.date()}: {len(found_tasks)} tasks, {new} dated earlier than before")
    store.commit()

    # Re-sync so current vault state (text, domain, done) sits on top of the
    # backfilled dates rather than being overwritten by them.
    store = TaskStore()
    live = store.sync(record_first_seen=True)
    store.commit()

    ages = sorted(
        ((store.age_days(r), t.text, r.get("floor", False)) for t, r in live if t.open),
        reverse=True,
    )
    print(f"\nbackfilled {seeded} first-sighting dates")
    print("oldest open items:")
    for age, text, floor in ages[:12]:
        print(f"  {age:>3}{'+' if floor else ' '} days  {text[:64]}")


if __name__ == "__main__":
    main()
