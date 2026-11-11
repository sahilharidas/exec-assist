"""One-off reminders with Done / Snooze buttons on Telegram.

- Tools reminder_add / reminder_list / reminder_cancel let the agent schedule
  reminders. The text is stored verbatim and sent as-is, with no model at send
  time (same guarantee as the no-agent cron scripts).
- A background loop in the gateway sends due reminders every 20 seconds.
- Taps on the buttons are handled here: Done closes the reminder, Snooze puts
  it back in the queue.
- Cron scripts (remind_*.sh) post through send.py so their messages get the
  same buttons.

State: data/reminders.json (see store.py). Repeating schedules stay cron jobs.
"""

import asyncio
import logging
import os
from datetime import timedelta

from . import store

logger = logging.getLogger(__name__)

_ctx = None
_loop_task = None

TICK_SECONDS = 20


def register(ctx):
    global _ctx
    _ctx = ctx
    for schema, handler in ((ADD_SCHEMA, _tool_add),
                            (LIST_SCHEMA, _tool_list),
                            (CANCEL_SCHEMA, _tool_cancel)):
        ctx.register_tool(name=schema["name"], toolset="reminders", schema=schema,
                          handler=handler, emoji="⏰")
    ctx.register_telegram_handler(_wire)


# -- agent tools -------------------------------------------------------------

ADD_SCHEMA = {
    "name": "reminder_add",
    "description": (
        "Set a one-off reminder. At the given time the text is sent to the "
        "user's Telegram exactly as written, with Done and Snooze buttons. No "
        "model runs at send time, so names and figures arrive exactly as written. Use this "
        "for every one-off 'remind me...' request instead of the cronjob tool; "
        "use cronjob only for repeating schedules. Write `text` as the message "
        "the user should see, e.g. \"Reminder: the library books are due today.\" "
        "The result gives the weekday and date of each reminder; tell the user."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The reminder message, sent verbatim."},
            "at": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "One or more send times in South African local time (SAST, "
                    "UTC+2), each 'YYYY-MM-DD HH:MM'. One entry per date for "
                    "reminders on several days. If the user gives a day but no "
                    "time, use 09:00."
                ),
            },
        },
        "required": ["text", "at"],
    },
}

LIST_SCHEMA = {
    "name": "reminder_list",
    "description": (
        "List the user's upcoming one-off reminders (including snoozed ones), "
        "with their ids. Repeating reminders are cron jobs: list those with "
        "the cronjob tool."
    ),
    "parameters": {"type": "object", "properties": {}},
}

CANCEL_SCHEMA = {
    "name": "reminder_cancel",
    "description": "Cancel an upcoming one-off reminder by the id from reminder_list.",
    "parameters": {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
    },
}


def _result(**data):
    from tools.registry import tool_result

    return tool_result(data)


def _error(message):
    from tools.registry import tool_error

    return tool_error(message)


def _tool_add(args, **kw):
    text = str(args.get("text") or "").strip()
    at = args.get("at") or []
    if isinstance(at, str):
        at = [at]
    if not text:
        return _error("text is empty")
    if len(text) > store.MAX_TEXT:
        return _error(f"text is longer than {store.MAX_TEXT} characters")
    if not at:
        return _error("at is empty")
    now = store.now_sast()
    try:
        times = [store.parse_local(a) for a in at]
    except ValueError:
        return _error(f"at must be 'YYYY-MM-DD HH:MM' (SAST); got {at!r}")
    past = [t for t in times if t < now - timedelta(minutes=1)]
    if past:
        return _error(f"{', '.join(f'{t:%a %Y-%m-%d %H:%M}' for t in past)} has already "
                      f"passed. It is now {now:%a %Y-%m-%d %H:%M} SAST.")
    entries = [store.add(text, t) for t in times]
    return _result(ok=True, text=text, set=[
        {"id": e["id"], "when": f"{t:%A %-d %B %Y, %H:%M} SAST"}
        for e, t in zip(entries, times)
    ])


def _tool_list(args, **kw):
    from datetime import datetime

    return _result(reminders=[
        {"id": r["id"], "when": f"{datetime.fromisoformat(r['due']):%a %-d %b %Y, %H:%M} SAST",
         "text": r["text"]}
        for r in store.pending()
    ])


def _tool_cancel(args, **kw):
    entry = store.cancel(str(args.get("id") or "").strip())
    if not entry:
        return _error("no upcoming reminder with that id (see reminder_list)")
    return _result(ok=True, cancelled=entry["text"])


# -- gateway side ------------------------------------------------------------

def _wire(application, adapter):
    global _loop_task
    from telegram.ext import CallbackQueryHandler

    application.add_handler(CallbackQueryHandler(_on_button, pattern=r"^rem:"))
    # connect() runs again on reconnect; keep a single sender loop.
    if _loop_task is None or _loop_task.done():
        _loop_task = _ctx.spawn_task(_due_loop(), name="reminder-buttons:due")


async def _due_loop():
    while True:
        try:
            sent = await asyncio.to_thread(store.send_due)
            if sent:
                logger.info("reminder-buttons: sent %d reminder(s)", sent)
        except Exception as exc:
            logger.error("reminder-buttons: due loop tick failed: %s", exc)
        await asyncio.sleep(TICK_SECONDS)


def _authorized(user_id):
    allowed = os.environ.get("TELEGRAM_ALLOWED_USERS", "")
    return str(user_id) in {u.strip() for u in allowed.split(",") if u.strip()}


async def _on_button(update, context):
    query = update.callback_query
    try:
        _, rid, op = query.data.split(":", 2)
    except ValueError:
        await query.answer()
        return
    if not _authorized(query.from_user.id):
        await query.answer(text="⛔ Not authorized.")
        return

    now = store.now_sast()
    text = query.message.text or ""
    try:
        if op == "d":
            await asyncio.to_thread(store.finish, rid)
            line = f"✅ Done at {now:%H:%M}."
        elif op.startswith("z"):
            until = store.next_morning(now) if op == "zt" else now + timedelta(minutes=int(op[1:]))
            await asyncio.to_thread(store.snooze, rid, until, text)
            line = f"⏰ Snoozed until {store.describe(until, now)}."
        else:
            await query.answer()
            return
    except Exception as exc:
        logger.error("reminder-buttons: %s on %s failed: %s", op, rid, exc)
        await query.answer(text="❌ Couldn't save that. Try again.", show_alert=True)
        return

    logger.info("reminder-buttons: %s -> %s", rid, line)
    await query.answer(text=line)
    try:
        await query.edit_message_text(text=f"{text}\n\n{line}", reply_markup=None)
    except Exception as exc:
        logger.warning("reminder-buttons: could not edit reminder message: %s", exc)
