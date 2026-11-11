#!/usr/bin/env python3
"""Inbound Telegram bot: capture, shortlists, focus sessions, unblocking.

Long-polls getUpdates. Only one process may poll a given bot token at a time --
a second consumer silently steals updates from the first -- so this takes an
exclusive lock and refuses to start twice. The outbound-only
scripts/brief-to-telegram.py does not conflict; it never calls getUpdates.

Every inbound update is checked against the owner's chat id before anything is
parsed: the bot's username is discoverable, so an allowlist is a correctness
requirement, not a hardening extra.
"""
import fcntl
import html
import re
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ea import brief, config, evening, llm, stt, tasks as tasklib, telegram, vault
from ea.match import best_match, retag_frog, tick_all
from ea.store import TaskStore, load, save

STATE = "bot"
LOCK = Path("/tmp/ea-bot.lock")
POLL_TIMEOUT = 30
AWAIT_TTL_MIN = 180
# Pending updates older than this at startup are dropped rather than replayed;
# anything newer is still delivered.
BACKLOG_MAX_AGE = 900

COMMANDS = [
    {"command": "today", "description": "Your three, plus anything overdue"},
    {"command": "stale", "description": "The pile, oldest and costliest first"},
    {"command": "waiting", "description": "Things you're waiting on someone for"},
    {"command": "frog", "description": "Show or set the one aversive task"},
    {"command": "done", "description": "Tick something off: /done pay bills"},
    {"command": "focus", "description": "Timed session: /focus 25 inflation model"},
    {"command": "unblock", "description": "Turn a stuck task into one next action"},
    {"command": "stop", "description": "Cancel the running focus session"},
    {"command": "help", "description": "What this bot can do"},
]

HELP = """**What I can do**

Just send me text and I'll write it into today's daily note. Voice notes work too — I transcribe them.

**Shortlists**
/today — your three, plus overdue
/stale — the pile, costliest first
/waiting — what you're waiting on, with ages

**Acting**
/done pay bills — tick it off
/frog — show the one aversive task; `/frog <text>` sets it
/unblock scholarship form — I turn it into one physical next action
/focus 25 inflation model — timed session, I check in

Anything else you send is captured, not parsed. That's the point: get it out of your head, sort it later."""


def state():
    return load(STATE, {"offset": None, "focus": None, "awaiting": None})


def put(st):
    save(STATE, st)


def acquire_lock():
    fh = open(LOCK, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("another ea-bot is already polling this token; refusing to start")
    fh.write(str(time.time()))
    fh.flush()
    return fh


def owner_chat():
    return str(config.secret("TELEGRAM_HOME_CHANNEL", "TELEGRAM_CHAT_ID", required=True))


def log(msg):
    print(f"{datetime.now().astimezone().isoformat(timespec='seconds')} {msg}", flush=True)


# --------------------------------------------------------------------------- capture

def capture(text, kind="note"):
    """Write a captured line into today's daily note under '## Captured'."""
    path = vault.ensure_daily()
    stamp = vault.now().strftime("%H:%M")
    prefix = {"note": "", "voice": "🎙 ", "focus": "⏱ "}.get(kind, "")
    line = f"- {prefix}{text.strip()}  _({stamp})_"
    vault.append(path, line, header="## Captured")
    return path


def capture_task(text):
    """Capture something that is clearly a task as an open checkbox."""
    path = vault.ensure_daily()
    vault.append(path, f"- [ ] {text.strip()}", header="## TODO")
    return path


TASKY = re.compile(
    r"^(?:todo|task|remind|remember|need to|must|should|buy|call|email|message|"
    r"pay|book|send|chase|ask|fix|order|cancel|renew|submit|apply|claim)\b", re.I)


def looks_like_task(text):
    if text.lower().startswith(("- [ ]", "[ ]", "todo:", "task:")):
        return True
    return bool(TASKY.match(text.strip())) and len(text.split()) <= 18


# --------------------------------------------------------------------------- handlers

def cmd_today(arg, st):
    snap = brief.snapshot()
    hour = vault.now().hour
    greet = "Morning" if hour < 12 else ("Afternoon" if hour < 18 else "Tonight")
    telegram.send(brief.render_telegram(snap, greeting=greet))


def cmd_stale(arg, st):
    snap = brief.snapshot()
    store = snap["store"]
    try:
        n = max(1, min(25, int(arg.strip())))
    except (ValueError, AttributeError):
        n = 10
    items = snap["stale"][:n]
    if not items:
        telegram.send("Nothing's gone stale. That is not a sentence I expected to write.")
        return
    L = [f"**{len(snap['stale'])} items going stale.** Costliest first:", ""]
    for t, r in items:
        L.append(f"- {t.text} — _{brief.age_str(store, r)}"
                 f"{f', {t.domain}' if t.domain else ''}_")
    L.append("")
    L.append("_/unblock <a few words> and I'll break one down._")
    telegram.send("\n".join(L))


def cmd_waiting(arg, st):
    snap = brief.snapshot()
    store = snap["store"]
    live = [(t, r) for t, r in store.sync() if t.open and t.waiting]
    if not live:
        telegram.send("Nothing tagged `[WAIT]` is open. Tag things `[WAIT]` in TODO.md "
                      "and I'll track how long they've been sitting.")
        return
    L = ["**Waiting on someone:**", ""]
    for t, r in sorted(live, key=lambda tr: store.age_days(tr[1]), reverse=True):
        L.append(f"- {t.text} — _{brief.age_str(store, r)}_"
                 f"{f' ({t.domain})' if t.domain else ''}")
    L.append("")
    L.append("_Anything over a week is usually a nudge, not a wait._")
    telegram.send("\n".join(L))


def cmd_frog(arg, st):
    store = TaskStore()
    live = store.sync()
    store.commit()
    frogs = [(t, r) for t, r in live if t.open and t.frog]
    arg = (arg or "").strip()
    if not arg:
        if frogs:
            t, r = frogs[0]
            telegram.send(f"🐸 **The frog:** {t.text}\n_open {brief.age_str(store, r)}_\n\n"
                          f"_/unblock to break it down, or `/frog <something else>` to swap it._")
        else:
            telegram.send("No frog set. `/frog <task>` marks the one aversive thing.\n\n"
                          "_Your own classification note says this category holds exactly one item._")
        return
    # Tag an existing task if we can find it; otherwise create it in TODO.md.
    task, _rec, confident, match = best_match(live, arg)
    if confident and retag_frog(task, live):
        telegram.send(f"🐸 Frog is now: **{task.text}**\n_Tagged in {task.where}; "
                      f"any previous frog untagged._")
        return
    vault.append(config.TODO_NOTE, f"- [ ] [FROG] {arg}", header="### 🐸 DOMAIN: THE FROG")
    msg = [f"🐸 Frog set: **{arg}**", "_Added to TODO.md._"]
    if match:
        msg.append("")
        msg.append("_Didn't match an existing item confidently. Nearest was: "
                   f"{match[0][1].text}_")
    telegram.send("\n".join(msg))


def cmd_done(arg, st):
    arg = (arg or "").strip()
    if not arg:
        telegram.send("`/done <a few words of the task>`")
        return
    store = TaskStore()
    live = store.sync()
    store.commit()
    task, _rec, confident, match = best_match(live, arg)
    if not match:
        # Nothing matched, so record it as a thing that happened rather than
        # losing it: the user clearly did something.
        capture(f"done: {arg}", kind="note")
        telegram.send(f"Couldn't find an open task matching “{arg}”, so I logged it "
                      f"under today's **Captured** instead.")
        return
    if confident:
        where = tick_all(task)
        if where:
            store = TaskStore()
            store.sync()
            store.commit()
            extra = f" (+{len(where) - 1} duplicate)" if len(where) > 1 else ""
            telegram.send(f"✅ **{task.text}**\n_ticked in {where[0]}{extra}_")
        else:
            telegram.send(f"Found “{task.text}” but couldn't rewrite the line. "
                          f"Odd — check {task.where}.")
        return
    options = match[:6]
    st["awaiting"] = {
        "mode": "done_pick",
        "expires": (vault.now() + timedelta(minutes=AWAIT_TTL_MIN)).isoformat(),
        "options": [{"text": t.text, "file": str(t.file)} for _s, t, _r in options],
    }
    put(st)
    L = [f"**{len(match)} near matches.** Reply with a number:", ""]
    for i, (_s, t, _r) in enumerate(options, 1):
        L.append(f"{i}. {t.text} — _{t.where}_")
    telegram.send("\n".join(L))


def handle_done_pick(text, st):
    aw = st.get("awaiting") or {}
    options = aw.get("options", [])
    m = re.match(r"^\s*(\d{1,2})\s*$", text)
    if not m:
        return False
    idx = int(m.group(1)) - 1
    if not (0 <= idx < len(options)):
        telegram.send(f"Pick 1–{len(options)}.")
        return True
    chosen = options[idx]
    store = TaskStore()
    live = store.sync()
    store.commit()
    match = [(t, r) for t, r in live
             if tasklib.normalise(t.text) == tasklib.normalise(chosen["text"])]
    st["awaiting"] = None
    put(st)
    where = tick_all(match[0][0]) if match else []
    if where:
        store = TaskStore()
        store.sync()
        store.commit()
        extra = f" (+{len(where) - 1} duplicate)" if len(where) > 1 else ""
        telegram.send(f"✅ **{chosen['text']}**\n_ticked in {where[0]}{extra}_")
    else:
        telegram.send(f"Couldn't tick “{chosen['text']}” — it may have changed. Try /done again.")
    return True


UNBLOCK_SYSTEM = """You help someone start tasks they keep putting off. \
Getting started is the hard part, not knowledge or motivation.

Given a stuck task, reply with:
1. One physical first action that takes under two minutes and requires no decisions. \
Name the exact app, page, drawer or person. Not "gather documents" but "open the ER \
receipt in Downloads and screenshot page 1".
2. The next two actions after that, one line each.
3. If the task is genuinely several tasks, say so in one line and split it.

Rules: no encouragement, no preamble, no "you've got this". If information is \
missing, state the one thing to look up first. Under 120 words. Plain text, no headers."""


def cmd_unblock(arg, st):
    arg = (arg or "").strip()
    if not arg:
        telegram.send("`/unblock <a few words of the task>` — I'll turn it into one next action.")
        return
    if not llm.available():
        telegram.send("No LLM key resolved, so I can't break tasks down right now.")
        return
    store = TaskStore()
    live = store.sync()
    store.commit()
    task, rec, _confident, match = best_match(live, arg)
    target, context = arg, ""
    if match:
        t, r = task, rec
        target = t.text
        bits = [b for b in (t.domain, t.section) if b]
        context = (f"\nVault context: {' / '.join(bits)}."
                   f" Open {brief.age_str(store, r)}." if bits else "")
        kids = [k.text for k in tasklib.parse_note(t.file)
                if k.indent > t.indent and k.lineno > t.lineno][:4]
        if kids:
            context += f"\nExisting sub-items: {'; '.join(kids)}."
    telegram.send(f"🔧 Breaking down: _{target}_…")
    out, err = llm.ask(f"Stuck task: {target}{context}", system=UNBLOCK_SYSTEM,
                       max_tokens=420, temperature=0.2)
    if err:
        telegram.send(f"Couldn't reach the model: {err}")
        return
    telegram.send(f"**{target}**\n\n{out}")
    path = vault.ensure_daily()
    vault.append(path, f"- 🔧 **unblock** {target}\n" +
                 "\n".join(f"  {ln}" for ln in out.splitlines() if ln.strip()),
                 header="## Log")


def cmd_focus(arg, st):
    arg = (arg or "").strip()
    m = re.match(r"^(\d{1,3})\s*m?\s*(.*)$", arg)
    minutes, label = (int(m.group(1)), m.group(2).strip()) if m else (25, arg)
    minutes = max(1, min(180, minutes))
    if not label:
        snap = brief.snapshot()
        label = snap["three"][0]["task"].text if snap["three"] else "unspecified"
    now = vault.now()
    st["focus"] = {
        "label": label,
        "started": now.isoformat(),
        "ends": (now + timedelta(minutes=minutes)).isoformat(),
        "mid_sent": minutes < 10,
        "minutes": minutes,
    }
    put(st)
    telegram.send(f"⏱ **{minutes} min** on _{label}_.\n\n"
                  f"Ends {(now + timedelta(minutes=minutes)).strftime('%H:%M')}. "
                  f"I'll check in{'' if minutes < 10 else ' halfway'}. /stop to cancel.")
    vault.append(vault.ensure_daily(),
                 f"- ⏱ started {minutes}min focus — {label}  _({now.strftime('%H:%M')})_",
                 header="## Log")


def cmd_skip(arg, st):
    if evening.is_armed():
        evening.disarm()
        telegram.send("Skipped. Nothing logged for tonight.")
        return
    telegram.send("Nothing to skip.")


def cmd_stop(arg, st):
    if not st.get("focus"):
        telegram.send("No focus session running.")
        return
    label = st["focus"]["label"]
    st["focus"] = None
    put(st)
    telegram.send(f"Stopped: _{label}_. No judgement — stopping deliberately beats drifting.")


def tick_focus(st):
    """Fire halfway and end-of-session check-ins. Called once per poll cycle."""
    f = st.get("focus")
    if not f:
        return
    now = vault.now()
    try:
        started = datetime.fromisoformat(f["started"])
        ends = datetime.fromisoformat(f["ends"])
    except (ValueError, KeyError):
        st["focus"] = None
        put(st)
        return
    if not f.get("mid_sent") and now >= started + (ends - started) / 2:
        f["mid_sent"] = True
        put(st)
        telegram.send(f"⏱ Halfway on _{f['label']}_. Still on it, or drifted?")
        return
    if now >= ends:
        st["focus"] = None
        st["awaiting"] = {
            "mode": "focus_log",
            "label": f["label"],
            "expires": (now + timedelta(minutes=30)).isoformat(),
        }
        put(st)
        telegram.send(f"⏱ **Time.** {f['minutes']} min on _{f['label']}_.\n\n"
                      f"Reply with what you got done and I'll log it. "
                      f"`/focus {f['minutes']}` to go again.")
        vault.append(vault.ensure_daily(),
                     f"- ⏱ finished {f['minutes']}min focus — {f['label']}  "
                     f"_({now.strftime('%H:%M')})_", header="## Log")


def handle_focus_log(text, st):
    label = (st.get("awaiting") or {}).get("label", "focus session")
    vault.append(vault.ensure_daily(), f"- ⏱ **{label}:** {text.strip()}", header="## Log")
    st["awaiting"] = None
    put(st)
    telegram.send("Logged.")
    return True


def handle_evening(text, st):
    """Route a reply to the 21:00 shutdown prompt into the daily note."""
    evening.record(text)
    st["awaiting"] = None
    put(st)
    return True


# --------------------------------------------------------------------------- dispatch

HANDLERS = {
    "today": cmd_today, "brief": cmd_today,
    "stale": cmd_stale, "pile": cmd_stale,
    "waiting": cmd_waiting, "wait": cmd_waiting,
    "frog": cmd_frog,
    "done": cmd_done, "did": cmd_done,
    "unblock": cmd_unblock, "next": cmd_unblock,
    "focus": cmd_focus, "pomodoro": cmd_focus,
    "stop": cmd_stop, "cancel": cmd_stop,
    "skip": cmd_skip,
}

AWAIT_HANDLERS = {
    "done_pick": handle_done_pick,
    "focus_log": handle_focus_log,
    "evening": handle_evening,
}


def awaiting_mode(st):
    aw = st.get("awaiting")
    if not aw:
        return None
    exp = aw.get("expires")
    if exp:
        try:
            if vault.now() > datetime.fromisoformat(exp):
                st["awaiting"] = None
                put(st)
                return None
        except ValueError:
            pass
    return aw.get("mode")


def pending_mode(st):
    """The bot's own awaiting mode, else the cron-armed evening prompt."""
    return awaiting_mode(st) or ("evening" if evening.is_armed() else None)


def handle_text(text, st):
    text = text.strip()
    if not text:
        return
    if text.startswith("/"):
        m = re.match(r"^/([a-zA-Z_]+)(?:@\w+)?\s*(.*)$", text, re.S)
        if not m:
            return
        name, arg = m.group(1).lower(), m.group(2)
        if name in ("help", "start"):
            telegram.send(HELP)
            return
        fn = HANDLERS.get(name)
        if fn:
            # A command always wins over a pending prompt: it is an explicit
            # change of subject.
            if name not in ("done",):
                st["awaiting"] = None
                put(st)
            fn(arg, st)
        else:
            telegram.send(f"No command `/{name}`. /help for the list.")
        return

    mode = pending_mode(st)
    if mode and AWAIT_HANDLERS.get(mode):
        if AWAIT_HANDLERS[mode](text, st):
            return

    if looks_like_task(text):
        path = capture_task(text)
        telegram.send(f"📥 Task added to {vault.rel(path)}")
    else:
        path = capture(text)
        telegram.send(f"📥 Captured.")


def handle_voice(msg, st):
    file_id = (msg.get("voice") or msg.get("audio") or msg.get("video_note") or {}).get("file_id")
    if not file_id:
        return
    data, err = telegram.file_bytes(file_id)
    if err:
        telegram.send(f"Couldn't fetch that audio: {err}")
        return
    text, err = stt.transcribe(data)
    if err or not text:
        saved = stt.park(data)
        telegram.send(f"Couldn't transcribe that ({err}). Saved the audio to "
                      f"`{vault.rel(saved)}` so it isn't lost.")
        return
    mode = pending_mode(st)
    if mode and AWAIT_HANDLERS.get(mode):
        if AWAIT_HANDLERS[mode](text, st):
            telegram.send(f"_heard:_ {text}")
            return
    if looks_like_task(text):
        capture_task(text)
        telegram.send(f"🎙 → 📥 **Task:** {text}")
    else:
        capture(text, kind="voice")
        telegram.send(f"🎙 → 📥 _{text}_")


def handle_update(upd, owner, st):
    msg = upd.get("message") or upd.get("edited_message")
    if not msg:
        return
    chat_id = str((msg.get("chat") or {}).get("id"))
    kind = ("voice" if msg.get("voice") else "audio" if msg.get("audio")
            else "video_note" if msg.get("video_note") else "text")
    preview = (msg.get("text") or msg.get("caption") or "")[:60]
    # Without this, a working inbound path logs nothing at all and is
    # indistinguishable from a dead one.
    log(f"recv update={upd.get('update_id')} chat={chat_id} kind={kind} {preview!r}")
    if chat_id != owner:
        log(f"ignored: chat {chat_id} is not the owner ({owner})")
        return
    if msg.get("voice") or msg.get("audio") or msg.get("video_note"):
        handle_voice(msg, st)
        return
    text = msg.get("text") or msg.get("caption")
    if text:
        handle_text(text, st)


DEDICATED_TOKEN_HELP = """ea-bot needs its own Telegram bot and does not have one.

TELEGRAM_BOT_TOKEN belongs to the agent's own bot, which the Hermes agent
already long-polls. Telegram permits one getUpdates consumer per token, so
running both makes them steal each other's messages.

To fix:
  1. Message @BotFather on Telegram, send /newbot, and follow the prompts.
  2. Add the token it gives you to your .env file as:
       EA_TELEGRAM_BOT_TOKEN="<token>"
  3. sudo systemctl enable --now ea-bot

Everything on a timer (morning snapshot, midday nudge, evening prompt, weekly
review) works without this -- those only send, and sending never conflicts."""


def main():
    config.ensure_dirs()
    if not telegram.require_dedicated():
        sys.exit(DEDICATED_TOKEN_HELP)
    lock = acquire_lock()
    owner = owner_chat()
    who = telegram.me()
    if not who.get("ok"):
        sys.exit(f"cannot reach Telegram: {who.get('description')}")
    bot_id = who["result"].get("id")
    log(f"started as @{who['result'].get('username')} ({bot_id}) for chat {owner}")
    telegram.set_commands(COMMANDS)

    st = state()
    # Update ids are allocated per bot, so an offset saved under a different bot
    # is meaningless and usually far higher than the new bot's ids -- which makes
    # getUpdates silently discard every incoming message rather than erroring.
    # Switching tokens therefore has to reset the offset.
    switched = st.get("bot_id") != bot_id
    if switched:
        if st.get("offset") is not None:
            log(f"bot changed (was {st.get('bot_id')}); discarding stale offset "
                f"{st['offset']}")
        st["offset"] = None
        st["bot_id"] = bot_id
        put(st)
    # Replaying hours-old captures is worse than dropping them, but the original
    # form of this skip discarded the pending backlog unconditionally whenever
    # the offset was null -- which silently ate a message that had only just
    # arrived, on every such restart. Skip by AGE instead: anything recent is
    # still worth delivering.
    if st.get("offset") is None:
        batch, err = telegram.get_updates(offset=-1, timeout=0)
        if err:
            log(f"backlog probe failed: {err}")
        elif batch:
            newest = batch[-1]
            msg = newest.get("message") or newest.get("edited_message") or {}
            age = time.time() - float(msg.get("date") or 0)
            if age > BACKLOG_MAX_AGE:
                st["offset"] = newest["update_id"] + 1
                put(st)
                log(f"skipped backlog of {int(age)}s old, offset={st['offset']}")
            else:
                log(f"pending update is only {int(age)}s old; delivering it")

    last_beat = time.time()
    while True:
        try:
            st = state()
            tick_focus(st)
            stt.maybe_unload()
            if time.time() - last_beat > 600:
                last_beat = time.time()
                log(f"alive, polling (offset={st.get('offset')})")
            batch, err = telegram.get_updates(offset=st.get("offset"), timeout=POLL_TIMEOUT,
                                              allowed=["message", "edited_message"])
            if err:
                log(f"getUpdates error: {err}")
                time.sleep(10)
                continue
            for upd in batch or []:
                st = state()
                st["offset"] = upd["update_id"] + 1
                put(st)
                try:
                    handle_update(upd, owner, st)
                except Exception:
                    log("handler crashed:\n" + traceback.format_exc())
                    try:
                        telegram.send("Something broke handling that. It's logged — "
                                      "the message itself wasn't lost.")
                    except Exception:
                        pass
        except KeyboardInterrupt:
            log("shutting down")
            return
        except Exception:
            # The daemon must outlive any single failure, including a Telegram
            # outage or a malformed update.
            log("loop crashed:\n" + traceback.format_exc())
            time.sleep(15)


if __name__ == "__main__":
    main()
