#!/usr/bin/env python3
"""The midday nudge: overdue, chases, and one or two stale items.

Deliberately narrow. A list of 136 stale items is not a nudge, it is wallpaper --
so this rotates: an item nagged recently is not nagged again until the cooldown
expires, which means the message is different most days and stays worth opening.

Exits silently when there is nothing to say, so it can run on a daily timer
without becoming noise.
"""
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ea import brief, telegram, vault

STALE_COOLDOWN_H = 24 * 6      # don't re-nag the same stale item within ~6 days
WAIT_COOLDOWN_H = 24 * 3       # chases can repeat sooner; someone else is blocking
MAX_STALE = 2
MAX_WAIT = 3
MAX_BUTTON_ROWS = 6
BUTTON_TEXT_CHARS = 30
# Taps are handled by the Hermes plugin ~/.hermes/plugins/task-buttons, which
# closes the item through the exec_assist MCP close_task tool. The footer used
# to say "/done <text>", a command of the retired ea-bot that nothing answered.
FOOTER = "_✅ done · 🗑 no longer matters · or just tell me._"
PLAIN_FOOTER = "_Tell me when one's done and I'll tick it._"
HERMES_CONFIG = Path(os.environ.get("HERMES_CONFIG", Path.home() / ".hermes" / "config.yaml"))


def buttons_live():
    """True when Hermes has the task-buttons plugin enabled.

    Without the handler a tap just spins forever, which is worse than no
    button, so the nag only carries buttons once the plugin is switched on.
    """
    try:
        import yaml
        plugins = yaml.safe_load(HERMES_CONFIG.read_text()).get("plugins") or {}
    except Exception:
        return False
    return ("task-buttons" in (plugins.get("enabled") or [])
            and "task-buttons" not in (plugins.get("disabled") or []))


def keyboard(items):
    """One Done / Drop row per listed item, keyed by id (callback data is 64 bytes)."""
    rows, seen = [], set()
    for t, _r in items:
        if t.key in seen or len(rows) >= MAX_BUTTON_ROWS:
            continue
        seen.add(t.key)
        label = t.text if len(t.text) <= BUTTON_TEXT_CHARS else t.text[:BUTTON_TEXT_CHARS - 1] + "…"
        rows.append([
            {"text": f"✅ {label}", "callback_data": f"task:x:{t.key}"},
            {"text": "🗑", "callback_data": f"task:-:{t.key}"},
        ])
    return {"inline_keyboard": rows} if rows else None


def main():
    snap = brief.snapshot()
    store = snap["store"]

    overdue = snap["overdue"]
    due_today = snap["due_today"]
    waiting = [
        (t, r) for t, r in snap["waiting"]
        if not store.nagged_within(t.key, WAIT_COOLDOWN_H)
    ][:MAX_WAIT]
    stale = [
        (t, r) for t, r in snap["stale"]
        if not store.nagged_within(t.key, STALE_COOLDOWN_H)
    ][:MAX_STALE]

    if not (overdue or due_today or waiting or stale):
        print("nothing worth saying")
        return

    L = []
    if overdue:
        L.append(f"⚠️ **Overdue ({len(overdue)})**")
        for t, r in overdue[:5]:
            L.append(f"- {t.text} — _was due {t.due.isoformat()}_")
        L.append("")
    if due_today:
        L.append("**Due today**")
        for t, r in due_today[:5]:
            L.append(f"- {t.text}")
        L.append("")
    if waiting:
        L.append("**Still waiting on**")
        for t, r in waiting:
            L.append(f"- {t.text} — _{brief.age_str(store, r)}_")
        L.append("")
    if stale:
        L.append("**Sitting untouched**")
        for t, r in stale:
            L.append(f"- {t.text} — _{brief.age_str(store, r)}"
                     f"{f', {t.domain}' if t.domain else ''}_")
        L.append("")
    listed = list(overdue[:5]) + list(due_today[:5]) + list(waiting) + list(stale)
    markup = keyboard(listed) if buttons_live() else None
    L.append(FOOTER if markup else PLAIN_FOOTER)

    telegram.send("\n".join(L), reply_markup=markup)

    for t, _r in list(waiting) + list(stale):
        store.note_nag(t.key)
    store.commit()
    print(f"nagged: {len(overdue)} overdue, {len(waiting)} waiting, {len(stale)} stale")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
