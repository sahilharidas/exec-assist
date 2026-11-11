#!/usr/bin/env python3
"""Morning prep, run before the 07:00 SAST LibreChat brief.

Three jobs, in order:
  1. carry yesterday's unfinished items into today's note (this is why the daily
     notes stopped in June -- nothing moved them forward);
  2. re-sync the task store so ages are current;
  3. write '00 📅 Daily/_Today.md', which the Morning Brief agent reads over the
     obsidian MCP. The agent cannot compute task ages itself, so this note is the
     interface between the Python side and the LLM side.

Sends nothing. The 07:00 brief does the talking.
"""
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ea import brief, config, evening, vault


def main():
    vault.ensure_daily()
    dest, moved = evening.carry_forward()
    if moved:
        print(f"carried {len(moved)} unfinished item(s) into {vault.rel(dest)}")
        for text in moved:
            print(f"  - {text}")
    else:
        print("nothing to carry forward")

    snap = brief.snapshot()
    config.REVIEW_DIR.parent.mkdir(parents=True, exist_ok=True)
    path = vault.write(config.INBOX_NOTE.with_name("_Today.md"),
                       brief.render_markdown(snap))
    print(f"wrote {vault.rel(path)}: {snap['open_count']} open, "
          f"{len(snap['overdue'])} overdue, {len(snap['stale'])} stale, "
          f"{len(snap['waiting'])} waiting")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
