"""Done / Drop buttons on the exec-assist task nags.

~/exec-assist/bin/ea-stale.py posts the midday nag with one row per item:
"✅ <item>" (callback task:x:<id>) and "🗑" (task:-:<id>). This plugin handles
the taps in the gateway: it calls the exec_assist MCP close_task tool, which
ticks ([x]) or drops ([-]) every open copy of the item in the vault, then
removes that row and notes the outcome under the message. No model involved.

Before this, the nag said "/done <text>", a command of the retired ea-bot that
nothing answered, and text replies ("car license is on") were acknowledged by
the agent without ever ticking anything.

Needs, in config.yaml:
    plugins.enabled: [..., task-buttons]
    plugins.entries.task-buttons.mcp_allowlist: [exec_assist]
"""

import asyncio
import html
import logging
import os
import re

logger = logging.getLogger(__name__)

_ctx = None
_lock = asyncio.Lock()

KEY = re.compile(r"[0-9a-f]{16}")
# close_task replies that mean the item is no longer open, so its row can go.
SETTLED = ("Ticked", "Dropped", "Already closed", "No longer open")


def register(ctx):
    global _ctx
    _ctx = ctx
    ctx.register_telegram_handler(_wire)


def _wire(application, adapter):
    from telegram.ext import CallbackQueryHandler

    application.add_handler(CallbackQueryHandler(_on_button, pattern=r"^task:"))


def _authorized(user_id):
    allowed = os.environ.get("TELEGRAM_ALLOWED_USERS", "")
    return str(user_id) in {u.strip() for u in allowed.split(",") if u.strip()}


def _close(key, how):
    """Call exec_assist close_task; return its text, raising on any failure."""
    envelope = _ctx.call_mcp("exec_assist", "close_task",
                             {"task_id": key, "how": how}, timeout=30)
    if not envelope.get("ok"):
        raise RuntimeError(envelope.get("error") or "MCP call failed")
    result = envelope.get("result")
    return result if isinstance(result, str) else str(result)


async def _on_button(update, context):
    query = update.callback_query
    try:
        _, op, key = query.data.split(":", 2)
    except ValueError:
        await query.answer()
        return
    if op not in ("x", "-") or not KEY.fullmatch(key):
        await query.answer()
        return
    if not _authorized(query.from_user.id):
        await query.answer(text="⛔ Not authorized.")
        return

    how = "done" if op == "x" else "drop"
    try:
        async with _lock:
            result = await asyncio.to_thread(_close, key, how)
    except Exception as exc:
        logger.error("task-buttons: closing %s (%s) failed: %s", key, how, exc)
        await query.answer(text="❌ Couldn't update the vault. Try again, or tell me in words.",
                           show_alert=True)
        return
    logger.info("task-buttons: %s %s -> %s", how, key, result)

    if not result.startswith(SETTLED):
        await query.answer(text=result[:200], show_alert=True)
        return
    await query.answer(text=result[:200])

    from telegram import InlineKeyboardMarkup

    message = query.message
    rows = [
        row for row in (message.reply_markup.inline_keyboard if message.reply_markup else [])
        if not any((b.callback_data or "").endswith(key) for b in row)
    ]
    icon = "🗑" if how == "drop" else "✅"
    try:
        await query.edit_message_text(
            text=f"{message.text_html}\n\n{icon} {html.escape(result)}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows) if rows else None,
        )
    except Exception as exc:  # e.g. "message is not modified" on a double tap
        logger.debug("task-buttons: message edit skipped: %s", exc)
