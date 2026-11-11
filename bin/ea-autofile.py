#!/usr/bin/env python3
"""Nightly tidy: move loose dated daily notes into '00 📅 Daily/'.

The vault has dailies in two places -- some in '00 📅 Daily/', and the rest
loose at the vault root. The tooling reads both, so this is
cosmetic for the bot but not for navigating the vault in Obsidian.

Self-healing rather than a fight with Obsidian: if the daily-note plugin keeps
creating notes at the root, this moves yesterday's each night and today's is left
alone. Every move is appended to state/autofile.log with both paths, so the whole
run can be reversed.

Only unambiguously-dated 'YYYY-MM-DD.md' notes are moved, and never over an
existing file. Anything else at the root is reported, not touched -- guessing
where someone's notes belong is how you lose their notes.
"""
import argparse
import re
import shutil
import sys
import traceback
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ea import config, vault

DATED = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\.md$")
LOG = config.STATE_DIR / "autofile.log"


def candidates():
    """Loose dated dailies at the vault root, excluding today's."""
    today = vault.today()
    out = []
    for p in sorted(config.VAULT.glob("*.md")):
        m = DATED.match(p.name)
        if not m:
            continue
        try:
            when = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if when >= today:
            continue          # never move the note that is still being written
        out.append((when, p))
    return out


def other_root_notes():
    return [p for p in sorted(config.VAULT.glob("*.md")) if not DATED.match(p.name)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="perform the moves (default is a dry run)")
    args = ap.parse_args()

    config.DAILY_DIR.mkdir(parents=True, exist_ok=True)
    moves, blocked = [], []
    for when, src in candidates():
        dest = config.DAILY_DIR / src.name
        if dest.exists():
            blocked.append((src, dest))
            continue
        moves.append((src, dest))

    for src, dest in moves:
        if args.apply:
            shutil.move(str(src), str(dest))
            config.STATE_DIR.mkdir(parents=True, exist_ok=True)
            with LOG.open("a", encoding="utf-8") as fh:
                fh.write(f"{vault.now().isoformat()}\tmove\t{src}\t{dest}\n")
        print(f"{'moved' if args.apply else 'would move'}: {src.name} -> {vault.rel(dest)}")

    for src, dest in blocked:
        print(f"skipped (target exists): {src.name} -- merge by hand if you want them joined")

    rest = other_root_notes()
    if rest:
        print(f"\n{len(rest)} other note(s) at the vault root, left alone:")
        for p in rest[:12]:
            print(f"  {p.name}")

    if not moves and not blocked:
        print("nothing to file")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
