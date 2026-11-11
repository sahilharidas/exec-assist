"""The evening shutdown: the half of the loop the vault is missing.

The vault's daily notes stop in June and resume only as loose root-level dumps,
which is what happens when writing the note is a separate act of will. Here the
note is a by-product of answering one message.

State lives in its own file rather than in the bot's: the 21:00 cron and the
long-polling bot would otherwise race on bot.json, and a lost 'armed' flag means
the reply gets filed as a random capture instead of the day's log.
"""
import re
from datetime import datetime, timedelta

from . import config, tasks as tasklib, vault
from .store import TaskStore, load, save

STATE = "evening"
ARM_TTL_HOURS = 11   # Still valid for a late-night reply, expired by morning.

PROMPT = """🌙 **Shutdown.**

Reply in one message — messy is fine:
- what actually happened today
- anything still on your mind

I'll write it into today's note and carry the unfinished items into tomorrow.

_Ignore this if today isn't one for it._"""


def arm(when=None):
    when = when or vault.now()
    save(STATE, {
        "armed_at": when.isoformat(),
        "date": when.date().isoformat(),
        "expires": (when + timedelta(hours=ARM_TTL_HOURS)).isoformat(),
    })


def disarm():
    save(STATE, {})


def is_armed(when=None):
    st = load(STATE, {})
    exp = st.get("expires")
    if not exp:
        return False
    try:
        return (when or vault.now()) <= datetime.fromisoformat(exp)
    except ValueError:
        return False


def record(text, when=None):
    """File a shutdown reply into the day's note and close the loop."""
    when = when or vault.now()
    st = load(STATE, {})
    try:
        day = datetime.fromisoformat(st["date"]).date() if st.get("date") else when.date()
    except (ValueError, KeyError):
        day = when.date()

    path = vault.ensure_daily(day)
    body = text.strip()
    vault.append(path, f"### Shutdown — {when.strftime('%H:%M')}\n\n{body}",
                 header="## Log")
    disarm()

    from . import telegram
    open_today = unfinished(day)
    lines = [f"Written into **{vault.rel(path)}**."]
    if open_today:
        lines.append("")
        lines.append(f"{len(open_today)} unfinished from today — they'll head "
                     f"tomorrow's note:")
        for t in open_today[:6]:
            lines.append(f"- {t.text}")
        if len(open_today) > 6:
            lines.append(f"- _…and {len(open_today) - 6} more_")
    lines.append("")
    lines.append("_Done for the day._")
    telegram.send("\n".join(lines))
    return path


def unfinished(day=None):
    """Open checkboxes in a given day's note."""
    day = day or vault.today()
    path = vault.daily_path(day)
    if not path.is_file():
        return []
    return [t for t in tasklib.parse_note(path) if t.open]


# Headings the daily template writes itself; they are not a label for an item.
TEMPLATE_HEADINGS = {"todo", "captured", "log"}
# A short plain line above a group of items ("Spens", "Till") names the group.
MAX_LABEL_CHARS = 40


def _carry_plan(lines):
    """Pick the open items out of a note's lines, keeping their context.

    Returns [(index, label, depth, text)]: the line index, the label the item
    sat under, its depth among the items being carried (a sub-item whose parent
    is finished moves up a level rather than dangling), and its text exactly as
    written, tags and due date included.
    """
    plan = []
    label = None
    stack = []   # (indent, carried) for the checkboxes enclosing this line
    for i, raw in enumerate(lines):
        cm = tasklib.CHECKBOX.match(raw)
        if cm:
            text = cm.group("text").strip()
            if not text:
                continue
            indent = len(cm.group("ws").replace("\t", "    ")) // 4
            while stack and stack[-1][0] >= indent:
                stack.pop()
            carried = cm.group("mark") not in (tasklib.DONE_MARKS | tasklib.CANCELLED_MARKS
                                               | tasklib.MIGRATED_MARKS)
            if carried:
                plan.append((i, label, sum(1 for _, c in stack if c), text))
            stack.append((indent, carried))
            continue
        line = raw.strip()
        if not line:
            continue
        stack = []
        hm = tasklib.HEADING.match(raw)
        bm = tasklib.BOLD_SECTION.match(raw)
        if hm:
            label = _label(hm.group(2))
        elif bm:
            label = _label(bm.group(1))
        elif re.fullmatch(r"[-*_]{3,}", line):
            label = None   # a horizontal rule ends the group
        elif not line.startswith(("-", "*", "+", ">", "|")) and len(line) <= MAX_LABEL_CHARS:
            label = _label(line)
    return plan


def _label(text):
    """A group label worth carrying, or None ("## TODO", "---", "todo")."""
    text = text.strip()
    if text.lower() in TEMPLATE_HEADINGS or not re.search(r"\w", text):
        return None
    return text


def carry_forward(to_day=None, from_day=None):
    """Move the previous day's unfinished items into a day's note.

    A move, not a copy: each carried line is re-marked '[>]' (migrated) in the
    source note. Copying left an open duplicate behind every morning, so an item
    ticked in today's note stayed open in all the older ones -- after two weeks
    each carried item had 14 open copies and ticking one changed nothing.

    Items keep their tags, due dates and nesting, and travel under the label
    they sat under ("Till", "**Spens**"), so a sub-item like "What does it even
    do?" still says what it belongs to.

    Called from the morning snapshot rather than at night, so a note is never
    created for a day that has not started.
    """
    to_day = to_day or vault.today()
    from_day = from_day or (to_day - timedelta(days=1))

    src = vault.daily_path(from_day)
    if not src.is_file():
        return None, []
    lines = vault.read(src).splitlines()
    plan = _carry_plan(lines)
    if not plan:
        return None, []

    dest = vault.ensure_daily(to_day)
    existing = {tasklib.normalise(t.text) for t in tasklib.parse_note(dest)}
    block, moved, last_label = [], [], None
    for i, label, depth, text in plan:
        if tasklib.normalise(tasklib.clean(text)) in existing:
            continue
        if label and label != last_label:
            block.append(f"**{label}**")
        last_label = label
        indent = "\t" * depth
        block.append(f"{indent}- [ ] {text}")
        moved.append(tasklib.clean(text))
    if block:
        vault.append(dest, "\n".join(block), header="## TODO")

    # Re-mark the source only once the destination holds every item. Already
    # present there counts too: it was carried by an earlier run.
    for i, _label, _depth, _text in plan:
        cm = tasklib.CHECKBOX.match(lines[i])
        lines[i] = f"{cm.group('ws')}{cm.group('bullet')} [>] {cm.group('text')}"
    vault.write(src, "\n".join(lines) + "\n")
    return dest, moved


def send_prompt(force=False):
    """The 21:00 nudge. Skipped if the day already has a shutdown entry."""
    from . import telegram
    path = vault.daily_path()
    if not force and path.is_file() and "### Shutdown" in vault.read(path):
        return False, "already logged today"
    arm()
    telegram.send(PROMPT)
    return True, None
