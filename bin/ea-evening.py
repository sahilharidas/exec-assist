#!/usr/bin/env python3
"""The 21:00 SAST shutdown prompt.

Arms the reply window and sends one message. The bot routes whatever comes back
into the day's note -- see ea/evening.py for why the armed flag lives in its own
state file rather than the bot's.
"""
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ea import evening

if __name__ == "__main__":
    try:
        sent, why = evening.send_prompt(force="--force" in sys.argv)
        print("sent" if sent else f"skipped: {why}")
    except Exception:
        traceback.print_exc()
        sys.exit(1)
