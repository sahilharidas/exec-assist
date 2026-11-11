"""Reminder store and Telegram sending for reminder-buttons.

Stdlib only: imported by the gateway plugin, by the agent's tools (which may
run in a separate CLI process) and by send.py from cron scripts, so the store
is a JSON file guarded by flock rather than in-memory state.
"""

import fcntl
import json
import logging
import os
import re
import secrets
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# South Africa has no DST, so a fixed offset is exact.
SAST = timezone(timedelta(hours=2), "SAST")

STORE = Path(__file__).resolve().parent / "data" / "reminders.json"
ENV_FILE = Path(os.environ.get("HERMES_HOME", "/opt/data")) / ".env"

# Finished reminders are kept this long so a late tap on an old message
# still finds its entry.
KEEP_FINISHED = timedelta(days=14)
# A reminder sent this long after its due time says so (Hermes was down).
LATE_AFTER = timedelta(minutes=15)
# Give up on a reminder Telegram rejects this many times. Network failures
# don't count: those retry until Telegram is reachable again.
MAX_ATTEMPTS = 3
MAX_TEXT = 3500


class Rejected(RuntimeError):
    """Telegram refused the request itself; resending it won't help."""

_LATE_PREFIX = re.compile(r"^LATE \([^)]*\): ")


def now_sast():
    return datetime.now(SAST)


def parse_local(value):
    """Parse 'YYYY-MM-DD HH:MM' as SAST; raise ValueError otherwise."""
    return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=SAST)


def describe(dt, now=None):
    """Short human time: '14:30', 'tomorrow 09:00' or 'Mon 2 Nov 09:00'."""
    now = now or now_sast()
    dt = dt.astimezone(SAST)
    if dt.date() == now.date():
        return f"{dt:%H:%M}"
    if dt.date() == now.date() + timedelta(days=1):
        return f"tomorrow {dt:%H:%M}"
    return f"{dt:%a} {dt.day} {dt:%b %H:%M}"


def next_morning(now):
    """09:00 tomorrow, or later today when tapped in the small hours."""
    day = now.date() if now.hour < 5 else now.date() + timedelta(days=1)
    return datetime(day.year, day.month, day.day, 9, 0, tzinfo=SAST)


def _env(name):
    # The gateway has it in its environment; cron scripts have the bot token
    # stripped, so fall back to Hermes' own .env.
    if os.environ.get(name):
        return os.environ[name]
    for line in ENV_FILE.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == name:
            return value.strip().strip("'\"")
    return ""


def home_chat():
    return _env("TELEGRAM_HOME_CHANNEL")


def telegram(method, payload):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{_env('TELEGRAM_BOT_TOKEN')}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as exc:
        if 400 <= exc.code < 500 and exc.code != 429:
            raise Rejected(f"Telegram {method} rejected (HTTP {exc.code})") from None
        raise RuntimeError(f"Telegram {method} failed (HTTP {exc.code})") from None
    except Exception as exc:
        # Type only: urllib errors can carry the URL, which holds the token.
        raise RuntimeError(f"Telegram {method} failed ({type(exc).__name__})") from None
    if not body.get("ok"):
        raise RuntimeError(f"Telegram {method} returned ok=false")
    return body["result"]


def buttons(rid):
    return {"inline_keyboard": [
        [{"text": "✅ Done", "callback_data": f"rem:{rid}:d"}],
        [{"text": "⏰ 1 hour", "callback_data": f"rem:{rid}:z60"},
         {"text": "⏰ 3 hours", "callback_data": f"rem:{rid}:z180"},
         {"text": "☀️ Tomorrow 9am", "callback_data": f"rem:{rid}:zt"}],
    ]}


@contextmanager
def store():
    """Yield the reminder list under an exclusive lock; saved on clean exit."""
    STORE.parent.mkdir(parents=True, exist_ok=True)
    with open(STORE.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        items = json.loads(STORE.read_text()) if STORE.exists() else []
        yield items
        cutoff = now_sast() - KEEP_FINISHED
        items[:] = [r for r in items
                    if r["status"] == "pending"
                    or datetime.fromisoformat(r["updated"]) > cutoff]
        tmp = STORE.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, indent=1, ensure_ascii=False))
        os.replace(tmp, STORE)


def _new(text, due, status):
    return {
        "id": secrets.token_hex(4),
        "text": text,
        "due": due.isoformat(),
        "status": status,
        "updated": now_sast().isoformat(),
        "chat_id": home_chat(),
        "attempts": 0,
    }


def _set(entry, **fields):
    entry.update(fields, updated=now_sast().isoformat())


def add(text, due):
    entry = _new(text, due, "pending")
    with store() as items:
        items.append(entry)
    return entry


def pending():
    with store() as items:
        return sorted((r for r in items if r["status"] == "pending"),
                      key=lambda r: r["due"])


def cancel(rid):
    with store() as items:
        for r in items:
            if r["id"] == rid and r["status"] == "pending":
                _set(r, status="cancelled")
                return r
    return None


def _deliver(entry, now):
    due = datetime.fromisoformat(entry["due"])
    text = entry["text"]
    if now - due > LATE_AFTER:
        text = f"LATE (Hermes was down, this was due {describe(due, now)}): {text}"
    msg = telegram("sendMessage", {
        "chat_id": entry["chat_id"] or home_chat(),
        "text": text,
        "reply_markup": buttons(entry["id"]),
    })
    _set(entry, status="sent", message_id=msg.get("message_id"))


def send_now(text):
    """Send a reminder immediately (cron scripts). Raises if Telegram fails."""
    entry = _new(text, now_sast(), "sent")
    _deliver(entry, now_sast())
    with store() as items:
        items.append(entry)


def send_due():
    """Send every pending reminder whose time has come. Returns the count."""
    now = now_sast()
    sent = 0
    with store() as items:
        for r in items:
            if r["status"] != "pending" or datetime.fromisoformat(r["due"]) > now:
                continue
            try:
                _deliver(r, now)
                sent += 1
            except Rejected as exc:
                r["attempts"] = r.get("attempts", 0) + 1
                if r["attempts"] >= MAX_ATTEMPTS:
                    _set(r, status="failed")
                    logger.error("reminder-buttons: giving up on %s: %s", r["id"], exc)
            except Exception as exc:
                logger.warning("reminder-buttons: send of %s failed, will retry: %s",
                               r["id"], exc)
    return sent


def finish(rid):
    with store() as items:
        for r in items:
            if r["id"] == rid:
                _set(r, status="done")
                return r
    return None


def snooze(rid, until, message_text):
    """Re-schedule a sent reminder. ``message_text`` is used if the entry is gone."""
    with store() as items:
        for r in items:
            if r["id"] == rid:
                _set(r, status="pending", due=until.isoformat(), attempts=0)
                return r
        entry = _new(_LATE_PREFIX.sub("", message_text), until, "pending")
        items.append(entry)
        return entry
