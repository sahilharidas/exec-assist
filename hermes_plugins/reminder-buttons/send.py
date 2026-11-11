#!/usr/bin/env python3
"""Send a reminder now, with Done / Snooze buttons, from a cron script:

    send.py "Reminder: Pick up the parcel from the post office."

Prints nothing and exits 0 once Telegram accepts it, so the no-agent cron job
stays silent. Exits non-zero otherwise, and the calling script prints the
plain text for Hermes to deliver instead.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import store  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].strip():
        sys.exit("usage: send.py TEXT")
    try:
        store.send_now(sys.argv[1].strip())
    except Exception as exc:
        sys.exit(f"reminder-buttons: {exc}")
